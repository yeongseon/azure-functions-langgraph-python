from __future__ import annotations

import threading
from typing import Any

import azure.functions as func

from azure_functions_langgraph._json import dumps
from azure_functions_langgraph._validation import (
    validate_thread_id,
)
from azure_functions_langgraph.authorization import (
    AuthorizationAction,
    AuthorizationContext,
    authorization_status,
)
from azure_functions_langgraph.observability import (
    finish_context,
    new_run_context,
    request_id_from_headers,
    safe_observer_call,
)
from azure_functions_langgraph.platform._common import (
    PlatformRouteDeps,
    _build_sse_response,
    _build_threaded_config,
    _build_threadless_config,
    _check_stream_overflow,
    _get_threadless_graph,
    _normalize_stream_mode,
    _parse_run_create,
    _platform_error,
    _resolve_context_kwarg,
    _resolve_run_graph,
    logger,
)
from azure_functions_langgraph.platform._sse import (
    format_data_event,
    format_end_event,
    format_error_event,
    format_metadata_event,
)
from azure_functions_langgraph.platform.contracts import ThreadStatus
from azure_functions_langgraph.protocols import StreamableGraph

_STORE_RENEWALS: dict[str, threading.Event] = {}


def _start_store_renewal(deps: PlatformRouteDeps, thread_id: str, owner_token: str) -> None:
    renew = getattr(deps.thread_store, "renew_run_lock", None)
    if not callable(renew):
        return
    stop = threading.Event()
    _STORE_RENEWALS[owner_token] = stop

    def worker() -> None:
        while not stop.wait(20):
            if not renew(thread_id, owner_token):
                logger.warning("Run-lock renewal lost ownership for thread %s", thread_id)
                return

    threading.Thread(target=worker, daemon=True).start()


def _release_thread_run_lock(
    deps: PlatformRouteDeps,
    thread_id: str,
    graph_name: str,
    lock_token: str,
    *,
    status: ThreadStatus,
    values: dict[str, Any] | None = None,
    store_owner_token: str | None = None,
) -> bool:
    renewal = _STORE_RENEWALS.pop(store_owner_token or "", None)
    if renewal is not None:
        renewal.set()
    try:
        deps.thread_store.release_run_lock(
            thread_id,
            status=status,
            values=values,
            owner_token=store_owner_token or lock_token,
        )
    except KeyError:
        logger.warning(
            "Thread %s disappeared before run lock release to %s",
            thread_id,
            status,
        )
    except Exception:
        logger.exception(
            "Unexpected error releasing run lock for thread %s (target status: %s)",
            thread_id,
            status,
        )
        return False
    finally:
        try:
            deps.thread_lock.release(graph_name, thread_id, lock_token)
        except Exception:
            logger.exception("Unexpected error releasing thread lock for thread %s", thread_id)
    return True


