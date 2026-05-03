from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

import azure.functions as func

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
