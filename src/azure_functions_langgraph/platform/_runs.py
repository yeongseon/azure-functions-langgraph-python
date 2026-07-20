from __future__ import annotations

import json
from typing import Any
import uuid

import azure.functions as func

from azure_functions_langgraph._validation import (
    validate_thread_id,
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


def _release_thread_run_lock(
    deps: PlatformRouteDeps,
    thread_id: str,
    *,
    status: ThreadStatus,
    values: dict[str, Any] | None = None,
) -> None:
    try:
        deps.thread_store.release_run_lock(thread_id, status=status, values=values)
    except KeyError:
        logger.warning(
            "Thread %s disappeared before run lock release to %s",
            thread_id,
            status,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Unexpected error releasing run lock for thread %s (target status: %s)",
            thread_id,
            status,
        )


def register_run_routes(
    app: func.FunctionApp,
    deps: PlatformRouteDeps,
) -> None:
    auth = deps.auth_level

    @app.function_name(name="aflg_platform_runs_wait")
    @app.route(route="threads/{thread_id}/runs/wait", methods=["POST"], auth_level=auth)
    def runs_wait(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
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

        try:
            locked = deps.thread_store.try_acquire_run_lock(
                thread_id,
                assistant_id=run_req.assistant_id,
            )
        except KeyError:
            return _platform_error(404, f"Thread {thread_id!r} not found")
        except ValueError as exc:
            return _platform_error(409, str(exc))
        if locked is None:
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"Concurrent runs are not supported (multitask_strategy=reject).",
            )

        config = _build_threaded_config(run_req, thread_id)

        graph_input = run_req.input or {}
        try:
            result = reg.graph.invoke(graph_input, config=config)
        except Exception:
            logger.exception(
                "Graph %s invoke failed for thread %s",
                run_req.assistant_id,
                thread_id,
            )
            _release_thread_run_lock(deps, thread_id, status="error")
            return _platform_error(500, "Graph execution failed")

        output = result if isinstance(result, dict) else {"result": result}
        _release_thread_run_lock(deps, thread_id, status="idle", values=output)

        run_id = str(uuid.uuid4())
        return func.HttpResponse(
            body=json.dumps(output, default=str),
            mimetype="application/json",
            status_code=200,
            headers={"Content-Location": f"/api/threads/{thread_id}/runs/{run_id}"},
        )

    @app.function_name(name="aflg_platform_runs_stream")
    @app.route(
        route="threads/{thread_id}/runs/stream",
        methods=["POST"],
        auth_level=auth,
    )
    def runs_stream(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
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

        try:
            locked = deps.thread_store.try_acquire_run_lock(
                thread_id,
                assistant_id=run_req.assistant_id,
            )
        except KeyError:
            return _platform_error(404, f"Thread {thread_id!r} not found")
        except ValueError as exc:
            return _platform_error(409, str(exc))
        if locked is None:
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"Concurrent runs are not supported (multitask_strategy=reject).",
            )

        config = _build_threaded_config(run_req, thread_id)

        run_id = str(uuid.uuid4())

        graph_input = run_req.input or {}
        chunks: list[str] = []
        buffered_bytes = 0
        max_bytes = deps.max_stream_response_bytes

        meta_chunk = format_metadata_event(run_id)
        chunks.append(meta_chunk)
        buffered_bytes += len(meta_chunk.encode())

        if _check_stream_overflow(chunks, buffered_bytes, 0, max_bytes):
            _release_thread_run_lock(deps, thread_id, status="error")
            return _build_sse_response(
                chunks,
                content_location=f"/api/threads/{thread_id}/runs/{run_id}",
            )
        try:
            for event in reg.graph.stream(
                graph_input,
                config=config,
                stream_mode=stream_mode,
            ):
                chunk = format_data_event(stream_mode, event)
                chunk_bytes = len(chunk.encode())
                if _check_stream_overflow(chunks, buffered_bytes, chunk_bytes, max_bytes):
                    _release_thread_run_lock(deps, thread_id, status="error")
                    return _build_sse_response(
                        chunks,
                        content_location=f"/api/threads/{thread_id}/runs/{run_id}",
                    )
                chunks.append(chunk)
                buffered_bytes += chunk_bytes
        except Exception:
            logger.exception(
                "Graph %s stream failed for thread %s",
                run_req.assistant_id,
                thread_id,
            )
            _release_thread_run_lock(deps, thread_id, status="error")
            chunks.append(format_error_event("stream processing failed"))
            chunks.append(format_end_event())
            return _build_sse_response(
                chunks,
                content_location=f"/api/threads/{thread_id}/runs/{run_id}",
            )

        chunks.append(format_end_event())

        _release_thread_run_lock(deps, thread_id, status="idle")

        return _build_sse_response(
            chunks,
            content_location=f"/api/threads/{thread_id}/runs/{run_id}",
        )

    @app.function_name(name="aflg_platform_runs_wait_threadless")
    @app.route(route="runs/wait", methods=["POST"], auth_level=auth)
    def runs_wait_threadless(req: func.HttpRequest) -> func.HttpResponse:
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

        graph_input = run_req.input or {}
        try:
            result = exec_graph.invoke(graph_input, config=config)
        except Exception:
            logger.exception(
                "Graph %s invoke failed (threadless)",
                run_req.assistant_id,
            )
            return _platform_error(500, "Graph execution failed")

        output = result if isinstance(result, dict) else {"result": result}

        run_id = str(uuid.uuid4())
        return func.HttpResponse(
            body=json.dumps(output, default=str),
            mimetype="application/json",
            status_code=200,
            headers={"Content-Location": f"/api/runs/{run_id}"},
        )

    @app.function_name(name="aflg_platform_runs_stream_threadless")
    @app.route(route="runs/stream", methods=["POST"], auth_level=auth)
    def runs_stream_threadless(req: func.HttpRequest) -> func.HttpResponse:
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

        run_id = str(uuid.uuid4())

        graph_input = run_req.input or {}
        chunks: list[str] = []
        buffered_bytes = 0
        max_bytes = deps.max_stream_response_bytes

        meta_chunk = format_metadata_event(run_id)
        chunks.append(meta_chunk)
        buffered_bytes += len(meta_chunk.encode())

        if _check_stream_overflow(chunks, buffered_bytes, 0, max_bytes):
            return _build_sse_response(
                chunks,
                content_location=f"/api/runs/{run_id}",
            )
        try:
            for event in exec_graph.stream(
                graph_input,
                config=config,
                stream_mode=stream_mode,
            ):
                chunk = format_data_event(stream_mode, event)
                chunk_bytes = len(chunk.encode())
                if _check_stream_overflow(chunks, buffered_bytes, chunk_bytes, max_bytes):
                    return _build_sse_response(
                        chunks,
                        content_location=f"/api/runs/{run_id}",
                    )
                chunks.append(chunk)
                buffered_bytes += chunk_bytes
        except Exception:
            logger.exception(
                "Graph %s stream failed (threadless)",
                run_req.assistant_id,
            )
            chunks.append(format_error_event("stream processing failed"))
            chunks.append(format_end_event())
            return _build_sse_response(
                chunks,
                content_location=f"/api/runs/{run_id}",
            )

        chunks.append(format_end_event())

        return _build_sse_response(
            chunks,
            content_location=f"/api/runs/{run_id}",
        )