def register_run_routes(
    app: func.FunctionApp,
    deps: PlatformRouteDeps,
) -> None:
    auth = deps.auth_level

    def denied(req: func.HttpRequest, thread_id: str | None) -> func.HttpResponse | None:
        status = authorization_status(
            deps.authorize,
            AuthorizationContext(AuthorizationAction.RUN, None, thread_id, req),
        )
        return _platform_error(status, "Access denied") if status is not None else None

    @app.function_name(name="aflg_platform_runs_wait")
    @app.route(route="threads/{thread_id}/runs/wait", methods=["POST"], auth_level=auth)
    def runs_wait(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        if response := denied(req, thread_id):
            return response
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        parsed = _parse_run_create(req, deps, require_dict_body=False)
        if isinstance(parsed, func.HttpResponse):
            return parsed
        run_req = parsed

        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        reg = _resolve_run_graph(run_req, deps)
        if isinstance(reg, func.HttpResponse):
            return reg

        context_kwargs = _resolve_context_kwarg(reg.graph.invoke, run_req.context, reg.name)
        if isinstance(context_kwargs, func.HttpResponse):
            return context_kwargs

        run_id = request_id_from_headers(req.headers)
        ctx = new_run_context(
            reg.name,
            "invoke",
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=run_req.assistant_id,
            has_checkpointer=getattr(reg.graph, "checkpointer", None) is not None,
        )

        lock_token = deps.thread_lock.acquire(reg.name, thread_id)
        if lock_token is None:
            safe_observer_call(
                deps.observer, "on_run_rejected", finish_context(ctx), "lock_contention"
            )
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"Concurrent runs are not supported (multitask_strategy=reject).",
            )
        try:
            locked = deps.thread_store.try_acquire_run_lock(
                thread_id,
                assistant_id=run_req.assistant_id,
            )
        except KeyError:
            deps.thread_lock.release(reg.name, thread_id, lock_token)
            return _platform_error(404, f"Thread {thread_id!r} not found")
        except ValueError as exc:
            deps.thread_lock.release(reg.name, thread_id, lock_token)
            return _platform_error(409, str(exc))
        except Exception as exc:
            try:
                safe_observer_call(deps.observer, "on_run_failed", finish_context(ctx), exc)
                return _platform_error(500, "Graph execution failed", ctx.run_id)
            finally:
                deps.thread_lock.release(reg.name, thread_id, lock_token)
        if locked is None:
            deps.thread_lock.release(reg.name, thread_id, lock_token)
            safe_observer_call(
                deps.observer, "on_run_rejected", finish_context(ctx), "lock_contention"
            )
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"Concurrent runs are not supported (multitask_strategy=reject).",
            )
        store_lock_token = locked.lock_token or lock_token
        status: ThreadStatus = "error"
        output = None
        try:
            _start_store_renewal(deps, thread_id, store_lock_token)
            config = _build_threaded_config(run_req, thread_id)
            if run_req.metadata:
                merged_metadata = {**(thread.metadata or {}), **run_req.metadata}
                deps.thread_store.update(thread_id, metadata=merged_metadata)
            graph_input = run_req.input or {}
            safe_observer_call(deps.observer, "on_run_started", ctx)
            result = reg.graph.invoke(graph_input, config=config, **context_kwargs)
            output = result if isinstance(result, dict) else {"result": result}
            response = func.HttpResponse(
                body=dumps(output),
                mimetype="application/json",
                status_code=200,
                headers={"Content-Location": f"/api/threads/{thread_id}/runs/{run_id}"},
            )
            status = "idle"
        except Exception as exc:
            logger.exception(
                "Graph %s invoke failed for thread %s",
                run_req.assistant_id,
                thread_id,
            )
            safe_observer_call(deps.observer, "on_run_failed", finish_context(ctx), exc)
            return _platform_error(500, "Graph execution failed", ctx.run_id)
        finally:
            released = _release_thread_run_lock(
                deps,
                thread_id,
                reg.name,
                lock_token,
                status=status,
                values=output if status == "idle" else None,
                store_owner_token=store_lock_token,
            )
            if not released:
                if status == "idle":
                    safe_observer_call(
                        deps.observer,
                        "on_run_failed",
                        finish_context(ctx),
                        RuntimeError("Run-lock release failed"),
                    )
                return _platform_error(500, "Run-lock release failed", ctx.run_id)
        safe_observer_call(deps.observer, "on_run_completed", finish_context(ctx))
        return response

    @app.function_name(name="aflg_platform_runs_stream")
    @app.route(
        route="threads/{thread_id}/runs/stream",
        methods=["POST"],
        auth_level=auth,
    )
    def runs_stream(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        if response := denied(req, thread_id):
            return response
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        parsed = _parse_run_create(req, deps, require_dict_body=False)
        if isinstance(parsed, func.HttpResponse):
            return parsed
        run_req = parsed

        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        stream_mode, mode_err = _normalize_stream_mode(run_req.stream_mode)
        if mode_err is not None:
            return mode_err

        reg = _resolve_run_graph(run_req, deps)
        if isinstance(reg, func.HttpResponse):
            return reg

        if not isinstance(reg.graph, StreamableGraph):
            return _platform_error(
                501,
                f"Graph {run_req.assistant_id!r} does not support streaming",
            )

        context_kwargs = _resolve_context_kwarg(reg.graph.stream, run_req.context, reg.name)
        if isinstance(context_kwargs, func.HttpResponse):
            return context_kwargs

        run_id = request_id_from_headers(req.headers)
        ctx = new_run_context(
            reg.name,
            "stream",
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=run_req.assistant_id,
            has_checkpointer=getattr(reg.graph, "checkpointer", None) is not None,
            stream_mode=stream_mode,
        )
        lock_token = deps.thread_lock.acquire(reg.name, thread_id)
        if lock_token is None:
            safe_observer_call(
                deps.observer, "on_run_rejected", finish_context(ctx), "lock_contention"
            )
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"Concurrent runs are not supported (multitask_strategy=reject).",
            )
        try:
            locked = deps.thread_store.try_acquire_run_lock(
                thread_id,
                assistant_id=run_req.assistant_id,
            )
        except KeyError:
            deps.thread_lock.release(reg.name, thread_id, lock_token)
            return _platform_error(404, f"Thread {thread_id!r} not found")
        except ValueError as exc:
            deps.thread_lock.release(reg.name, thread_id, lock_token)
            return _platform_error(409, str(exc))
        except Exception as exc:
            try:
                safe_observer_call(deps.observer, "on_run_failed", finish_context(ctx), exc)
                return _platform_error(500, "Graph execution failed", ctx.run_id)
            finally:
                deps.thread_lock.release(reg.name, thread_id, lock_token)
        if locked is None:
            deps.thread_lock.release(reg.name, thread_id, lock_token)
            safe_observer_call(
                deps.observer, "on_run_rejected", finish_context(ctx), "lock_contention"
            )
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"Concurrent runs are not supported (multitask_strategy=reject).",
            )
        store_lock_token = locked.lock_token or lock_token
        chunks: list[str] = []
        status: ThreadStatus = "error"
        try:
            _start_store_renewal(deps, thread_id, store_lock_token)
            config = _build_threaded_config(run_req, thread_id)
            if run_req.metadata:
                merged_metadata = {**(thread.metadata or {}), **run_req.metadata}
                deps.thread_store.update(thread_id, metadata=merged_metadata)
            graph_input = run_req.input or {}
            buffered_bytes = 0
            max_bytes = deps.max_stream_response_bytes
            meta_chunk = format_metadata_event(run_id)
            chunks.append(meta_chunk)
            buffered_bytes += len(meta_chunk.encode())
            safe_observer_call(deps.observer, "on_run_started", ctx)

            if _check_stream_overflow(chunks, buffered_bytes, 0, max_bytes):
                safe_observer_call(
                    deps.observer,
                    "on_run_failed",
                    finish_context(ctx),
                    RuntimeError("stream response exceeded max buffered size"),
                )
                return _build_sse_response(
                    chunks,
                    content_location=f"/api/threads/{thread_id}/runs/{run_id}",
                )
            for event in reg.graph.stream(
                graph_input,
                config=config,
                stream_mode=stream_mode,
                **context_kwargs,
            ):
                chunk = format_data_event(stream_mode, event)
                chunk_bytes = len(chunk.encode())
                if _check_stream_overflow(chunks, buffered_bytes, chunk_bytes, max_bytes):
                    safe_observer_call(
                        deps.observer,
                        "on_run_failed",
                        finish_context(ctx),
                        RuntimeError("stream response exceeded max buffered size"),
                    )
                    return _build_sse_response(
                        chunks,
                        content_location=f"/api/threads/{thread_id}/runs/{run_id}",
                    )
                chunks.append(chunk)
                buffered_bytes += chunk_bytes
            chunks.append(format_end_event())
            response = _build_sse_response(
                chunks,
                content_location=f"/api/threads/{thread_id}/runs/{run_id}",
            )
            status = "idle"
        except Exception as exc:
            logger.exception(
                "Graph %s stream failed for thread %s",
                run_req.assistant_id,
                thread_id,
            )
            safe_observer_call(deps.observer, "on_run_failed", finish_context(ctx), exc)
            if not chunks:
                return _platform_error(500, "Graph execution failed", ctx.run_id)
            chunks.append(format_error_event("stream processing failed", ctx.run_id))
            chunks.append(format_end_event())
            return _build_sse_response(
                chunks,
                content_location=f"/api/threads/{thread_id}/runs/{run_id}",
            )

        finally:
            released = _release_thread_run_lock(
                deps,
                thread_id,
                reg.name,
                lock_token,
                status=status,
                store_owner_token=store_lock_token,
            )
            if not released:
                if status == "idle":
                    safe_observer_call(
                        deps.observer,
                        "on_run_failed",
                        finish_context(ctx),
                        RuntimeError("Run-lock release failed"),
                    )
                return _platform_error(500, "Run-lock release failed", ctx.run_id)
        safe_observer_call(deps.observer, "on_run_completed", finish_context(ctx))
        return response

    @app.function_name(name="aflg_platform_runs_wait_threadless")
    @app.route(route="runs/wait", methods=["POST"], auth_level=auth)
    def runs_wait_threadless(req: func.HttpRequest) -> func.HttpResponse:
        if response := denied(req, None):
            return response
        parsed = _parse_run_create(req, deps, require_dict_body=True)
        if isinstance(parsed, func.HttpResponse):
            return parsed
        run_req = parsed

        reg = _resolve_run_graph(run_req, deps)
        if isinstance(reg, func.HttpResponse):
            return reg

        exec_graph = _get_threadless_graph(reg.graph)
        if exec_graph is None:
            return _platform_error(
                501,
                f"Graph {run_req.assistant_id!r} has a checkpointer that cannot "
                f"be disabled for threadless execution.",
            )

        config = _build_threadless_config(run_req)
        if isinstance(config, func.HttpResponse):
            return config

        context_kwargs = _resolve_context_kwarg(exec_graph.invoke, run_req.context, reg.name)
        if isinstance(context_kwargs, func.HttpResponse):
            return context_kwargs

        run_id = request_id_from_headers(req.headers)
        ctx = new_run_context(
            reg.name,
            "invoke",
            run_id=run_id,
            assistant_id=run_req.assistant_id,
            has_checkpointer=False,
        )
        graph_input = run_req.input or {}
        safe_observer_call(deps.observer, "on_run_started", ctx)
        try:
            result = exec_graph.invoke(graph_input, config=config, **context_kwargs)
        except Exception as exc:
            logger.exception(
                "Graph %s invoke failed (threadless)",
                run_req.assistant_id,
            )
            safe_observer_call(deps.observer, "on_run_failed", finish_context(ctx), exc)
            return _platform_error(500, "Graph execution failed", ctx.run_id)

        output = result if isinstance(result, dict) else {"result": result}
        safe_observer_call(deps.observer, "on_run_completed", finish_context(ctx))

        return func.HttpResponse(
            body=dumps(output),
            mimetype="application/json",
            status_code=200,
            headers={"Content-Location": f"/api/runs/{run_id}"},
        )

    @app.function_name(name="aflg_platform_runs_stream_threadless")
    @app.route(route="runs/stream", methods=["POST"], auth_level=auth)
    def runs_stream_threadless(req: func.HttpRequest) -> func.HttpResponse:
        if response := denied(req, None):
            return response
        parsed = _parse_run_create(req, deps, require_dict_body=True)
        if isinstance(parsed, func.HttpResponse):
            return parsed
        run_req = parsed

        stream_mode, mode_err = _normalize_stream_mode(run_req.stream_mode)
        if mode_err is not None:
            return mode_err

        reg = _resolve_run_graph(run_req, deps)
        if isinstance(reg, func.HttpResponse):
            return reg

        exec_graph = _get_threadless_graph(reg.graph)
        if exec_graph is None:
            return _platform_error(
                501,
                f"Graph {run_req.assistant_id!r} has a checkpointer that cannot "
                f"be disabled for threadless execution.",
            )

        if not isinstance(exec_graph, StreamableGraph):
            return _platform_error(
                501,
                f"Graph {run_req.assistant_id!r} does not support streaming",
            )

        config = _build_threadless_config(run_req)
        if isinstance(config, func.HttpResponse):
            return config

        context_kwargs = _resolve_context_kwarg(exec_graph.stream, run_req.context, reg.name)
        if isinstance(context_kwargs, func.HttpResponse):
            return context_kwargs

        run_id = request_id_from_headers(req.headers)
        ctx = new_run_context(
            reg.name,
            "stream",
            run_id=run_id,
            assistant_id=run_req.assistant_id,
            has_checkpointer=False,
            stream_mode=stream_mode,
        )

        graph_input = run_req.input or {}
        chunks: list[str] = []
        buffered_bytes = 0
        max_bytes = deps.max_stream_response_bytes

        meta_chunk = format_metadata_event(run_id)
        chunks.append(meta_chunk)
        buffered_bytes += len(meta_chunk.encode())
        safe_observer_call(deps.observer, "on_run_started", ctx)

        if _check_stream_overflow(chunks, buffered_bytes, 0, max_bytes):
            safe_observer_call(
                deps.observer,
                "on_run_failed",
                finish_context(ctx),
                RuntimeError("stream response exceeded max buffered size"),
            )
            return _build_sse_response(
                chunks,
                content_location=f"/api/runs/{run_id}",
            )
        try:
            for event in exec_graph.stream(
                graph_input,
                config=config,
                stream_mode=stream_mode,
                **context_kwargs,
            ):
                chunk = format_data_event(stream_mode, event)
                chunk_bytes = len(chunk.encode())
                if _check_stream_overflow(chunks, buffered_bytes, chunk_bytes, max_bytes):
                    safe_observer_call(
                        deps.observer,
                        "on_run_failed",
                        finish_context(ctx),
                        RuntimeError("stream response exceeded max buffered size"),
                    )
                    return _build_sse_response(
                        chunks,
                        content_location=f"/api/runs/{run_id}",
                    )
                chunks.append(chunk)
                buffered_bytes += chunk_bytes
        except Exception as exc:
            logger.exception(
                "Graph %s stream failed (threadless)",
                run_req.assistant_id,
            )
            chunks.append(format_error_event("stream processing failed", ctx.run_id))
            safe_observer_call(deps.observer, "on_run_failed", finish_context(ctx), exc)
            chunks.append(format_end_event())
            return _build_sse_response(
                chunks,
                content_location=f"/api/runs/{run_id}",
            )

        chunks.append(format_end_event())
        safe_observer_call(deps.observer, "on_run_completed", finish_context(ctx))

        return _build_sse_response(
            chunks,
            content_location=f"/api/runs/{run_id}",
        )
