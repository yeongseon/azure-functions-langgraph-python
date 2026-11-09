"""HTTP control-plane handlers for the Durable async-run lifecycle.

Implements ``runs.create`` / ``runs.get`` / ``runs.cancel`` as pure async
functions that take already-parsed inputs and a
:class:`DurableClientLike` client, returning ``(status_code, body)`` tuples.
Keeping the handlers free of ``azure.functions`` request/response types (and of
any ``azure.durable_functions`` import) makes the entire lifecycle unit-testable
with a fake client and no Azure host (issue #408). The thin decorated HTTP
functions in :mod:`azure_functions_langgraph.durable._runtime` adapt
``func.HttpRequest`` to these impls and build ``func.HttpResponse``.

Identifier model (per the design review): ``run_id == Durable instance_id ==
platform run_id`` — one stable id, no second run registry. Durable instance
state is the single source of truth for lifecycle status.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, Final, Protocol, runtime_checkable
from uuid import uuid4

from azure_functions_langgraph.durable._orchestrator import ACTIVITY_NAME  # noqa: F401
from azure_functions_langgraph.durable._state import (
    DurableRunPayload,
    DurableRunStatus,
    normalize_status,
)

__all__ = [
    "CANCEL_REQUESTED",
    "DurableClientLike",
    "OrchestrationStatusLike",
    "create_run_impl",
    "get_run_impl",
    "cancel_run_impl",
    "runtime_status_str",
]

logger = logging.getLogger(__name__)

#: Acknowledgement returned by ``POST runs/{run_id}/cancel`` (HTTP 202). It says only that the
#: termination call returned without reporting a failure; it is **not** a lifecycle status, so
#: ``GET runs/{run_id}`` never returns it, and it does not mean the orchestration is terminated
#: or that the Activity running the graph (or its tools) has stopped.
CANCEL_REQUESTED: Final = "cancel_requested"

# Durable runtime statuses an orchestration never leaves again. Terminality is read from the
# runtime status, not from the normalized one: a Completed run may still normalize to "error".
_TERMINAL_RUNTIME_STATUSES: Final = frozenset(
    {"Completed", "Failed", "Terminated", "Canceled", "Cancelled"}
)


def _cancel_failed(run_id: str) -> dict[str, Any]:
    """The generic body for a cancellation the backend failed to confirm (never backend text)."""
    return {
        "run_id": run_id,
        "error": {
            "code": "cancel_failed",
            "message": "Unable to confirm the cancellation request.",
        },
    }


@runtime_checkable
class OrchestrationStatusLike(Protocol):
    """Structural subset of ``DurableOrchestrationStatus`` read by the handlers."""

    runtime_status: Any
    output: Any


@runtime_checkable
class DurableClientLike(Protocol):
    """Structural subset of ``DurableOrchestrationClient`` used by the control plane.

    Declared so lifecycle handlers can be tested with a fake client and never
    need a real Durable Task backend. Method shapes mirror the async SDK surface.
    """

    async def start_new(
        self,
        orchestration_function_name: str,
        instance_id: str | None = None,
        client_input: Any = None,
    ) -> str:
        """Start a new orchestration; returns the instance id."""
        ...

    async def get_status(self, instance_id: str) -> OrchestrationStatusLike | None:
        """Return the orchestration status, or a not-found sentinel."""
        ...

    async def terminate(self, instance_id: str, reason: str) -> None:
        """Terminate a running orchestration."""
        ...


def runtime_status_str(status: OrchestrationStatusLike | None) -> str | None:
    """Extract a plain runtime-status string from a status object.

    Tolerates both a string ``runtime_status`` and an
    ``OrchestrationRuntimeStatus`` enum (using its ``.name``), returning ``None``
    when the status object is missing or carries no runtime status (not found).
    """

    if status is None:
        return None
    raw = getattr(status, "runtime_status", None)
    if raw is None:
        return None
    # Enum → its member name (e.g. OrchestrationRuntimeStatus.Running -> "Running");
    # plain strings pass through unchanged.
    name = getattr(raw, "name", None)
    return name if isinstance(name, str) else str(raw)


async def create_run_impl(
    client: DurableClientLike,
    orchestrator_name: str,
    body: Mapping[str, Any],
    *,
    graph_name: str,
) -> tuple[int, dict[str, Any]]:
    """Start a durable async run and return ``(202, {run_id, status})``.

    ``body`` is the parsed request payload: ``input`` (required-ish, defaulted to
    ``None``), optional ``config``, ``thread_id``, ``assistant_id``, and an
    optional client-supplied ``run_id`` (idempotency key). When ``run_id`` is
    omitted a fresh one is minted here (HTTP side — non-determinism is fine).
    """

    run_id = str(body.get("run_id") or uuid4().hex)
    config = dict(body.get("config") or {})
    thread_id = body.get("thread_id")
    # Derive thread_id from config.configurable when not explicit.
    if thread_id is None:
        configurable = config.get("configurable") if isinstance(config, dict) else None
        if isinstance(configurable, Mapping):
            thread_id = configurable.get("thread_id")

    payload = DurableRunPayload(
        run_id=run_id,
        graph_name=graph_name,
        input=body.get("input"),
        thread_id=thread_id,
        config=config,
        assistant_id=body.get("assistant_id"),
        correlation_id=body.get("correlation_id"),
    )

    await client.start_new(orchestrator_name, instance_id=run_id, client_input=payload.to_dict())
    return 202, {"run_id": run_id, "status": "pending"}


async def get_run_impl(client: DurableClientLike, run_id: str) -> tuple[int, dict[str, Any]]:
    """Poll a durable run's normalized status.

    Returns ``(404, {...})`` when the instance is unknown, otherwise ``(200,
    {run_id, status, output})`` where ``status`` is the normalized lifecycle
    status and ``output`` is the orchestration output (present once completed).
    """

    status = await client.get_status(run_id)
    runtime = runtime_status_str(status)
    if runtime is None:
        return 404, {"run_id": run_id, "error": "run not found"}

    output = getattr(status, "output", None)
    output_mapping = output if isinstance(output, Mapping) else None
    normalized: DurableRunStatus = normalize_status(runtime, output_mapping)
    if normalized == "error" and output_mapping is not None:
        error = output_mapping.get("error")
        if isinstance(error, Mapping):
            logger.error(
                "Durable run failed: type=%s message=%s",
                error.get("type", "unknown"),
                error.get("message", "unknown"),
            )
            output = {
                **output_mapping,
                "error": {"code": "run_failed", "message": "The run failed."},
            }
    return 200, {"run_id": run_id, "status": normalized, "output": output}


async def cancel_run_impl(
    client: DurableClientLike, run_id: str, *, reason: str = "cancelled by client"
) -> tuple[int, dict[str, Any]]:
    """Request termination of a durable run's orchestration and say only what is known.

    Durable termination is asynchronous, and it terminates the *orchestration*: it does not
    interrupt an Activity that is already running, so the graph and its tools may keep
    executing, and writing checkpoints, after this returns and even after ``GET`` reports
    ``cancelled``. The response therefore claims no more than was observed:

    * unknown run, or a status object without a runtime status: ``404``, nothing is requested;
    * the run is already finished (Completed, Failed, Terminated or Canceled): ``200`` with
      the normalized status ``GET`` would return (``success``, ``error`` or ``cancelled``),
      and no termination is requested, so a run that succeeded is never reported as cancelled;
    * otherwise ``terminate`` is called and ``202 {run_id, status: "cancel_requested"}`` is
      returned once it returns without raising. That is "the request was made", nothing more:
      the run can still complete normally (the SDK also returns normally when the instance
      finished in the meantime), so poll ``GET`` for the outcome;
    * the status lookup or the termination call raises: ``502`` with a generic body, never the
      backend's text (it is logged); after a failed ``terminate`` the outcome is unknown.
    """

    try:
        status = await client.get_status(run_id)
    except Exception:  # noqa: BLE001 - backend failure at the HTTP boundary: logged, answered generically
        logger.exception("Durable status lookup failed while cancelling run %r", run_id)
        return 502, _cancel_failed(run_id)

    runtime = runtime_status_str(status)
    if runtime is None:
        return 404, {"run_id": run_id, "error": "run not found"}

    if runtime.strip() in _TERMINAL_RUNTIME_STATUSES:
        output = getattr(status, "output", None)
        output_mapping = output if isinstance(output, Mapping) else None
        return 200, {"run_id": run_id, "status": normalize_status(runtime, output_mapping)}

    try:
        await client.terminate(run_id, reason)
    except Exception:  # noqa: BLE001 - backend failure at the HTTP boundary: logged, answered generically
        logger.exception("Durable terminate failed for run %r", run_id)
        return 502, _cancel_failed(run_id)
    return 202, {"run_id": run_id, "status": CANCEL_REQUESTED}
