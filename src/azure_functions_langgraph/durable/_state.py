"""Pure state models and status mapping for the Durable async-run control plane.

This module is intentionally free of any ``azure.durable_functions`` import so it
stays importable without the ``durable`` extra and is fully unit-testable without
Azure. It defines:

* :data:`DurableRunStatus` — the normalized, transport-agnostic run status.
* :func:`normalize_status` — maps a Durable ``runtime_status`` (plus the
  orchestration output) onto :data:`DurableRunStatus`.
* :class:`DurableRunPayload` / :class:`DurableRunResult` — the JSON-serializable
  activity input/output records passed through Durable orchestration history.
* :func:`serialize_error` — a safe, JSON-serializable error shape (type + message
  only; never a traceback or arbitrary object).

The orchestrator MUST stay deterministic, so everything here is pure: no clocks,
no randomness, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, cast

__all__ = [
    "DurableRunStatus",
    "DurableRunPayload",
    "DurableRunResult",
    "normalize_status",
    "serialize_error",
]

# Normalized lifecycle status exposed by the durable async-run endpoints.
# ``rejected`` is folded into ``error`` at the HTTP boundary to match the
# existing platform ``RunStatus`` vocabulary, but the activity output can carry
# an ``activity_status`` of ``"rejected"`` for observability.
DurableRunStatus = Literal["pending", "running", "success", "error", "cancelled"]

# Activity-level outcome recorded in the orchestration output payload.
_ActivityStatus = Literal["success", "error", "rejected"]


def serialize_error(exc: BaseException) -> dict[str, str]:
    """Return a safe, JSON-serializable error shape (type + message only).

    Never includes a traceback, ``args`` beyond the string form, or any
    arbitrary object — consistent with the observability boundary that a run's
    correlation surface carries the exception *type* and message only.
    """

    return {"type": type(exc).__name__, "message": str(exc)}


@dataclass(frozen=True)
class DurableRunPayload:
    """JSON-serializable input handed to the ``execute_langgraph_run`` activity.

    Carries the stable ``run_id`` (== Durable ``instance_id`` == platform run id)
    so replay never mints a second logical run, plus the graph selector and the
    LangGraph invoke ``input`` / ``config``.
    """

    run_id: str
    graph_name: str
    input: Any
    thread_id: Optional[str] = None
    config: dict[str, Any] = field(default_factory=dict)
    assistant_id: Optional[str] = None
    correlation_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain ``dict`` for Durable orchestration input."""

        return {
            "run_id": self.run_id,
            "graph_name": self.graph_name,
            "input": self.input,
            "thread_id": self.thread_id,
            "config": self.config,
            "assistant_id": self.assistant_id,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DurableRunPayload:
        """Rehydrate from the dict Durable passes to the activity."""

        return cls(
            run_id=str(data["run_id"]),
            graph_name=str(data["graph_name"]),
            input=data.get("input"),
            thread_id=data.get("thread_id"),
            config=dict(data.get("config") or {}),
            assistant_id=data.get("assistant_id"),
            correlation_id=data.get("correlation_id"),
        )


@dataclass(frozen=True)
class DurableRunResult:
    """JSON-serializable activity output; becomes the orchestration output.

    ``activity_status`` records the activity-level outcome; ``result`` holds the
    graph output on success, and ``error`` holds a :func:`serialize_error` shape
    on failure/rejection.
    """

    run_id: str
    activity_status: _ActivityStatus
    result: Any = None
    error: Optional[dict[str, str]] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain ``dict`` for the orchestration output."""

        return {
            "run_id": self.run_id,
            "activity_status": self.activity_status,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DurableRunResult:
        """Rehydrate from a serialized orchestration output ``dict``."""

        return cls(
            run_id=str(data["run_id"]),
            activity_status=cast(_ActivityStatus, data["activity_status"]),
            result=data.get("result"),
            error=data.get("error"),
        )

    @classmethod
    def success(cls, run_id: str, result: Any) -> DurableRunResult:
        """Build a successful result carrying the graph output."""

        return cls(run_id=run_id, activity_status="success", result=result)

    @classmethod
    def failure(cls, run_id: str, exc: BaseException) -> DurableRunResult:
        """Build a failed result from an exception."""

        return cls(run_id=run_id, activity_status="error", error=serialize_error(exc))

    @classmethod
    def rejected(cls, run_id: str, reason: str) -> DurableRunResult:
        """Build a rejected result (e.g. thread lock contention)."""

        return cls(
            run_id=run_id,
            activity_status="rejected",
            error={"type": "ThreadContentionError", "message": reason},
        )


def normalize_status(
    runtime_status: Optional[str],
    output: Optional[Mapping[str, Any]] = None,
) -> DurableRunStatus:
    """Map a Durable ``runtime_status`` (+ output) onto :data:`DurableRunStatus`.

    ``output`` is the orchestration output — a serialized :class:`DurableRunResult`
    — consulted only when the orchestration ``Completed`` so an activity-level
    ``error`` / ``rejected`` surfaces as a normalized ``error`` even though the
    orchestration itself completed successfully.

    Unknown / ``None`` runtime statuses are treated as ``running`` (the run is
    in-flight from the caller's perspective) rather than raising, so a transient
    Durable query gap never crashes ``runs.get``.
    """

    status = (runtime_status or "").strip()

    if status in ("", "Pending"):
        return "pending" if status == "Pending" else "running"
    if status == "Running":
        return "running"
    if status in ("Suspended", "ContinuedAsNew"):
        return "running"
    if status == "Completed":
        activity_status = None
        if output is not None:
            activity_status = output.get("activity_status")
        if activity_status in ("error", "rejected"):
            return "error"
        return "success"
    if status == "Failed":
        return "error"
    if status in ("Terminated", "Canceled", "Cancelled"):
        return "cancelled"
    return "running"
