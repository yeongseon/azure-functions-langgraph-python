from __future__ import annotations

import json
from typing import Any
import uuid

import azure.functions as func

from azure_functions_langgraph._validation import (
    validate_body_size,
    validate_graph_name,
    validate_input_structure,
    validate_thread_id,
)
from azure_functions_langgraph.platform._common import (
    PlatformRouteDeps,
    _get_threadless_graph,
    _platform_error,
    _preflight_run_create,
    logger,
)
from azure_functions_langgraph.platform._sse import (
    format_data_event,
    format_end_event,
    format_error_event,
    format_metadata_event,
)
from azure_functions_langgraph.platform.contracts import RunCreate, ThreadStatus
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


def register_run_routes(
    app: func.FunctionApp,
    deps: PlatformRouteDeps,
) -> None:
    auth = deps.auth_level

    @app.function_name(name="aflg_platform_runs_wait")
    @app.route(
        route="threads/{thread_id}/runs/wait", methods=["POST"], auth_level=auth
    )
    def runs_wait(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        raw_body = req.get_body()
        size_err = validate_body_size(raw_body, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)

        try:
            body = req.get_json()
        except ValueError:
            return _platform_error(400, "Invalid JSON body")

        try:
            run_req = RunCreate.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        name_err = validate_graph_name(run_req.assistant_id)
        if name_err:
            return _platform_error(400, name_err)
        preflight = _preflight_run_create(run_req)
        if preflight is not None:
            return preflight

        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        reg = deps.registrations.get(run_req.assistant_id)
        if reg is None:
            return _platform_error(
                404, f"Assistant {run_req.assistant_id!r} not found"
            )

        if run_req.input:
            input_err = validate_input_structure(
                run_req.input, max_depth=deps.max_input_depth, max_nodes=deps.max_input_nodes,
            )
            if input_err:
                return _platform_error(400, input_err)
        if run_req.config:
            config_err = validate_input_structure(
                run_req.config, max_depth=deps.max_input_depth, max_nodes=deps.max_input_nodes,
            )
            if config_err:
                return _platform_error(400, config_err)

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

        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        if run_req.config:
            user_config = dict(run_req.config)
            user_configurable = user_config.pop("configurable", {})
            config["configurable"].update(user_configurable)
            config["configurable"]["thread_id"] = thread_id
            config.update(user_config)

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

        raw_body = req.get_body()
        size_err = validate_body_size(raw_body, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)

        try:
            body = req.get_json()
        except ValueError:
            return _platform_error(400, "Invalid JSON body")

        try:
            run_req = RunCreate.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        name_err = validate_graph_name(run_req.assistant_id)
        if name_err:
            return _platform_error(400, name_err)
        preflight = _preflight_run_create(run_req)
        if preflight is not None:
            return preflight

        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        raw_mode = run_req.stream_mode
        if isinstance(raw_mode, list):
            if len(raw_mode) == 1:
                stream_mode = raw_mode[0]
            elif len(raw_mode) == 0:
                stream_mode = "values"
            else:
                return _platform_error(
                    501,
                    "Multi-stream-mode is not supported in this release. "
                    "Provide a single stream_mode string or a one-element list.",
                )
        else:
            stream_mode = raw_mode

        reg = deps.registrations.get(run_req.assistant_id)
        if reg is None:
            return _platform_error(
                404, f"Assistant {run_req.assistant_id!r} not found"
            )

        if run_req.input:
            input_err = validate_input_structure(
                run_req.input, max_depth=deps.max_input_depth, max_nodes=deps.max_input_nodes,
            )
            if input_err:
                return _platform_error(400, input_err)
        if run_req.config:
            config_err = validate_input_structure(
                run_req.config, max_depth=deps.max_input_depth, max_nodes=deps.max_input_nodes,
            )
            if config_err:
                return _platform_error(400, config_err)

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

        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        if run_req.config:
            user_config = dict(run_req.config)
            user_configurable = user_config.pop("configurable", {})
            config["configurable"].update(user_configurable)
            config["configurable"]["thread_id"] = thread_id
            config.update(user_config)

        run_id = str(uuid.uuid4())

        graph_input = run_req.input or {}
        chunks: list[str] = []
        buffered_bytes = 0
        max_bytes = deps.max_stream_response_bytes

        meta_chunk = format_metadata_event(run_id)
        chunks.append(meta_chunk)
        buffered_bytes += len(meta_chunk.encode())

        if buffered_bytes > max_bytes:
            chunks.append(
                format_error_event(
                    f"stream response exceeded max buffered size "
                    f"({max_bytes} bytes)"
                )
            )
            chunks.append(format_end_event())
            _release_thread_run_lock(deps, thread_id, status="error")
            return func.HttpResponse(
                body="".join(chunks),
                mimetype="text/event-stream",
                status_code=200,
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Content-Location": f"/api/threads/{thread_id}/runs/{run_id}",
                },
            )
        try:
            for event in reg.graph.stream(
                graph_input,
                config=config,
                stream_mode=stream_mode,
            ):
                chunk = format_data_event(stream_mode, event)
                chunk_bytes = len(chunk.encode())
                if buffered_bytes + chunk_bytes > max_bytes:
                    err_chunk = format_error_event(
                        f"stream response exceeded max buffered size "
                        f"({max_bytes} bytes)"
                    )
                    chunks.append(err_chunk)
                    chunks.append(format_end_event())
                    _release_thread_run_lock(deps, thread_id, status="error")
                    return func.HttpResponse(
                        body="".join(chunks),
                        mimetype="text/event-stream",
                        status_code=200,
                        headers={
                            "Cache-Control": "no-cache",
                            "X-Accel-Buffering": "no",
                            "Content-Location": f"/api/threads/{thread_id}/runs/{run_id}",
                        },
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
            return func.HttpResponse(
                body="".join(chunks),
                mimetype="text/event-stream",
                status_code=200,
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Content-Location": f"/api/threads/{thread_id}/runs/{run_id}",
                },
            )

        chunks.append(format_end_event())

        _release_thread_run_lock(deps, thread_id, status="idle")

        return func.HttpResponse(
            body="".join(chunks),
            mimetype="text/event-stream",
            status_code=200,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Content-Location": f"/api/threads/{thread_id}/runs/{run_id}",
            },
        )

    @app.function_name(name="aflg_platform_runs_wait_threadless")
    @app.route(route="runs/wait", methods=["POST"], auth_level=auth)
    def runs_wait_threadless(req: func.HttpRequest) -> func.HttpResponse:
        raw_body = req.get_body()
        size_err = validate_body_size(raw_body, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)

        try:
            body = req.get_json()
        except ValueError:
            return _platform_error(400, "Invalid JSON body")

        if not isinstance(body, dict):
            return _platform_error(400, "Request body must be a JSON object")

        try:
            run_req = RunCreate.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        name_err = validate_graph_name(run_req.assistant_id)
        if name_err:
            return _platform_error(400, name_err)
        preflight = _preflight_run_create(run_req)
        if preflight is not None:
            return preflight

        reg = deps.registrations.get(run_req.assistant_id)
        if reg is None:
            return _platform_error(
                404, f"Assistant {run_req.assistant_id!r} not found"
            )

        if run_req.input:
            input_err = validate_input_structure(
                run_req.input, max_depth=deps.max_input_depth, max_nodes=deps.max_input_nodes,
            )
            if input_err:
                return _platform_error(400, input_err)
        if run_req.config:
            config_err = validate_input_structure(
                run_req.config, max_depth=deps.max_input_depth, max_nodes=deps.max_input_nodes,
            )
            if config_err:
                return _platform_error(400, config_err)

        exec_graph = _get_threadless_graph(reg.graph)
        if exec_graph is None:
            return _platform_error(
                501,
                f"Graph {run_req.assistant_id!r} has a checkpointer that cannot "
                f"be disabled for threadless execution.",
            )

        config: dict[str, Any] = {}
        if run_req.config:
            user_config = dict(run_req.config)
            user_configurable = user_config.pop("configurable", {})
            config["configurable"] = user_configurable
            config.update(user_config)

        if config.get("configurable", {}).get("thread_id") is not None:
            return _platform_error(
                422, "thread_id is not allowed on threadless runs"
            )

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
        raw_body = req.get_body()
        size_err = validate_body_size(raw_body, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)

        try:
            body = req.get_json()
        except ValueError:
            return _platform_error(400, "Invalid JSON body")

        if not isinstance(body, dict):
            return _platform_error(400, "Request body must be a JSON object")

        try:
            run_req = RunCreate.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        name_err = validate_graph_name(run_req.assistant_id)
        if name_err:
            return _platform_error(400, name_err)
        preflight = _preflight_run_create(run_req)
        if preflight is not None:
            return preflight

        raw_mode = run_req.stream_mode
        if isinstance(raw_mode, list):
            if len(raw_mode) == 1:
                stream_mode = raw_mode[0]
            elif len(raw_mode) == 0:
                stream_mode = "values"
            else:
                return _platform_error(
                    501,
                    "Multi-stream-mode is not supported in this release. "
                    "Provide a single stream_mode string or a one-element list.",
                )
        else:
            stream_mode = raw_mode

        reg = deps.registrations.get(run_req.assistant_id)
        if reg is None:
            return _platform_error(
                404, f"Assistant {run_req.assistant_id!r} not found"
            )

        if run_req.input:
            input_err = validate_input_structure(
                run_req.input, max_depth=deps.max_input_depth, max_nodes=deps.max_input_nodes,
            )
            if input_err:
                return _platform_error(400, input_err)
        if run_req.config:
            config_err = validate_input_structure(
                run_req.config, max_depth=deps.max_input_depth, max_nodes=deps.max_input_nodes,
            )
            if config_err:
                return _platform_error(400, config_err)

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

        config: dict[str, Any] = {}
        if run_req.config:
            user_config = dict(run_req.config)
            user_configurable = user_config.pop("configurable", {})
            config["configurable"] = user_configurable
            config.update(user_config)

        if config.get("configurable", {}).get("thread_id") is not None:
            return _platform_error(
                422, "thread_id is not allowed on threadless runs"
            )

        run_id = str(uuid.uuid4())

        graph_input = run_req.input or {}
        chunks: list[str] = []
        buffered_bytes = 0
        max_bytes = deps.max_stream_response_bytes

        meta_chunk = format_metadata_event(run_id)
        chunks.append(meta_chunk)
        buffered_bytes += len(meta_chunk.encode())

        if buffered_bytes > max_bytes:
            chunks.append(
                format_error_event(
                    f"stream response exceeded max buffered size "
                    f"({max_bytes} bytes)"
                )
            )
            chunks.append(format_end_event())
            return func.HttpResponse(
                body="".join(chunks),
                mimetype="text/event-stream",
                status_code=200,
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Content-Location": f"/api/runs/{run_id}",
                },
            )
        try:
            for event in exec_graph.stream(
                graph_input,
                config=config,
                stream_mode=stream_mode,
            ):
                chunk = format_data_event(stream_mode, event)
                chunk_bytes = len(chunk.encode())
                if buffered_bytes + chunk_bytes > max_bytes:
                    err_chunk = format_error_event(
                        f"stream response exceeded max buffered size "
                        f"({max_bytes} bytes)"
                    )
                    chunks.append(err_chunk)
                    chunks.append(format_end_event())
                    return func.HttpResponse(
                        body="".join(chunks),
                        mimetype="text/event-stream",
                        status_code=200,
                        headers={
                            "Cache-Control": "no-cache",
                            "X-Accel-Buffering": "no",
                            "Content-Location": f"/api/runs/{run_id}",
                        },
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
            return func.HttpResponse(
                body="".join(chunks),
                mimetype="text/event-stream",
                status_code=200,
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Content-Location": f"/api/runs/{run_id}",
                },
            )

        chunks.append(format_end_event())

        return func.HttpResponse(
            body="".join(chunks),
            mimetype="text/event-stream",
            status_code=200,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Content-Location": f"/api/runs/{run_id}",
            },
        )
