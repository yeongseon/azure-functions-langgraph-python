"""Platform API–compatible route registration.

Registers Azure Functions HTTP routes that mirror the LangGraph Platform
REST API (``langgraph-sdk ~0.1``).  When enabled via
``LangGraphApp(platform_compat=True)``, the official ``langgraph-sdk``
Python client can communicate with Azure Functions–hosted graphs.

Routes registered (under ``/api/`` prefix, managed by Azure Functions):

* ``POST /api/assistants/search``
* ``POST /api/assistants/count``
* ``GET  /api/assistants/{assistant_id}``
* ``POST /api/threads``
* ``GET  /api/threads/{thread_id}``
* ``PATCH /api/threads/{thread_id}``
* ``DELETE /api/threads/{thread_id}``
* ``POST /api/threads/search``
* ``POST /api/threads/count``
* ``GET  /api/threads/{thread_id}/state``
* ``POST /api/threads/{thread_id}/runs/wait``
* ``POST /api/threads/{thread_id}/runs/stream``
* ``POST /api/runs/wait``  *(threadless)*
* ``POST /api/runs/stream``  *(threadless)*
.. versionadded:: 0.3.0
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any
import uuid

import azure.functions as func

from azure_functions_langgraph._validation import (
    validate_body_size,
    validate_graph_name,
    validate_input_structure,
    validate_thread_id,
)
from azure_functions_langgraph.platform._sse import (
    format_data_event,
    format_end_event,
    format_error_event,
    format_metadata_event,
)
from azure_functions_langgraph.platform.contracts import (
    Assistant,
    AssistantCount,
    AssistantSearch,
    RunCreate,
    ThreadCount,
    ThreadCreate,
    ThreadSearch,
    ThreadState,
    ThreadUpdate,
)
from azure_functions_langgraph.platform.stores import ThreadStore
from azure_functions_langgraph.protocols import StatefulGraph, StreamableGraph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error helper — Platform-specific JSON error responses
# ---------------------------------------------------------------------------


def _platform_error(status_code: int, detail: str) -> func.HttpResponse:
    """Return a JSON error matching LangGraph Platform conventions."""
    body = json.dumps({"detail": detail})
    return func.HttpResponse(
        body=body,
        mimetype="application/json",
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Unsupported-feature preflight check
# ---------------------------------------------------------------------------

_UNSUPPORTED_FIELDS: dict[str, str] = {
    "interrupt_before": "Interrupt-before is not supported in this release.",
    "interrupt_after": "Interrupt-after is not supported in this release.",
    "webhook": "Webhook callbacks are not supported in this release.",
    "on_completion": "on_completion callbacks are not supported in this release.",
    "after_seconds": "Delayed runs are not supported in this release.",
    "if_not_exists": "if_not_exists is not supported in this release.",
    "checkpoint_id": "Checkpoint resumption is not supported in this release.",
    "command": "Command-based resumption is not supported in this release.",
    "feedback_keys": "Feedback keys are not supported in this release.",
}

_UNSUPPORTED_THREAD_FILTER_FIELDS: set[str] = {
    "values", "ids", "sort_by", "sort_order", "select", "extract",
}


def _preflight_run_create(run: RunCreate) -> func.HttpResponse | None:
    """Return a 501 response if *run* uses unsupported features, else ``None``."""
    for field_name, message in _UNSUPPORTED_FIELDS.items():
        value = getattr(run, field_name, None)
        if value is not None:
            return _platform_error(501, message)
    if run.multitask_strategy is not None and run.multitask_strategy != "reject":
        return _platform_error(
            501,
            f"Multitask strategy {run.multitask_strategy!r} is not supported; "
            f"only 'reject' is available.",
        )
    return None


def _get_threadless_graph(graph: Any) -> Any | None:
    """Return a checkpoint-disabled clone of *graph* for threadless execution.

    If the graph has a checkpointer, we clone it with ``checkpointer=None``
    so that threadless runs never persist orphaned state.  If the graph has
    no checkpointer, return it as-is.

    If the graph has a checkpointer but no ``copy`` method, or ``copy()``
    raises, return ``None`` — threadless runs are not safe for this graph.
    """
    checkpointer = getattr(graph, "checkpointer", None)
    if checkpointer is None:
        return graph  # No checkpointer — safe as-is
    # Has checkpointer — try to disable it
    copy_fn = getattr(graph, "copy", None)
    if copy_fn is None:
        logger.warning(
            "Graph has checkpointer but no copy() method; threadless runs unavailable"
        )
        return None  # Cannot disable checkpointer safely
    try:
        return copy_fn(update={"checkpointer": None})
    except Exception:
        logger.warning(
            "Failed to clone graph with checkpointer disabled",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Registration deps — narrow interface, NOT the whole LangGraphApp
# ---------------------------------------------------------------------------


class PlatformRouteDeps:
    """Holds all dependencies the platform routes need.

    Constructed by ``LangGraphApp._build_function_app()`` when
    ``platform_compat`` is enabled.
    """

    __slots__ = (
        "registrations",
        "thread_store",
        "auth_level",
        "max_stream_response_bytes",
        "max_request_body_bytes",
        "max_input_depth",
        "max_input_nodes",
    )

    def __init__(
        self,
        *,
        registrations: dict[str, Any],
        thread_store: ThreadStore,
        auth_level: func.AuthLevel,
        max_stream_response_bytes: int,
        max_request_body_bytes: int = 1024 * 1024,
        max_input_depth: int = 32,
        max_input_nodes: int = 10_000,
    ) -> None:
        self.registrations = registrations
        self.thread_store = thread_store
        self.auth_level = auth_level
        self.max_stream_response_bytes = max_stream_response_bytes
        self.max_request_body_bytes = max_request_body_bytes
        self.max_input_depth = max_input_depth
        self.max_input_nodes = max_input_nodes


# ---------------------------------------------------------------------------
# Assistant helpers
# ---------------------------------------------------------------------------

# Module-level timestamp for stable assistant responses within a process.
# Re-computed only on import / process restart.
_PROCESS_START = datetime.now(timezone.utc)


def _registration_to_assistant(name: str, reg: Any) -> Assistant:
    """Build an ``Assistant`` response from an internal ``_GraphRegistration``."""
    return Assistant(
        assistant_id=name,
        graph_id=name,
        config={},
        created_at=_PROCESS_START,
        metadata=None,
        version=1,
        name=name,
        description=getattr(reg, "description", None),
        updated_at=_PROCESS_START,
        context={},
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_platform_routes(
    app: func.FunctionApp,
    deps: PlatformRouteDeps,
) -> None:
    """Register all LangGraph Platform–compatible routes on *app*.

    Parameters
    ----------
    app:
        The Azure ``FunctionApp`` to register routes on.
    deps:
        Narrow dependency bag containing registrations, thread store,
        auth level, and max stream bytes.
    """

    auth = deps.auth_level

    # ── POST /assistants/search ──────────────────────────────────────

    @app.function_name(name="aflg_platform_assistants_search")
    @app.route(route="assistants/search", methods=["POST"], auth_level=auth)
    def assistants_search(req: func.HttpRequest) -> func.HttpResponse:
        # Body size check — reject before parsing
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
            search = AssistantSearch.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        results: list[Assistant] = []
        for reg_name, reg in deps.registrations.items():
            if search.graph_id is not None and reg_name != search.graph_id:
                continue
            if search.metadata is not None:
                # Assistants don't have user metadata — any filter excludes all
                continue
            if search.name is not None and search.name.casefold() not in reg_name.casefold():
                continue
            results.append(_registration_to_assistant(reg_name, reg))

        # Apply offset/limit
        page = results[search.offset : search.offset + search.limit]
        return func.HttpResponse(
            body=json.dumps([a.model_dump(mode="json") for a in page], default=str),
            mimetype="application/json",
            status_code=200,
        )

    # ── POST /assistants/count ────────────────────────────────────

    @app.function_name(name="aflg_platform_assistants_count")
    @app.route(route="assistants/count", methods=["POST"], auth_level=auth)
    def assistants_count(req: func.HttpRequest) -> func.HttpResponse:
        # Body size check — reject before parsing
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
            count_req = AssistantCount.model_validate(body)
        except Exception as exc:
            return _platform_error(422, f"Validation error: {exc}")

        total = 0
        for reg_name, _reg in deps.registrations.items():
            if count_req.graph_id is not None and reg_name != count_req.graph_id:
                continue
            if count_req.metadata is not None:
                # Assistants don't have user metadata — any filter excludes all
                continue
            if count_req.name is not None and count_req.name.casefold() not in reg_name.casefold():
                continue
            total += 1

        return func.HttpResponse(
            body=json.dumps(total),
            mimetype="application/json",
            status_code=200,
        )

    # ── GET /assistants/{assistant_id} ───────────────────────────────

    @app.function_name(name="aflg_platform_assistants_get")
    @app.route(route="assistants/{assistant_id}", methods=["GET"], auth_level=auth)
    def assistants_get(req: func.HttpRequest) -> func.HttpResponse:
        assistant_id = req.route_params.get("assistant_id", "")
        reg = deps.registrations.get(assistant_id)
        if reg is None:
            return _platform_error(404, f"Assistant {assistant_id!r} not found")
        assistant = _registration_to_assistant(assistant_id, reg)
        return func.HttpResponse(
            body=json.dumps(assistant.model_dump(mode="json"), default=str),
            mimetype="application/json",
            status_code=200,
        )

    # ── POST /threads ────────────────────────────────────────────────

    @app.function_name(name="aflg_platform_threads_create")
    @app.route(route="threads", methods=["POST"], auth_level=auth)
    def threads_create(req: func.HttpRequest) -> func.HttpResponse:
        # Body size check — reject before parsing
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

    # ── GET /threads/{thread_id} ─────────────────────────────────────

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

    # ── PATCH /threads/{thread_id} ────────────────────────────────────

    @app.function_name(name="aflg_platform_threads_update")
    @app.route(route="threads/{thread_id}", methods=["PATCH"], auth_level=auth)
    def threads_update(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        # Body size check — reject before parsing
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

        # Thread must exist
        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        # Merge metadata (shallow) only when metadata is provided
        if update_req.metadata is not None:
            merged = {**(thread.metadata or {}), **update_req.metadata}
            try:
                updated = deps.thread_store.update(thread_id, metadata=merged)
            except KeyError:
                return _platform_error(404, f"Thread {thread_id!r} not found")
        else:
            # No fields to update — just return current thread
            updated = thread

        return func.HttpResponse(
            body=json.dumps(updated.model_dump(mode="json"), default=str),
            mimetype="application/json",
            status_code=200,
        )

    # ── DELETE /threads/{thread_id} ───────────────────────────────────

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

    # ── POST /threads/search ─────────────────────────────────────

    @app.function_name(name="aflg_platform_threads_search")
    @app.route(route="threads/search", methods=["POST"], auth_level=auth)
    def threads_search(req: func.HttpRequest) -> func.HttpResponse:
        # Body size check
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

        # Reject non-object JSON (e.g. [], "abc", 123, null)
        if not isinstance(body, dict):
            return _platform_error(400, "Request body must be a JSON object")

        # Reject unsupported fields BEFORE model validation strips them
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

    # ── POST /threads/count ──────────────────────────────────────

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

        # Reject non-object JSON (e.g. [], "abc", 123, null)
        if not isinstance(body, dict):
            return _platform_error(400, "Request body must be a JSON object")

        # Reject unsupported fields
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
    # ── GET /threads/{thread_id}/state ───────────────────────────────

    @app.function_name(name="aflg_platform_threads_state_get")
    @app.route(
        route="threads/{thread_id}/state", methods=["GET"], auth_level=auth
    )
    def threads_state_get(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)
        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        # Thread must be bound to an assistant (graph) to have state
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
            # No checkpoint yet — return empty state
            state = ThreadState(
                values={},
                next=[],
                checkpoint={"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": None},  # type: ignore[arg-type]
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

        values: dict[str, Any] | list[dict[str, Any]] = (
            snapshot.values
            if isinstance(snapshot.values, (dict, list))
            else {}
        )
        next_nodes: list[str] = list(snapshot.next) if hasattr(snapshot, "next") else []
        metadata = (
            dict(snapshot.metadata)
            if hasattr(snapshot, "metadata") and snapshot.metadata is not None
            else None
        )

        state = ThreadState(
            values=values,
            next=next_nodes,
            checkpoint={"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": None},  # type: ignore[arg-type]
            metadata=metadata,
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

    # ── POST /threads/{thread_id}/runs/wait ──────────────────────────

    @app.function_name(name="aflg_platform_runs_wait")
    @app.route(
        route="threads/{thread_id}/runs/wait", methods=["POST"], auth_level=auth
    )
    def runs_wait(req: func.HttpRequest) -> func.HttpResponse:
        thread_id = req.route_params.get("thread_id", "")
        tid_err = validate_thread_id(thread_id)
        if tid_err:
            return _platform_error(400, tid_err)

        # Parse request
        # Body size check — reject before parsing
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

        # Validate assistant_id as a graph name
        name_err = validate_graph_name(run_req.assistant_id)
        if name_err:
            return _platform_error(400, name_err)
        # Preflight: reject unsupported features
        preflight = _preflight_run_create(run_req)
        if preflight is not None:
            return preflight

        # Thread must exist (after cheap syntax checks)
        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        # Resolve assistant → graph registration
        reg = deps.registrations.get(run_req.assistant_id)
        if reg is None:
            return _platform_error(
                404, f"Assistant {run_req.assistant_id!r} not found"
            )

        # Structural validation on user-supplied fields
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

        # Thread-assistant binding: set on first run, immutable after.
        # NOTE: There is an inherent TOCTOU race between the read above and
        # the update below.  For the InMemoryThreadStore single-process case
        # this is acceptable; a durable backend should use compare-and-swap.
        if thread.assistant_id is None:
            deps.thread_store.update(thread_id, assistant_id=run_req.assistant_id)
        elif thread.assistant_id != run_req.assistant_id:
            return _platform_error(
                409,
                f"Thread {thread_id!r} is bound to assistant "
                f"{thread.assistant_id!r}, cannot run with "
                f"{run_req.assistant_id!r}",
            )

        # Reject if thread is busy (multitask_strategy=reject is the only
        # supported strategy; concurrent runs are not allowed).
        if thread.status == "busy":
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"Concurrent runs are not supported (multitask_strategy=reject).",
            )

        # Mark thread busy
        deps.thread_store.update(thread_id, status="busy")

        # Build config
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        if run_req.config:
            user_config = dict(run_req.config)
            user_configurable = user_config.pop("configurable", {})
            config["configurable"].update(user_configurable)
            config["configurable"]["thread_id"] = thread_id
            config.update(user_config)

        # Execute graph synchronously
        graph_input = run_req.input or {}
        try:
            result = reg.graph.invoke(graph_input, config=config)
        except Exception:
            logger.exception(
                "Graph %s invoke failed for thread %s",
                run_req.assistant_id,
                thread_id,
            )
            deps.thread_store.update(thread_id, status="error")
            return _platform_error(500, "Graph execution failed")

        # Update thread state
        output = result if isinstance(result, dict) else {"result": result}
        deps.thread_store.update(thread_id, status="idle", values=output)

        # SDK runs/wait returns final state values (dict), NOT a Run object.
        # Include Content-Location header so the SDK can extract run metadata.
        run_id = str(uuid.uuid4())
        return func.HttpResponse(
            body=json.dumps(output, default=str),
            mimetype="application/json",
            status_code=200,
            headers={"Content-Location": f"/api/threads/{thread_id}/runs/{run_id}"},
        )

    # ── POST /threads/{thread_id}/runs/stream ────────────────────────

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

        # Parse request
        # Body size check — reject before parsing
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

        # Validate assistant_id as a graph name
        name_err = validate_graph_name(run_req.assistant_id)
        if name_err:
            return _platform_error(400, name_err)
        # Preflight: reject unsupported features
        preflight = _preflight_run_create(run_req)
        if preflight is not None:
            return preflight

        # Thread must exist (after cheap syntax checks)
        thread = deps.thread_store.get(thread_id)
        if thread is None:
            return _platform_error(404, f"Thread {thread_id!r} not found")

        # Resolve stream_mode early — string: use as-is; one-item list:
        # unwrap; multi-item list: unsupported (501).  Done before thread
        # mutation so a rejection cannot leave stale assistant_id bindings.
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

        # Resolve assistant → graph
        reg = deps.registrations.get(run_req.assistant_id)
        if reg is None:
            return _platform_error(
                404, f"Assistant {run_req.assistant_id!r} not found"
            )

        # Structural validation on user-supplied fields
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

        # Graph must support streaming
        if not isinstance(reg.graph, StreamableGraph):
            return _platform_error(
                501,
                f"Graph {run_req.assistant_id!r} does not support streaming",
            )

        # Thread-assistant binding (see TOCTOU note in runs_wait).
        if thread.assistant_id is None:
            deps.thread_store.update(thread_id, assistant_id=run_req.assistant_id)
        elif thread.assistant_id != run_req.assistant_id:
            return _platform_error(
                409,
                f"Thread {thread_id!r} is bound to assistant "
                f"{thread.assistant_id!r}, cannot run with "
                f"{run_req.assistant_id!r}",
            )

        # Reject if thread is busy (multitask_strategy=reject).
        if thread.status == "busy":
            return _platform_error(
                409,
                f"Thread {thread_id!r} is already busy. "
                f"Concurrent runs are not supported (multitask_strategy=reject).",
            )

        # Mark thread busy
        deps.thread_store.update(thread_id, status="busy")

        # Build config
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        if run_req.config:
            user_config = dict(run_req.config)
            user_configurable = user_config.pop("configurable", {})
            config["configurable"].update(user_configurable)
            config["configurable"]["thread_id"] = thread_id
            config.update(user_config)

        # Synthetic run ID for metadata event
        run_id = str(uuid.uuid4())

        graph_input = run_req.input or {}
        chunks: list[str] = []
        buffered_bytes = 0
        max_bytes = deps.max_stream_response_bytes

        # Metadata event (first SSE event per SDK protocol)
        meta_chunk = format_metadata_event(run_id)
        chunks.append(meta_chunk)
        buffered_bytes += len(meta_chunk.encode())

        # If metadata alone exceeds the limit, bail immediately.
        if buffered_bytes > max_bytes:
            chunks.append(
                format_error_event(
                    f"stream response exceeded max buffered size "
                    f"({max_bytes} bytes)"
                )
            )
            chunks.append(format_end_event())
            deps.thread_store.update(thread_id, status="error")
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
                    deps.thread_store.update(thread_id, status="error")
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
            deps.thread_store.update(thread_id, status="error")
            chunks.append(format_error_event("stream processing failed"))
            # End event after error
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

        # End event
        chunks.append(format_end_event())

        # Update thread state to idle after successful stream
        deps.thread_store.update(thread_id, status="idle")

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

    # ── POST /runs/wait (threadless) ──────────────────────────────

    @app.function_name(name="aflg_platform_runs_wait_threadless")
    @app.route(route="runs/wait", methods=["POST"], auth_level=auth)
    def runs_wait_threadless(req: func.HttpRequest) -> func.HttpResponse:
        # Body size check — reject before parsing
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

        # Validate assistant_id as a graph name
        name_err = validate_graph_name(run_req.assistant_id)
        if name_err:
            return _platform_error(400, name_err)
        # Preflight: reject unsupported features
        preflight = _preflight_run_create(run_req)
        if preflight is not None:
            return preflight

        # Resolve assistant → graph registration
        reg = deps.registrations.get(run_req.assistant_id)
        if reg is None:
            return _platform_error(
                404, f"Assistant {run_req.assistant_id!r} not found"
            )

        # Structural validation on user-supplied fields
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

        # Disable checkpointer for threadless runs — prevents orphaned state.
        exec_graph = _get_threadless_graph(reg.graph)
        if exec_graph is None:
            return _platform_error(
                501,
                f"Graph {run_req.assistant_id!r} has a checkpointer that cannot "
                f"be disabled for threadless execution.",
            )

        # Build config — threadless: no thread_id injected.
        config: dict[str, Any] = {}
        if run_req.config:
            user_config = dict(run_req.config)
            user_configurable = user_config.pop("configurable", {})
            config["configurable"] = user_configurable
            config.update(user_config)

        # Reject thread_id on threadless routes — prevent semantic confusion.
        if config.get("configurable", {}).get("thread_id") is not None:
            return _platform_error(
                422, "thread_id is not allowed on threadless runs"
            )

        # Execute graph synchronously
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

    # ── POST /runs/stream (threadless) ────────────────────────────

    @app.function_name(name="aflg_platform_runs_stream_threadless")
    @app.route(route="runs/stream", methods=["POST"], auth_level=auth)
    def runs_stream_threadless(req: func.HttpRequest) -> func.HttpResponse:
        # Body size check — reject before parsing
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

        # Validate assistant_id as a graph name
        name_err = validate_graph_name(run_req.assistant_id)
        if name_err:
            return _platform_error(400, name_err)
        # Preflight: reject unsupported features
        preflight = _preflight_run_create(run_req)
        if preflight is not None:
            return preflight

        # Resolve stream_mode early
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

        # Resolve assistant → graph
        reg = deps.registrations.get(run_req.assistant_id)
        if reg is None:
            return _platform_error(
                404, f"Assistant {run_req.assistant_id!r} not found"
            )

        # Structural validation on user-supplied fields
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

        # Disable checkpointer for threadless runs — prevents orphaned state.
        exec_graph = _get_threadless_graph(reg.graph)
        if exec_graph is None:
            return _platform_error(
                501,
                f"Graph {run_req.assistant_id!r} has a checkpointer that cannot "
                f"be disabled for threadless execution.",
            )

        # Graph must support streaming (check the clone, not the original).
        if not isinstance(exec_graph, StreamableGraph):
            return _platform_error(
                501,
                f"Graph {run_req.assistant_id!r} does not support streaming",
            )

        # Build config — threadless: no thread_id injected.
        config: dict[str, Any] = {}
        if run_req.config:
            user_config = dict(run_req.config)
            user_configurable = user_config.pop("configurable", {})
            config["configurable"] = user_configurable
            config.update(user_config)

        # Reject thread_id on threadless routes — prevent semantic confusion.
        if config.get("configurable", {}).get("thread_id") is not None:
            return _platform_error(
                422, "thread_id is not allowed on threadless runs"
            )

        # Synthetic run ID for metadata event
        run_id = str(uuid.uuid4())

        graph_input = run_req.input or {}
        chunks: list[str] = []
        buffered_bytes = 0
        max_bytes = deps.max_stream_response_bytes

        # Metadata event (first SSE event per SDK protocol)
        meta_chunk = format_metadata_event(run_id)
        chunks.append(meta_chunk)
        buffered_bytes += len(meta_chunk.encode())

        # If metadata alone exceeds the limit, bail immediately.
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

        # End event
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

# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "PlatformRouteDeps",
    "register_platform_routes",
]
