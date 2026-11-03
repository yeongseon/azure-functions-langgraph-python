from __future__ import annotations

import json
from typing import Any

import azure.functions as func
from pydantic import ValidationError

from azure_functions_langgraph._validation import (
    sanitized_validation_errors,
    validate_input_structure,
    validate_thread_id,
)
from azure_functions_langgraph.authorization import (
    AuthorizationAction,
    AuthorizationContext,
    authorization_status,
)
from azure_functions_langgraph.platform._common import (
    _UNSUPPORTED_THREAD_FILTER_FIELDS,
    PlatformRouteDeps,
    _build_checkpoint_config,
    _check_unknown_platform_fields,
    _platform_error,
    _read_json_body,
    _resolve_thread_graph,
    _result_limit_error,
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
    ThreadStatus,
    ThreadUpdate,
)
from azure_functions_langgraph.protocols import (
    StatefulGraph,
    StateHistoryGraph,
    UpdatableStateGraph,
)


def _release_thread_state_lock(
    deps: PlatformRouteDeps,
    thread_id: str,
    graph_name: str,
    lock_token: str,
    *,
    status: ThreadStatus,
    store_owner_token: str | None = None,
) -> Exception | None:
    """Release both the distributed thread lock and the store-backed run lock
    held while a state update is in flight, returning any cleanup failure.

    Mirrors ``_release_thread_run_lock`` in ``_runs.py`` (no renewal thread is
    started here since ``update_state`` is a single synchronous call expected
    to complete well within the store's lock-lease window).
    """
    failure: Exception | None = None
    try:
        released = deps.thread_store.release_run_lock(
            thread_id,
            status=status,
            owner_token=store_owner_token or lock_token,
        )
        if released.status == "busy" or released.lock_token is not None:
            failure = RuntimeError("State-update store ownership was not released")
            logger.warning(
                "State-update lock release did not clear ownership for thread %s", thread_id
            )
    except KeyError as exc:
        failure = exc
        logger.warning(
            "Thread %s disappeared before state-update lock release to %s",
            thread_id,
            status,
        )
    except Exception as exc:
        failure = exc
        logger.exception(
            "Unexpected error releasing state-update lock for thread %s (target status: %s)",
            thread_id,
            status,
        )
    finally:
        try:
            deps.thread_lock.release(graph_name, thread_id, lock_token)
        except Exception as exc:
            failure = failure or exc
            logger.exception("Unexpected error releasing thread lock for thread %s", thread_id)
    return failure


