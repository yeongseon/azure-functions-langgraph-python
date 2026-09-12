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

from typing import Any, Mapping, Optional, Protocol, runtime_checkable
from uuid import uuid4

from azure_functions_langgraph.durable._orchestrator import ACTIVITY_NAME  # noqa: F401
from azure_functions_langgraph.durable._state import (
    DurableRunPayload,
    DurableRunStatus,
    normalize_status,
)

__all__ = [
    "DurableClientLike",
    "OrchestrationStatusLike",
    "create_run_impl",
    "get_run_impl",
    "cancel_run_impl",
    "runtime_status_str",
]


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
        instance_id: Optional[str] = None,
        client_input: Any = None,
    ) -> str:
        """Start a new orchestration; returns the instance id."""
        ...

    async def get_status(self, instance_id: str) -> Optional[OrchestrationStatusLike]:
        """Return the orchestration status, or a not-found sentinel."""
        ...

    async def terminate(self, instance_id: str, reason: str) -> None:
        """Terminate a running orchestration."""
        ...


def runtime_status_str(status: Optional[OrchestrationStatusLike]) -> Optional[str]:
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
    return 200, {"run_id": run_id, "status": normalized, "output": output}


async def cancel_run_impl(
    client: DurableClientLike, run_id: str, *, reason: str = "cancelled by client"
) -> tuple[int, dict[str, Any]]:
    """Cancel a durable run by terminating its orchestration.

    Returns ``(404, {...})`` when the instance is unknown, otherwise ``(202,
    {run_id, status: "cancelled"})``. Terminate is best-effort and asynchronous
    on the Durable side; the normalized status becomes ``cancelled`` once the
    termination is applied.
    """

    status = await client.get_status(run_id)
    if runtime_status_str(status) is None:
        return 404, {"run_id": run_id, "error": "run not found"}

    await client.terminate(run_id, reason)
    return 202, {"run_id": run_id, "status": "cancelled"}
