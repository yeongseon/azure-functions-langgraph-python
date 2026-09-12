"""The Durable *activity* that actually executes a LangGraph run.

All user/graph code, network, LLM, tool, clock and randomness lives here — in a
Durable **activity**, never in the orchestrator — so the orchestrator stays
deterministic and replay-safe (issue #408).

The activity mirrors the Service Bus trigger's execution shape
(:mod:`azure_functions_langgraph.triggers.service_bus`): resolve the registered
graph, acquire the existing per-thread lock when the graph is checkpointed and a
``thread_id`` is present, invoke the graph, and emit the ``RunObserver``
lifecycle. The crucial difference is that the activity **never re-raises** —
graph failures and lock contention are captured into a normalized
:class:`~azure_functions_langgraph.durable._state.DurableRunResult` so the
orchestration *completes* with a normalized output rather than surfacing as a
Durable ``Failed`` status (which would also trigger Durable's at-least-once
activity retry). This keeps a single logical run record per ``run_id``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from azure_functions_langgraph.durable._state import DurableRunPayload, DurableRunResult
from azure_functions_langgraph.locks import ThreadLock
from azure_functions_langgraph.observability import (
    NOOP_OBSERVER,
    RunObserver,
    finish_context,
    new_run_context,
    safe_observer_call,
)

__all__ = ["GraphRegistrationLike", "execute_langgraph_run_impl"]

# The generic trigger label stamped onto every RunContext this adapter emits so
# exporters can distinguish durable async runs from HTTP invoke/stream runs.
TRIGGER_TYPE = "durable"


@runtime_checkable
class GraphRegistrationLike(Protocol):
    """Structural subset of the app's internal graph registration record.

    Declares only the members the activity reads, so tests can supply a
    lightweight fake and the activity never has to import
    :class:`azure_functions_langgraph.app._GraphRegistration` (avoiding a cycle).
    """

    graph: Any
    name: str
    async_mode: bool


def _has_checkpointer(graph: Any) -> bool:
    """Whether a compiled graph has a checkpointer attached."""

    return getattr(graph, "checkpointer", None) is not None


async def execute_langgraph_run_impl(
    payload: DurableRunPayload,
    *,
    registry: Mapping[str, GraphRegistrationLike],
    thread_lock: ThreadLock,
    observer: RunObserver = NOOP_OBSERVER,
) -> DurableRunResult:
    """Execute one graph run for a durable async orchestration.

    Returns a normalized :class:`DurableRunResult` (never raises for graph or
    lock-contention failures), so the calling orchestration completes with a
    deterministic, JSON-serializable output.
    """

    reg = registry.get(payload.graph_name)
    if reg is None:
        return DurableRunResult.failure(
            payload.run_id,
            KeyError(f"No graph registered under name {payload.graph_name!r}"),
        )

    thread_id = payload.thread_id
    has_ckpt = _has_checkpointer(reg.graph)
    ctx = new_run_context(
        reg.name,
        "invoke",
        run_id=payload.run_id,
        thread_id=thread_id,
        assistant_id=payload.assistant_id,
        has_checkpointer=has_ckpt,
        lock_backend=type(thread_lock).__name__,
        trigger_type=TRIGGER_TYPE,
    )

    lock_token: Optional[str] = None
    use_lock = bool(thread_id) and has_ckpt
    if use_lock and thread_id is not None:
        lock_token = await asyncio.to_thread(thread_lock.acquire, reg.name, thread_id)
        if not lock_token:
            safe_observer_call(observer, "on_run_rejected", finish_context(ctx), "lock_contention")
            return DurableRunResult.rejected(
                payload.run_id,
                f"Thread {thread_id!r} for graph {reg.name!r} is currently in use",
            )

    config = payload.config or ({"configurable": {"thread_id": thread_id}} if thread_id else None)
    safe_observer_call(observer, "on_run_started", ctx)
    try:
        result = await _invoke_graph(reg, payload.input, config)
    except BaseException as exc:  # noqa: BLE001 - captured into a normalized result
        safe_observer_call(observer, "on_run_failed", finish_context(ctx), exc)
        return DurableRunResult.failure(payload.run_id, exc)
    else:
        safe_observer_call(observer, "on_run_completed", finish_context(ctx))
        return DurableRunResult.success(payload.run_id, result)
    finally:
        if lock_token is not None and thread_id is not None:
            await asyncio.to_thread(thread_lock.release, reg.name, thread_id, lock_token)


async def _invoke_graph(
    reg: GraphRegistrationLike, graph_input: Any, config: Optional[dict[str, Any]]
) -> Any:
    """Invoke the graph, using ``ainvoke`` when the registration is async."""

    if reg.async_mode:
        if config is not None:
            return await reg.graph.ainvoke(graph_input, config=config)
        return await reg.graph.ainvoke(graph_input)
    if config is not None:
        return await asyncio.to_thread(reg.graph.invoke, graph_input, config=config)
    return await asyncio.to_thread(reg.graph.invoke, graph_input)
