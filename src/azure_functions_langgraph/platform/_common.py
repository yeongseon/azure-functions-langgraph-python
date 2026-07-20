from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

import azure.functions as func

from azure_functions_langgraph._validation import (
    validate_body_size,
    validate_graph_name,
    validate_input_structure,
)
from azure_functions_langgraph.platform._sse import format_end_event, format_error_event
from azure_functions_langgraph.platform.contracts import (
    Assistant,
    Checkpoint,
    RunCreate,
    ThreadState,
)
from azure_functions_langgraph.platform.stores import ThreadStore
from azure_functions_langgraph.protocols import CloneableGraph

logger = logging.getLogger(__name__)


def _platform_error(status_code: int, detail: str) -> func.HttpResponse:
    """Return a JSON error matching LangGraph Platform conventions."""
    body = json.dumps({"detail": detail})
    return func.HttpResponse(
        body=body,
        mimetype="application/json",
        status_code=status_code,
    )


def _build_sse_response(
    chunks: list[str],
    *,
    content_location: str,
) -> func.HttpResponse:
    """Build the buffered SSE ``HttpResponse`` shared by all streaming routes.

    Every platform streaming exit path emits the same ``text/event-stream``
    response with identical headers, differing only in ``Content-Location``.
    """
    return func.HttpResponse(
        body="".join(chunks),
        mimetype="text/event-stream",
        status_code=200,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Content-Location": content_location,
        },
    )


def _normalize_stream_mode(
    raw_mode: Any,
) -> tuple[Any, func.HttpResponse | None]:
    """Normalize a ``RunCreate.stream_mode`` value to a single mode.

    Returns ``(stream_mode, None)`` on success. A one-element list collapses to
    its single element, an empty list defaults to ``"values"``, and a
    multi-element list returns ``(None, 501_response)`` since multi-stream-mode
    is unsupported. Non-list values pass through unchanged.
    """
    if isinstance(raw_mode, list):
        if len(raw_mode) == 1:
            return raw_mode[0], None
        if len(raw_mode) == 0:
            return "values", None
        return None, _platform_error(
            501,
            "Multi-stream-mode is not supported in this release. "
            "Provide a single stream_mode string or a one-element list.",
        )
    return raw_mode, None