def register_thread_routes(
    app: func.FunctionApp,
    deps: PlatformRouteDeps,
) -> None:
    auth = deps.auth_level

    def denied(
        req: func.HttpRequest,
        action: AuthorizationAction,
        thread_id: str | None = None,
    ) -> func.HttpResponse | None:
        status = authorization_status(
            deps.authorize,
            AuthorizationContext(action, None, thread_id, req),
        )
        return _platform_error(status, "Access denied") if status is not None else None

    @app.function_name(name="aflg_platform_threads_create")
    @app.route(route="threads", methods=["POST"], auth_level=auth)
    def threads_create(req: func.HttpRequest) -> func.HttpResponse:
        if response := denied(req, AuthorizationAction.THREAD_CREATE):
            return response
        body = _read_json_body(req, deps, require_dict=False, allow_empty=True)
        if isinstance(body, func.HttpResponse):
            return body

        unknown_err = _check_unknown_platform_fields(ThreadCreate, body)
        if unknown_err is not None:
            return unknown_err
        try:
            create_req = ThreadCreate.model_validate(body)
        except ValidationError as exc:
            return _platform_error(422, sanitized_validation_errors(exc))
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
        if response := denied(req, AuthorizationAction.THREAD_READ, thread_id):
            return response
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
        if response := denied(req, AuthorizationAction.THREAD_UPDATE, thread_id):
            return response
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        body = _read_json_body(req, deps, require_dict=False, allow_empty=True)
        if isinstance(body, func.HttpResponse):
            return body

        unknown_err = _check_unknown_platform_fields(ThreadUpdate, body)
        if unknown_err is not None:
            return unknown_err
        try:
            update_req = ThreadUpdate.model_validate(body)
        except ValidationError as exc:
            return _platform_error(422, sanitized_validation_errors(exc))
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
        if response := denied(req, AuthorizationAction.THREAD_DELETE, thread_id):
            return response
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
        if response := denied(req, AuthorizationAction.THREAD_SEARCH):
            return response
        body = _read_json_body(req, deps, require_dict=True, allow_empty=True)
        if isinstance(body, func.HttpResponse):
            return body

        unsupported = set(body.keys()) & _UNSUPPORTED_THREAD_FILTER_FIELDS
        if unsupported:
            return _platform_error(
                501,
                f"Unsupported filter(s): {', '.join(sorted(unsupported))}",
            )

        unknown_err = _check_unknown_platform_fields(ThreadSearch, body)
        if unknown_err is not None:
            return unknown_err
        try:
            search_req = ThreadSearch.model_validate(body)
        except ValidationError as exc:
            return _platform_error(422, sanitized_validation_errors(exc))
        limit_error = _result_limit_error(search_req.limit, deps.max_platform_result_limit)
        if limit_error is not None:
            return limit_error

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
        if response := denied(req, AuthorizationAction.THREAD_SEARCH):
            return response
        body = _read_json_body(req, deps, require_dict=True, allow_empty=True)
        if isinstance(body, func.HttpResponse):
            return body

        unsupported = set(body.keys()) & _UNSUPPORTED_THREAD_FILTER_FIELDS
        if unsupported:
            return _platform_error(
                501,
                f"Unsupported filter(s): {', '.join(sorted(unsupported))}",
            )

        unknown_err = _check_unknown_platform_fields(ThreadCount, body)
        if unknown_err is not None:
            return unknown_err
        try:
            count_req = ThreadCount.model_validate(body)
        except ValidationError as exc:
            return _platform_error(422, sanitized_validation_errors(exc))

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
        if response := denied(req, AuthorizationAction.STATE_READ, thread_id):
            return response
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)
        graph = _resolve_thread_graph(
            deps, thread_id, protocol=StatefulGraph, capability="state retrieval"
        )
        if isinstance(graph, func.HttpResponse):
            return graph

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
        if response := denied(req, AuthorizationAction.STATE_UPDATE, thread_id):
            return response
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        body = _read_json_body(req, deps, require_dict=True, allow_empty=False)
        if isinstance(body, func.HttpResponse):
            return body

        unknown_err = _check_unknown_platform_fields(ThreadStateUpdate, body)
        if unknown_err is not None:
            return unknown_err
        try:
            update_req = ThreadStateUpdate.model_validate(body)
        except ValidationError as exc:
            return _platform_error(422, sanitized_validation_errors(exc))

        # Validate input structure depth/size on values payload
        values_to_check = (
            update_req.values
            if isinstance(update_req.values, dict)
            else {"items": update_req.values}
        )
        val_err = validate_input_structure(
            values_to_check,
            max_depth=deps.max_input_depth,
            max_nodes=deps.max_input_nodes,
        )
        if val_err:
            return _platform_error(400, val_err)

        graph = _resolve_thread_graph(
            deps, thread_id, protocol=UpdatableStateGraph, capability="state updates"
        )
        if isinstance(graph, func.HttpResponse):
            return graph

        config = _build_checkpoint_config(
            thread_id,
            update_req.checkpoint,
            fallback_checkpoint_id=update_req.checkpoint_id,
        )
        if isinstance(config, func.HttpResponse):
            return config

        # _resolve_thread_graph already confirmed the thread exists and is
        # bound to an assistant; re-fetch it to learn the graph name (for the
        # distributed thread lock) and the thread's current terminal status,
        # so the update can be coordinated with active native/Platform runs
        # using the same single-writer exclusion, and so the prior status
        # (idle/interrupted/error) is preserved across the update instead of
        # being unconditionally forced back to "idle".
        thread = deps.thread_store.get(thread_id)
        if thread is None or thread.assistant_id is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")
        graph_name = thread.assistant_id
        prior_status = thread.status

        lock_token = deps.thread_lock.acquire(graph_name, thread_id)
        if lock_token is None:
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"State updates are not supported while a run is active.",
            )
        try:
            locked = deps.thread_store.try_acquire_run_lock(thread_id, assistant_id=graph_name)
        except KeyError:
            deps.thread_lock.release(graph_name, thread_id, lock_token)
            return _platform_error(404, f"Thread {thread_id!r} not found")
        except ValueError as exc:
            deps.thread_lock.release(graph_name, thread_id, lock_token)
            return _platform_error(409, str(exc))
        if locked is None:
            deps.thread_lock.release(graph_name, thread_id, lock_token)
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"State updates are not supported while a run is active.",
            )
        store_lock_token = locked.lock_token or lock_token

        try:
            result = graph.update_state(config, update_req.values, as_node=update_req.as_node)
        except (KeyError, ValueError) as exc:
            _release_thread_state_lock(
                deps,
                thread_id,
                graph_name,
                lock_token,
                status="error",
                store_owner_token=store_lock_token,
            )
            return _platform_error(404, f"Cannot update state for thread {thread_id!r}: {exc}")
        except Exception:
            logger.exception("update_state failed for thread %s", thread_id)
            _release_thread_state_lock(
                deps,
                thread_id,
                graph_name,
                lock_token,
                status="error",
                store_owner_token=store_lock_token,
            )
            return _platform_error(500, "Internal error updating thread state")

        release_failure = _release_thread_state_lock(
            deps,
            thread_id,
            graph_name,
            lock_token,
            status=prior_status,
            store_owner_token=store_lock_token,
        )
        if release_failure is not None:
            return _platform_error(500, "Internal error releasing thread state lock")

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
        if response := denied(req, AuthorizationAction.HISTORY_READ, thread_id):
            return response
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        body = _read_json_body(req, deps, require_dict=True, allow_empty=True)
        if isinstance(body, func.HttpResponse):
            return body

        unknown_err = _check_unknown_platform_fields(ThreadHistoryRequest, body)
        if unknown_err is not None:
            return unknown_err
        try:
            hist_req = ThreadHistoryRequest.model_validate(body)
        except ValidationError as exc:
            return _platform_error(422, sanitized_validation_errors(exc))
        limit_error = _result_limit_error(hist_req.limit, deps.max_platform_result_limit)
        if limit_error is not None:
            return limit_error

        # Validate metadata structure if provided
        if hist_req.metadata is not None:
            val_err = validate_input_structure(
                hist_req.metadata,
                max_depth=deps.max_input_depth,
                max_nodes=deps.max_input_nodes,
            )
            if val_err:
                return _platform_error(400, val_err)

        graph = _resolve_thread_graph(
            deps, thread_id, protocol=StateHistoryGraph, capability="state history"
        )
        if isinstance(graph, func.HttpResponse):
            return graph

        config = _build_checkpoint_config(thread_id, hist_req.checkpoint)
        if isinstance(config, func.HttpResponse):
            return config

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
