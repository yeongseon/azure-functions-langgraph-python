"""Characterization of Durable-activity request-to-execution planning (#861).

Pins the stages recorded in the planning stage matrix of ``docs/architecture.md``
for the Durable single Activity: thread identity, lock scope, execution config
through ``guard_run_config``, observer context (including ``on_run_rejected`` on
contention) and serialization/error handling. Written before any adapter was
routed through shared planning helpers.

Imports only ``durable._activity``/``durable._state`` (no ``azure.durable_functions``),
so it runs without the ``durable`` extra.

Durable has its own contract: the payload carries ``thread_id`` and ``config``;
the activity **never re-raises** an ``Exception`` (failures and contention become
a normalized result), thread ids are not validated, and a checkpointed graph
does not require one.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

import pytest

from azure_functions_langgraph.durable._activity import TRIGGER_TYPE, execute_langgraph_run_impl
from azure_functions_langgraph.durable._state import DurableRunPayload
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.locks.guard import OWNERSHIP_GUARD_CONFIG_KEY, ThreadLeaseLostError
from azure_functions_langgraph.observability import RunContext
from tests.ownership_guard_support import GuardableLock, guard_in

CHECKPOINTER = object()


class Reg:
    def __init__(self, graph: Any, name: str = "echo", async_mode: bool = False) -> None:
        self.graph = graph
        self.name = name
        self.async_mode = async_mode


class Graph:
    def __init__(self, *, checkpointer: Any = None, result: Any = None, error: Any = None) -> None:
        self.checkpointer = checkpointer
        self.result = {"ok": True} if result is None else result
        self.error = error
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def invoke(self, input: Any, **kwargs: Any) -> Any:
        self.calls.append((input, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class Observer:
    def __init__(self) -> None:
        self.events: list[tuple[str, RunContext]] = []
        self.reasons: list[str] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append(("started", ctx))

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append(("completed", ctx))

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self.events.append(("failed", ctx))

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:
        self.events.append(("rejected", ctx))
        self.reasons.append(reason)

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class RecordingLock(InProcessThreadLock):
    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[tuple[str, str, float]] = []
        self.released: list[tuple[str, str]] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.acquired.append((graph_name, thread_id, timeout))
        return super().acquire(graph_name, thread_id, timeout)

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.released.append((graph_name, thread_id))
        super().release(graph_name, thread_id, token)


def _payload(**kwargs: Any) -> DurableRunPayload:
    kwargs.setdefault("run_id", "run-1")
    kwargs.setdefault("graph_name", "echo")
    kwargs.setdefault("input", {"q": 1})
    return DurableRunPayload(**kwargs)


async def _run(
    payload: DurableRunPayload, graph: Any, lock: Any, observer: Observer | None = None
) -> Any:
    kwargs: dict[str, Any] = {}
    if observer is not None:
        kwargs["observer"] = observer
    return await execute_langgraph_run_impl(
        payload, registry={"echo": Reg(graph)}, thread_lock=lock, **kwargs
    )


# --- thread identity ---------------------------------------------------------


async def test_no_thread_id_and_no_config_is_threadless_without_lock() -> None:
    graph, lock = Graph(checkpointer=CHECKPOINTER), RecordingLock()
    result = await _run(_payload(), graph, lock)
    assert result.activity_status == "success"
    assert graph.calls == [({"q": 1}, {})]  # no config keyword at all
    assert lock.acquired == []


async def test_thread_id_is_merged_over_a_conflicting_configurable_thread_id() -> None:
    graph, lock = Graph(checkpointer=CHECKPOINTER), RecordingLock()
    payload = _payload(
        thread_id="t-1",
        config={"tags": ["x"], "configurable": {"thread_id": "other", "keep": 1}},
    )
    await _run(payload, graph, lock)
    assert graph.calls[0][1]["config"] == {
        "tags": ["x"],
        "configurable": {"thread_id": "t-1", "keep": 1},
    }
    assert lock.acquired == [("echo", "t-1", 0.0)]
    assert lock.released == [("echo", "t-1")]


async def test_payload_config_is_never_mutated() -> None:
    config = {"configurable": {"keep": 1}}
    snapshot = copy.deepcopy(config)
    graph = Graph(checkpointer=CHECKPOINTER)
    payload = _payload(thread_id="t-1", config=config)
    await _run(payload, graph, RecordingLock())
    assert payload.config == snapshot
    assert graph.calls[0][1]["config"] is not config


async def test_config_only_thread_id_is_not_a_thread_identity_for_lock_or_ctx() -> None:
    """The HTTP create route lifts ``config.configurable.thread_id`` into the payload; the
    activity itself reads ``payload.thread_id`` only."""
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER), RecordingLock(), Observer()
    payload = _payload(config={"configurable": {"thread_id": "t-9"}})
    await _run(payload, graph, lock, obs)
    assert graph.calls[0][1]["config"] == {"configurable": {"thread_id": "t-9"}}
    assert lock.acquired == []
    assert obs.events[0][1].thread_id is None


@pytest.mark.parametrize("thread_id", ["x" * 1000, "bad\nid", "ünï"])
async def test_thread_id_is_not_admitted_with_validate_thread_id(thread_id: str) -> None:
    graph = Graph(checkpointer=CHECKPOINTER)
    result = await _run(_payload(thread_id=thread_id), graph, RecordingLock())
    assert result.activity_status == "success"
    assert graph.calls[0][1]["config"]["configurable"]["thread_id"] == thread_id


async def test_checkpointer_without_thread_id_is_not_an_error() -> None:
    result = await _run(_payload(), Graph(checkpointer=CHECKPOINTER), RecordingLock())
    assert result.activity_status == "success"


async def test_without_checkpointer_thread_id_is_forwarded_but_not_locked() -> None:
    graph, lock, obs = Graph(), RecordingLock(), Observer()
    await _run(_payload(thread_id="t-1"), graph, lock, obs)
    assert graph.calls[0][1]["config"] == {"configurable": {"thread_id": "t-1"}}
    assert lock.acquired == []
    assert obs.events[0][1].has_checkpointer is False


# --- execution config through guard_run_config ----------------------------------


async def test_guard_aware_lock_hands_the_saver_a_guard_in_a_fresh_config() -> None:
    graph, lock = Graph(checkpointer=CHECKPOINTER), GuardableLock()
    await _run(_payload(thread_id="t-1"), graph, lock)
    config = graph.calls[0][1]["config"]
    assert guard_in(config) is lock.guards[0]
    assert lock.issued == [("echo", "t-1", lock.acquired[0])]
    assert lock.guards[0].checks >= 1


async def test_guard_trip_is_captured_as_a_normalized_error_and_releases() -> None:
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER), GuardableLock(trip_on=1), Observer()
    result = await _run(_payload(thread_id="t-1"), graph, lock, obs)
    assert result.activity_status == "error"
    assert result.error == {"type": ThreadLeaseLostError.__name__, "message": "The run failed."}
    assert obs.names == ["started", "failed"]
    assert lock.released == lock.acquired


async def test_plain_lock_leaves_the_config_free_of_a_guard() -> None:
    graph = Graph(checkpointer=CHECKPOINTER)
    await _run(_payload(thread_id="t-1"), graph, RecordingLock())
    assert OWNERSHIP_GUARD_CONFIG_KEY not in graph.calls[0][1]["config"]["configurable"]


# --- observer context ----------------------------------------------------------


async def test_observer_context_carries_payload_correlation_identifiers() -> None:
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER), RecordingLock(), Observer()
    await _run(_payload(thread_id="t-1", assistant_id="asst-1"), graph, lock, obs)
    assert obs.names == ["started", "completed"]
    started, completed = obs.events[0][1], obs.events[1][1]
    assert started.graph_name == "echo"
    assert started.endpoint == "invoke"
    assert started.trigger_type == TRIGGER_TYPE == "durable"
    assert started.run_id == "run-1"  # the stable Durable run id, never a fresh one
    assert started.thread_id == "t-1"
    assert started.assistant_id == "asst-1"
    assert started.invocation_id is None
    assert started.has_checkpointer is True
    assert started.lock_backend == "RecordingLock"
    assert started.ended_at_ns is None
    assert completed.run_id == "run-1"
    assert completed.ended_at_ns is not None


async def test_contention_emits_on_run_rejected_and_returns_rejected_without_running() -> None:
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER), RecordingLock(), Observer()
    held = lock.acquire("echo", "t-1")
    assert held is not None
    result = await _run(_payload(thread_id="t-1"), graph, lock, obs)
    assert result.activity_status == "rejected"
    assert result.error is not None
    assert obs.names == ["rejected"]
    assert obs.reasons == ["lock_contention"]
    assert obs.events[0][1].run_id == "run-1"
    assert obs.events[0][1].ended_at_ns is not None
    assert graph.calls == []
    lock.release("echo", "t-1", held)


async def test_unregistered_graph_emits_no_observer_event() -> None:
    obs = Observer()
    result = await execute_langgraph_run_impl(
        _payload(graph_name="missing"),
        registry={},
        thread_lock=RecordingLock(),
        observer=obs,
    )
    assert result.activity_status == "error"
    assert result.error is not None and result.error["type"] == "KeyError"
    assert obs.events == []


# --- serialization and error handling -----------------------------------------


async def test_result_is_round_tripped_through_json() -> None:
    graph = Graph(result={"items": (1, 2), "nested": {"k": ("a",)}})
    result = await _run(_payload(), graph, RecordingLock())
    assert result.activity_status == "success"
    assert result.result == {"items": [1, 2], "nested": {"k": ["a"]}}


async def test_non_json_objects_fall_back_to_their_string_form() -> None:
    result = await _run(_payload(), Graph(result={"v": object()}), RecordingLock())
    assert result.activity_status == "success"
    assert result.result["v"].startswith("<object object at ")


@pytest.mark.parametrize("bad", [{"v": float("nan")}, {"v": float("inf")}])
async def test_unserializable_output_becomes_a_sanitized_error_and_failed_event(bad: Any) -> None:
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER, result=bad), RecordingLock(), Observer()
    result = await _run(_payload(thread_id="t-1"), graph, lock, obs)
    assert result.activity_status == "error"
    assert result.error == {"type": "_OutputSerializationError", "message": "The run failed."}
    assert obs.names == ["started", "failed"]
    assert lock.released == [("echo", "t-1")]


async def test_graph_error_is_captured_sanitized_and_logged_server_side(
    caplog: pytest.LogCaptureFixture,
) -> None:
    graph, lock, obs = (
        Graph(checkpointer=CHECKPOINTER, error=RuntimeError("secret detail")),
        RecordingLock(),
        Observer(),
    )
    with caplog.at_level(logging.ERROR):
        result = await _run(_payload(thread_id="t-1"), graph, lock, obs)
    assert result.error == {"type": "RuntimeError", "message": "The run failed."}
    assert "secret detail" not in repr(result)
    assert "run_id=run-1" in caplog.text
    assert obs.names == ["started", "failed"]
    assert lock.released == [("echo", "t-1")]


async def test_base_exception_is_observed_released_and_propagated_not_captured() -> None:
    graph = Graph(checkpointer=CHECKPOINTER, error=asyncio.CancelledError())
    lock, obs = RecordingLock(), Observer()
    with pytest.raises(asyncio.CancelledError):
        await _run(_payload(thread_id="t-1"), graph, lock, obs)
    assert obs.names == ["started", "failed"]
    assert lock.released == [("echo", "t-1")]
    assert lock.acquire("echo", "t-1") is not None
