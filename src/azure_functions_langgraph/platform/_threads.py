from __future__ import annotations

import json
from typing import Any

import azure.functions as func

from azure_functions_langgraph._validation import validate_body_size, validate_thread_id
from azure_functions_langgraph.platform._common import (
    _UNSUPPORTED_THREAD_FILTER_FIELDS,
    PlatformRouteDeps,
    _platform_error,
    _snapshot_to_thread_state,
    logger,
)
from azure_functions_langgraph.platform.contracts import (
    Checkpoint,
    ThreadCount,
    ThreadCreate,
    ThreadHistoryRequest,
    ThreadSearch,
    ThreadState,
    ThreadStateUpdate,
    ThreadUpdate,
)
from azure_functions_langgraph.protocols import (
    StatefulGraph,
    StateHistoryGraph,
    UpdatableStateGraph,
)


def register_thread_routes(
    app: func.FunctionApp,
    deps: PlatformRouteDeps,
) -> None:
    auth = deps.auth_level

    @app.function_name(name="aflg_platform_threads_create")
    @app.route(route="threads", methods=["POST"], auth_level=auth)
    def threads_create(req: func.HttpRequest) -> func.HttpResponse:
        raw = req.get_body()
        size_err = validate_body_size(raw, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)
        if raw and raw.strip() != b"":
            try:
                body: dict[str, Any] = req.get_json()
            except ValueError:
                return _platform_error(400, "Invalid JSON body")
        else:
            body = {}

        try:
            create_req = ThreadCreate.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        thread = deps.thread_store.create(metadata=create_req.metadata)
        return func.HttpResponse(
            body=json.dumps(thread.model_dump(mode="json"), default=str),
            mimetype="application/json",
            status_code=200,
        )

    @app.function_name(name="aflg_platform_threads_get")
    @app.route(route="threads/{thread_id}", methods=["GET"], auth_level=auth)
    def threads_get(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)
        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")
        return func.HttpResponse(
            body=json.dumps(thread.model_dump(mode="json"), default=str),
            mimetype="application/json",
            status_code=200,
        )

    @app.function_name(name="aflg_platform_threads_update")
    @app.route(route="threads/{thread_id}", methods=["PATCH"], auth_level=auth)
    def threads_update(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        raw = req.get_body()
        size_err = validate_body_size(raw, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)
        if raw and raw.strip() != b"":
            try:
                body: dict[str, Any] = req.get_json()
            except ValueError:
                return _platform_error(400, "Invalid JSON body")
        else:
            body = {}

        try:
            update_req = ThreadUpdate.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        if update_req.metadata is not None:
            merged = {**(thread.metadata or {}), **update_req.metadata}
            try:
                updated = deps.thread_store.update(thread_id, metadata=merged)
            except KeyError:
                return _platform_error(404, f"Thread {thread_id!r} not found")
        else:
            updated = thread

        return func.HttpResponse(
            body=json.dumps(updated.model_dump(mode="json"), default=str),
            mimetype="application/json",
            status_code=200,
        )

    @app.function_name(name="aflg_platform_threads_delete")
    @app.route(route="threads/{thread_id}", methods=["DELETE"], auth_level=auth)
    def threads_delete(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        try:
            deps.thread_store.delete(thread_id)
        except KeyError:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        return func.HttpResponse(
            body=b"",
            status_code=204,
        )

    @app.function_name(name="aflg_platform_threads_search")
    @app.route(route="threads/search", methods=["POST"], auth_level=auth)
    def threads_search(req: func.HttpRequest) -> func.HttpResponse:
        raw = req.get_body()
        size_err = validate_body_size(raw, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)
        if raw and raw.strip() != b"":
            try:
                body = req.get_json()
            except ValueError:
                return _platform_error(400, "Invalid JSON body")
        else:
            body = {}

        if not isinstance(body, dict):
            return _platform_error(400, "Request body must be a JSON object")

        unsupported = set(body.keys()) & _UNSUPPORTED_THREAD_FILTER_FIELDS
        if unsupported:
            return _platform_error(
                501,
                f"Unsupported filter(s): {', '.join(sorted(unsupported))}",
            )

        try:
            search_req = ThreadSearch.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        results = deps.thread_store.search(
            metadata=search_req.metadata,
            status=search_req.status,
            limit=search_req.limit,
            offset=search_req.offset,
        )
        return func.HttpResponse(
            body=json.dumps([t.model_dump(mode="json") for t in results], default=str),
            mimetype="application/json",
            status_code=200,
        )

    @app.function_name(name="aflg_platform_threads_count")
    @app.route(route="threads/count", methods=["POST"], auth_level=auth)
    def threads_count(req: func.HttpRequest) -> func.HttpResponse:
        raw = req.get_body()
        size_err = validate_body_size(raw, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)
        if raw and raw.strip() != b"":
            try:
                body = req.get_json()
            except ValueError:
                return _platform_error(400, "Invalid JSON body")
        else:
            body = {}

        if not isinstance(body, dict):
            return _platform_error(400, "Request body must be a JSON object")

        unsupported = set(body.keys()) & _UNSUPPORTED_THREAD_FILTER_FIELDS
        if unsupported:
            return _platform_error(
                501,
                f"Unsupported filter(s): {', '.join(sorted(unsupported))}",
            )

        try:
            count_req = ThreadCount.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        total = deps.thread_store.count(
            metadata=count_req.metadata,
            status=count_req.status,
        )
        return func.HttpResponse(
            body=json.dumps(total),
            mimetype="application/json",
            status_code=200,
        )

    @app.function_name(name="aflg_platform_threads_state_get")
    @app.route(route="threads/{thread_id}/state", methods=["GET"], auth_level=auth)
    def threads_state_get(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)
        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        if thread.assistant_id is None:
            return _platform_error(
                409,
                f"Thread {thread_id!r} is not bound to any assistant. "
                f"Run a graph on this thread first.",
            )

        reg = deps.registrations.get(thread.assistant_id)
        if reg is None:
            return _platform_error(
                404,
                f"Assistant {thread.assistant_id!r} not found for thread {thread_id!r}",
            )

        graph = reg.graph
        if not isinstance(graph, StatefulGraph):
            return _platform_error(
                409,
                f"Graph {thread.assistant_id!r} does not support state retrieval",
            )

        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        try:
            snapshot = graph.get_state(config)
        except (KeyError, ValueError):
            state = ThreadState(
                values={},
                next=[],
                checkpoint=Checkpoint(thread_id=thread_id, checkpoint_ns="", checkpoint_id=None),
                metadata=None,
                created_at=None,
                parent_checkpoint=None,
                tasks=[],
                interrupts=[],
            )
            return func.HttpResponse(
                body=json.dumps(state.model_dump(mode="json"), default=str),
                mimetype="application/json",
                status_code=200,
            )
        except Exception:
            logger.exception("get_state failed for thread %s", thread_id)
            return _platform_error(500, "Internal error retrieving thread state")

        state = _snapshot_to_thread_state(snapshot, thread_id)
        return func.HttpResponse(
            body=json.dumps(state.model_dump(mode="json"), default=str),
            mimetype="application/json",
            status_code=200,
        )

    @app.function_name(name="aflg_platform_threads_state_update")
    @app.route(route="threads/{thread_id}/state", methods=["POST"], auth_level=auth)
    def threads_state_update(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        raw = req.get_body()
        size_err = validate_body_size(raw, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)
        try:
            body: dict[str, Any] = req.get_json()
        except ValueError:
            return _platform_error(400, "Invalid JSON body")

        if not isinstance(body, dict):
            return _platform_error(400, "Request body must be a JSON object")

        try:
            update_req = ThreadStateUpdate.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        if thread.assistant_id is None:
            return _platform_error(
                409,
                f"Thread {thread_id!r} is not bound to any assistant. "
                f"Run a graph on this thread first.",
            )

        reg = deps.registrations.get(thread.assistant_id)
        if reg is None:
            return _platform_error(
                404,
                f"Assistant {thread.assistant_id!r} not found for thread {thread_id!r}",
            )

        graph = reg.graph
        if not isinstance(graph, UpdatableStateGraph):
            return _platform_error(
                409,
                f"Graph {thread.assistant_id!r} does not support state updates",
            )

        configurable: dict[str, Any] = {"thread_id": thread_id}
        if update_req.checkpoint is not None:
            cp_thread_id = update_req.checkpoint.get("thread_id")
            if cp_thread_id is not None and cp_thread_id != thread_id:
                return _platform_error(
                    422,
                    f"Checkpoint thread_id {cp_thread_id!r} does not match "
                    f"path thread_id {thread_id!r}",
                )
            cp_id = update_req.checkpoint.get("checkpoint_id")
            if cp_id is not None:
                configurable["checkpoint_id"] = cp_id
            cp_ns = update_req.checkpoint.get("checkpoint_ns")
            if cp_ns is not None:
                configurable["checkpoint_ns"] = cp_ns
        elif update_req.checkpoint_id is not None:
            configurable["checkpoint_id"] = update_req.checkpoint_id
        config: dict[str, Any] = {"configurable": configurable}

        try:
            result = graph.update_state(config, update_req.values, as_node=update_req.as_node)
        except (KeyError, ValueError) as exc:
            return _platform_error(404, f"Cannot update state for thread {thread_id!r}: {exc}")
        except Exception:
            logger.exception("update_state failed for thread %s", thread_id)
            return _platform_error(500, "Internal error updating thread state")

        result_configurable = result.get("configurable", {}) if isinstance(result, dict) else {}
        response_checkpoint = Checkpoint(
            thread_id=thread_id,
            checkpoint_ns=result_configurable.get("checkpoint_ns", ""),
            checkpoint_id=result_configurable.get("checkpoint_id"),
        )
        payload = {"checkpoint": response_checkpoint.model_dump(mode="json")}
        return func.HttpResponse(
            body=json.dumps(payload, default=str),
            mimetype="application/json",
            status_code=200,
        )

    @app.function_name(name="aflg_platform_threads_history")
    @app.route(route="threads/{thread_id}/history", methods=["POST"], auth_level=auth)
    def threads_history(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        raw = req.get_body()
        size_err = validate_body_size(raw, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(400, size_err)
        if raw and raw.strip() != b"":
            try:
                body: dict[str, Any] = req.get_json()
            except ValueError:
                return _platform_error(400, "Invalid JSON body")

            if not isinstance(body, dict):
                return _platform_error(400, "Request body must be a JSON object")
        else:
            body = {}

        try:
            hist_req = ThreadHistoryRequest.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        if thread.assistant_id is None:
            return _platform_error(
                409,
                f"Thread {thread_id!r} is not bound to any assistant. "
                f"Run a graph on this thread first.",
            )

        reg = deps.registrations.get(thread.assistant_id)
        if reg is None:
            return _platform_error(
                404,
                f"Assistant {thread.assistant_id!r} not found for thread {thread_id!r}",
            )

        graph = reg.graph
        if not isinstance(graph, StateHistoryGraph):
            return _platform_error(
                409,
                f"Graph {thread.assistant_id!r} does not support state history",
            )

        configurable: dict[str, Any] = {"thread_id": thread_id}
        if hist_req.checkpoint is not None:
            cp_thread_id = hist_req.checkpoint.get("thread_id")
            if cp_thread_id is not None and cp_thread_id != thread_id:
                return _platform_error(
                    422,
                    f"Checkpoint thread_id {cp_thread_id!r} does not match "
                    f"path thread_id {thread_id!r}",
                )
            cp_id = hist_req.checkpoint.get("checkpoint_id")
            if cp_id is not None:
                configurable["checkpoint_id"] = cp_id
            cp_ns = hist_req.checkpoint.get("checkpoint_ns")
            if cp_ns is not None:
                configurable["checkpoint_ns"] = cp_ns
        config: dict[str, Any] = {"configurable": configurable}

        try:
            history_iter = graph.get_state_history(config)

            before_id: str | None = None
            if hist_req.before is not None:
                if isinstance(hist_req.before, str):
                    before_id = hist_req.before
                elif isinstance(hist_req.before, dict):
                    before_thread_id = hist_req.before.get("thread_id")
                    if before_thread_id is not None and before_thread_id != thread_id:
                        return _platform_error(
                            422,
                            f"before checkpoint thread_id {before_thread_id!r} does not match "
                            f"path thread_id {thread_id!r}",
                        )
                    before_id = hist_req.before.get("checkpoint_id")

            results: list[ThreadState] = []
            found_before = before_id is None
            for snapshot in history_iter:
                snap_config = getattr(snapshot, "config", None) or {}
                snap_configurable = (
                    snap_config.get("configurable", {}) if isinstance(snap_config, dict) else {}
                )
                snap_cp_id = snap_configurable.get("checkpoint_id")

                if not found_before:
                    if snap_cp_id == before_id:
                        found_before = True
                    continue

                if hist_req.metadata is not None:
                    snap_metadata = getattr(snapshot, "metadata", None) or {}
                    if not all(snap_metadata.get(k) == v for k, v in hist_req.metadata.items()):
                        continue

                results.append(_snapshot_to_thread_state(snapshot, thread_id))
                if len(results) >= hist_req.limit:
                    break
        except (KeyError, ValueError):
            return func.HttpResponse(
                body=json.dumps([]),
                mimetype="application/json",
                status_code=200,
            )
        except Exception:
            logger.exception("get_state_history failed for thread %s", thread_id)
            return _platform_error(500, "Internal error retrieving thread history")

        return func.HttpResponse(
            body=json.dumps([s.model_dump(mode="json") for s in results], default=str),
            mimetype="application/json",
            status_code=200,
        )