def _check_stream_overflow(
    chunks: list[str],
    buffered_bytes: int,
    chunk_bytes: int,
    max_bytes: int,
) -> bool:
    """Guard the buffered SSE size cap shared by every streaming route.

    Returns ``True`` when appending *chunk_bytes* would push the buffer past
    *max_bytes*; on overflow the standard error + end events are appended to
    *chunks* so the caller can release any locks and emit the SSE response.
    Pass ``chunk_bytes=0`` for the initial metadata-only check.
    """
    if buffered_bytes + chunk_bytes > max_bytes:
        chunks.append(
            format_error_event(
                f"stream response exceeded max buffered size ({max_bytes} bytes)"
            )
        )
        chunks.append(format_end_event())
        return True
    return False


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
    "values",
    "ids",
    "sort_by",
    "sort_order",
    "select",
    "extract",
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

    The graph must satisfy the :class:`CloneableGraph` protocol (i.e. have a
    ``copy(*, update)`` method).  If it does not, or ``copy()`` raises, return
    ``None`` — threadless runs are not safe for this graph.
    """
    checkpointer = getattr(graph, "checkpointer", None)
    if checkpointer is None:
        return graph  # No checkpointer - safe as-is
    # Has checkpointer - try to disable it
    if not isinstance(graph, CloneableGraph):
        logger.warning(
            "Graph has checkpointer but does not satisfy CloneableGraph protocol; "
            "threadless runs unavailable"
        )
        return None
    try:
        return graph.copy(update={"checkpointer": None})
    except Exception:
        logger.warning(
            "Failed to clone graph with checkpointer disabled",
            exc_info=True,
        )
        return None


def _parse_run_create(
    req: func.HttpRequest,
    deps: PlatformRouteDeps,
    *,
    require_dict_body: bool,
) -> RunCreate | func.HttpResponse:
    """Parse and validate the shared RunCreate request preamble.

    Covers the body-size guard, JSON parse, optional dict-shape guard, model
    validation, assistant-name validation, and unsupported-feature preflight
    shared by every ``runs/*`` route. Returns the parsed ``RunCreate`` on
    success or an ``HttpResponse`` on the first failing check, preserving the
    exact status-code precedence of the inlined handlers.
    """
    raw_body = req.get_body()
    size_err = validate_body_size(raw_body, deps.max_request_body_bytes)
    if size_err:
        return _platform_error(400, size_err)

    try:
        body = req.get_json()
    except ValueError:
        return _platform_error(400, "Invalid JSON body")

    if require_dict_body and not isinstance(body, dict):
        return _platform_error(400, "Request body must be a JSON object")

    try:
        run_req = RunCreate.model_validate(body)
    except Exception as exc:  # noqa: BLE001 - surfaced as a 422 to the client
        return _platform_error(422, f"Validation error: {exc}")

    name_err = validate_graph_name(run_req.assistant_id)
    if name_err:
        return _platform_error(400, name_err)

    preflight = _preflight_run_create(run_req)
    if preflight is not None:
        return preflight

    return run_req


def _validate_run_io_structure(
    run_req: RunCreate,
    deps: PlatformRouteDeps,
) -> func.HttpResponse | None:
    """Validate the ``input`` and ``config`` structure depth/breadth limits."""
    if run_req.input:
        input_err = validate_input_structure(
            run_req.input,
            max_depth=deps.max_input_depth,
            max_nodes=deps.max_input_nodes,
        )
        if input_err:
            return _platform_error(400, input_err)
    if run_req.config:
        config_err = validate_input_structure(
            run_req.config,
            max_depth=deps.max_input_depth,
            max_nodes=deps.max_input_nodes,
        )
        if config_err:
            return _platform_error(400, config_err)
    return None


def _resolve_run_graph(
    run_req: RunCreate,
    deps: PlatformRouteDeps,
) -> Any:
    """Look up the assistant registration and validate its run I/O structure.

    Returns the registration on success, or an ``HttpResponse`` when the
    assistant is unknown (404) or the input/config structure is invalid (400).
    """
    reg = deps.registrations.get(run_req.assistant_id)
    if reg is None:
        return _platform_error(404, f"Assistant {run_req.assistant_id!r} not found")
    io_err = _validate_run_io_structure(run_req, deps)
    if io_err is not None:
        return io_err
    return reg


def _build_threaded_config(run_req: RunCreate, thread_id: str) -> dict[str, Any]:
    """Build the run config for a thread-scoped run, pinning ``thread_id``."""
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if run_req.config:
        user_config = dict(run_req.config)
        user_configurable = user_config.pop("configurable", {})
        config["configurable"].update(user_configurable)
        config["configurable"]["thread_id"] = thread_id
        config.update(user_config)
    return config


def _build_threadless_config(
    run_req: RunCreate,
) -> dict[str, Any] | func.HttpResponse:
    """Build the run config for a threadless run, rejecting any ``thread_id``.

    Returns ``(config, None)`` on success or ``(None, 422_response)`` when the
    caller supplied a ``thread_id`` (not permitted for threadless execution).
    """
    config: dict[str, Any] = {}
    if run_req.config:
        user_config = dict(run_req.config)
        user_configurable = user_config.pop("configurable", {})
        config["configurable"] = user_configurable
        config.update(user_config)
    if config.get("configurable", {}).get("thread_id") is not None:
        return _platform_error(422, "thread_id is not allowed on threadless runs")
    return config


def _read_json_body(
    req: func.HttpRequest,
    deps: PlatformRouteDeps,
    *,
    require_dict: bool,
    allow_empty: bool,
) -> Any:
    """Read and size-guard a request body, returning parsed JSON or an error.

    Shared by the ``threads`` routes. Returns the decoded body on success, or a
    ``func.HttpResponse`` error (400) on an oversized body, invalid JSON, or —
    when *require_dict* — a non-object body. When *allow_empty* is set an empty
    request body decodes to ``{}`` instead of being parsed.
    """
    raw = req.get_body()
    size_err = validate_body_size(raw, deps.max_request_body_bytes)
    if size_err:
        return _platform_error(400, size_err)
    if allow_empty and not (raw and raw.strip() != b""):
        return {}
    try:
        body = req.get_json()
    except ValueError:
        return _platform_error(400, "Invalid JSON body")
    if require_dict and not isinstance(body, dict):
        return _platform_error(400, "Request body must be a JSON object")
    return body


def _resolve_thread_graph(
    deps: PlatformRouteDeps,
    thread_id: str,
    *,
    protocol: type,
    capability: str,
) -> Any:
    """Resolve the graph bound to *thread_id*, enforcing capability support.

    Returns the graph on success, or a ``func.HttpResponse`` error when the
    thread is missing (404), unbound (409), its assistant is unknown (404), or
    the graph does not satisfy *protocol* (409, ``does not support {capability}``).
    """
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
    if not isinstance(graph, protocol):
        return _platform_error(
            409,
            f"Graph {thread.assistant_id!r} does not support {capability}",
        )
    return graph


def _build_checkpoint_config(
    thread_id: str,
    checkpoint: dict[str, Any] | None,
    *,
    fallback_checkpoint_id: str | None = None,
) -> Any:
    """Build the ``config`` dict for a checkpoint-scoped thread operation.

    Shared by ``threads_state_update`` and ``threads_history``. Validates that a
    checkpoint-supplied ``thread_id`` matches the path (422 on mismatch) and
    carries over ``checkpoint_id`` / ``checkpoint_ns``. When no ``checkpoint`` is
    given, *fallback_checkpoint_id* (state-update only) is applied. Returns the
    config dict on success, or a ``func.HttpResponse`` on a thread_id mismatch.
    """
    configurable: dict[str, Any] = {"thread_id": thread_id}
    if checkpoint is not None:
        cp_thread_id = checkpoint.get("thread_id")
        if cp_thread_id is not None and cp_thread_id != thread_id:
            return _platform_error(
                422,
                f"Checkpoint thread_id {cp_thread_id!r} does not match "
                f"path thread_id {thread_id!r}",
            )
        cp_id = checkpoint.get("checkpoint_id")
        if cp_id is not None:
            configurable["checkpoint_id"] = cp_id
        cp_ns = checkpoint.get("checkpoint_ns")
        if cp_ns is not None:
            configurable["checkpoint_ns"] = cp_ns
    elif fallback_checkpoint_id is not None:
        configurable["checkpoint_id"] = fallback_checkpoint_id
    return {"configurable": configurable}


def _snapshot_to_thread_state(snapshot: Any, thread_id: str) -> ThreadState:
    """Convert a LangGraph ``StateSnapshot`` to the SDK ``ThreadState`` contract.

    Extracts ``checkpoint_id`` and ``checkpoint_ns`` from the snapshot's
    ``config["configurable"]`` when available, falling back to bare defaults.
    """
    values: dict[str, Any] | list[dict[str, Any]] = (
        snapshot.values if isinstance(snapshot.values, (dict, list)) else {}
    )
    next_nodes: list[str] = list(snapshot.next) if hasattr(snapshot, "next") else []
    metadata = (
        dict(snapshot.metadata)
        if hasattr(snapshot, "metadata") and snapshot.metadata is not None
        else None
    )

    # Extract checkpoint info from snapshot config when available
    snap_config = getattr(snapshot, "config", None) or {}
    snap_configurable = snap_config.get("configurable", {}) if isinstance(snap_config, dict) else {}
    checkpoint_id = snap_configurable.get("checkpoint_id")
    checkpoint_ns = snap_configurable.get("checkpoint_ns", "")

    # Parent checkpoint from parent_config
    parent_config = getattr(snapshot, "parent_config", None) or {}
    parent_configurable = (
        parent_config.get("configurable", {}) if isinstance(parent_config, dict) else {}
    )
    parent_checkpoint_id = parent_configurable.get("checkpoint_id")
    parent_checkpoint: Checkpoint | None = None
    if parent_checkpoint_id is not None:
        parent_checkpoint = Checkpoint(
            thread_id=thread_id,
            checkpoint_ns=parent_configurable.get("checkpoint_ns", ""),
            checkpoint_id=parent_checkpoint_id,
        )

    # created_at
    created_at_raw = getattr(snapshot, "created_at", None)
    created_at = str(created_at_raw) if created_at_raw is not None else None

    return ThreadState(
        values=values,
        next=next_nodes,
        checkpoint=Checkpoint(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
        ),
        metadata=metadata,
        created_at=created_at,
        parent_checkpoint=parent_checkpoint,
        tasks=[],
        interrupts=[],
    )


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
