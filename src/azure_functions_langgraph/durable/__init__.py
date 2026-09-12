"""Optional Durable Functions-backed async-run control plane (issue #408).

This subpackage adds an optional create/get/cancel async-run lifecycle on top of
a :class:`~azure_functions_langgraph.app.LangGraphApp`, backed by Azure Durable
Functions. It is gated behind the ``durable`` extra::

    pip install azure-functions-langgraph[durable]

Design boundaries (see issue #408):

* The Durable **orchestrator** is trivial and deterministic — no I/O, clock,
  randomness, LLM, tool, or graph user code.
* All graph execution runs in a Durable **activity**, preserving replay
  determinism and reusing the existing checkpointer / thread-lock semantics.
* ``run_id == Durable instance_id == platform run_id`` — one stable id, no
  second run registry.

Durable history is **not** a replacement for LangGraph checkpoints, and this is
not an "unlimited"-duration guarantee — it is an async run lifecycle over the
existing graph runtime.

Only :mod:`._runtime` imports ``azure.durable_functions``; the state, activity,
orchestrator, and lifecycle modules are import-safe without the extra so they
stay fully unit-testable.
"""

from __future__ import annotations

from azure_functions_langgraph.durable._activity import (
    GraphRegistrationLike,
    execute_langgraph_run_impl,
)
from azure_functions_langgraph.durable._lifecycle import (
    DurableClientLike,
    OrchestrationStatusLike,
    cancel_run_impl,
    create_run_impl,
    get_run_impl,
    runtime_status_str,
)
from azure_functions_langgraph.durable._orchestrator import (
    ACTIVITY_NAME,
    OrchestrationContextLike,
    run_orchestration,
)
from azure_functions_langgraph.durable._state import (
    DurableRunPayload,
    DurableRunResult,
    DurableRunStatus,
    normalize_status,
    serialize_error,
)

__all__ = [
    # State
    "DurableRunStatus",
    "DurableRunPayload",
    "DurableRunResult",
    "normalize_status",
    "serialize_error",
    # Activity
    "GraphRegistrationLike",
    "execute_langgraph_run_impl",
    # Orchestrator
    "ACTIVITY_NAME",
    "OrchestrationContextLike",
    "run_orchestration",
    # Lifecycle
    "DurableClientLike",
    "OrchestrationStatusLike",
    "create_run_impl",
    "get_run_impl",
    "cancel_run_impl",
    "runtime_status_str",
]
