"""Characterization of Service Bus request-to-execution planning (#861).

Pins, for both the sync and async Service Bus processors, the stages recorded in
the planning stage matrix of ``docs/architecture.md``: thread identity, lock
scope, execution config through ``guard_run_config``, observer context
(including what is *not* emitted on rejection) and serialization/error
handling. Written before any adapter was routed through shared planning
helpers, so it fails if that refactor changes observable behavior.

Service Bus has its own contract: thread identity comes from
``thread_id_factory``/``session_id`` and is **not** admitted with
``validate_thread_id``; a checkpointed graph does not require a thread id;
contention raises instead of returning a rejection; the raw result goes to
``result_handler`` unserialized.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest

from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY,
    ThreadLeaseLostError,
)
from azure_functions_langgraph.observability import RunContext
from azure_functions_langgraph.triggers.service_bus import (
    TRIGGER_TYPE,
    ThreadContentionError,
    _ServiceBusRegistration,
    process_service_bus_message,
    process_service_bus_message_async,
)
from tests.ownership_guard_support import GuardableLock, guard_in
from tests.test_service_bus import FakeServiceBusMessage

Mode = Literal["sync", "async"]
MODES: list[Mode] = ["sync", "async"]
CHECKPOINTER = object()


class Graph:
    """Records the exact keyword arguments each invocation received."""

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

    async def ainvoke(self, input: Any, **kwargs: Any) -> Any:
        return self.invoke(input, **kwargs)


class Observer:
    def __init__(self) -> None:
        self.events: list[tuple[str, RunContext]] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append(("started", ctx))

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append(("completed", ctx))

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self.events.append(("failed", ctx))

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:
        self.events.append(("rejected", ctx))

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


def _reg(graph: Any, **kwargs: Any) -> _ServiceBusRegistration:
    kwargs.setdefault("lock_timeout", 0.0)
    return _ServiceBusRegistration(graph=graph, name="worker", connection="SB", **kwargs)


class _Msg(FakeServiceBusMessage):
    def __init__(self, body: bytes = b'{"q": 1}', *, session_id: Any = None) -> None:
        super().__init__(body)
        self.session_id = session_id


async def _process(
    mode: Mode,
    reg: _ServiceBusRegistration,
    msg: Any,
    lock: Any,
    observer: Observer | None = None,
) -> Any:
    kwargs: dict[str, Any] = {"thread_lock": lock}
    if observer is not None:
        kwargs["observer"] = observer
    if mode == "sync":
        return process_service_bus_message(reg, msg, **kwargs)
    return await process_service_bus_message_async(reg, msg, **kwargs)


# --- thread identity ---------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_no_factory_is_threadless_and_takes_no_lock_even_when_checkpointed(
    mode: Mode,
) -> None:
    graph, lock = Graph(checkpointer=CHECKPOINTER), RecordingLock()
    await _process(mode, _reg(graph), _Msg(), lock)
    # No config keyword at all (not ``config=None``) and no lock for a threadless run.
    assert graph.calls == [({"q": 1}, {})]
    assert lock.acquired == []


@pytest.mark.parametrize("mode", MODES)
async def test_thread_id_is_forwarded_as_the_only_configurable_key(mode: Mode) -> None:
    graph, lock = Graph(checkpointer=CHECKPOINTER), RecordingLock()
    await _process(mode, _reg(graph, thread_id_factory=lambda _m: "t-1"), _Msg(), lock)
    assert graph.calls[0][1] == {"config": {"configurable": {"thread_id": "t-1"}}}
    assert lock.acquired == [("worker", "t-1", 0.0)]
    assert lock.released == [("worker", "t-1")]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("thread_id", ["x" * 1000, "bad\nid", "ünï"])
async def test_thread_id_is_not_admitted_with_validate_thread_id(
    mode: Mode, thread_id: str
) -> None:
    """Native rejects these with 400; Service Bus trusts its own factory."""
    graph = Graph(checkpointer=CHECKPOINTER)
    await _process(
        mode, _reg(graph, thread_id_factory=lambda _m: thread_id), _Msg(), RecordingLock()
    )
    assert graph.calls[0][1]["config"]["configurable"]["thread_id"] == thread_id


@pytest.mark.parametrize("mode", MODES)
async def test_checkpointer_without_thread_id_is_not_an_error(mode: Mode) -> None:
    graph = Graph(checkpointer=CHECKPOINTER)
    result = await _process(
        mode, _reg(graph, thread_id_factory=lambda _m: None), _Msg(), RecordingLock()
    )
    assert result == {"ok": True}
    assert graph.calls[0][1] == {}


@pytest.mark.parametrize("mode", MODES)
async def test_without_checkpointer_thread_id_is_forwarded_but_not_locked(mode: Mode) -> None:
    graph, lock, obs = Graph(), RecordingLock(), Observer()
    await _process(mode, _reg(graph, thread_id_factory=lambda _m: "t-1"), _Msg(), lock, obs)
    assert graph.calls[0][1] == {"config": {"configurable": {"thread_id": "t-1"}}}
    assert lock.acquired == []
    assert obs.events[0][1].has_checkpointer is False


@pytest.mark.parametrize("mode", MODES)
async def test_session_id_is_the_thread_and_exclusive_session_skips_the_lock(mode: Mode) -> None:
    shared_graph, shared_lock = Graph(checkpointer=CHECKPOINTER), RecordingLock()
    await _process(mode, _reg(shared_graph, session=True), _Msg(session_id="s-1"), shared_lock)
    assert shared_graph.calls[0][1]["config"] == {"configurable": {"thread_id": "s-1"}}
    assert shared_lock.acquired == [("worker", "s-1", 0.0)]

    graph, lock = Graph(checkpointer=CHECKPOINTER), RecordingLock()
    await _process(
        mode, _reg(graph, session=True, session_exclusive=True), _Msg(session_id="s-1"), lock
    )
    assert graph.calls[0][1]["config"] == {"configurable": {"thread_id": "s-1"}}
    assert lock.acquired == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("session_id", [None, "", "   ", 7])
async def test_invalid_session_id_fails_before_observer_lock_or_graph(
    mode: Mode, session_id: Any
) -> None:
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER), RecordingLock(), Observer()
    with pytest.raises(ValueError, match="session mode requires a nonempty message session_id"):
        await _process(mode, _reg(graph, session=True), _Msg(session_id=session_id), lock, obs)
    assert (graph.calls, lock.acquired, obs.events) == ([], [], [])


@pytest.mark.parametrize("mode", MODES)
async def test_session_factory_mismatch_fails_before_observer_lock_or_graph(mode: Mode) -> None:
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER), RecordingLock(), Observer()
    reg = _reg(graph, session=True, thread_id_factory=lambda _m: "other")
    with pytest.raises(ValueError, match="must match message session_id"):
        await _process(mode, reg, _Msg(session_id="s-1"), lock, obs)
    assert (graph.calls, lock.acquired, obs.events) == ([], [], [])


# --- execution config through guard_run_config ----------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_guard_aware_lock_hands_the_saver_a_guard_in_a_fresh_config(mode: Mode) -> None:
    graph, lock = Graph(checkpointer=CHECKPOINTER), GuardableLock()
    await _process(mode, _reg(graph, thread_id_factory=lambda _m: "t-1"), _Msg(), lock)
    config = graph.calls[0][1]["config"]
    assert config["configurable"]["thread_id"] == "t-1"
    assert guard_in(config) is lock.guards[0]
    assert lock.issued == [("worker", "t-1", lock.acquired[0])]
    assert lock.guards[0].checks >= 1  # checked again once the run finished


@pytest.mark.parametrize("mode", MODES)
async def test_guard_trip_during_the_run_fails_the_message_and_releases(mode: Mode) -> None:
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER), GuardableLock(trip_on=1), Observer()
    reg = _reg(graph, thread_id_factory=lambda _m: "t-1")
    with pytest.raises(ThreadLeaseLostError):
        await _process(mode, reg, _Msg(), lock, obs)
    assert obs.names == ["started", "failed"]
    assert lock.released == lock.acquired
    assert lock.acquire("worker", "t-1") is not None


@pytest.mark.parametrize("mode", MODES)
async def test_plain_lock_leaves_the_config_free_of_a_guard(mode: Mode) -> None:
    graph = Graph(checkpointer=CHECKPOINTER)
    await _process(mode, _reg(graph, thread_id_factory=lambda _m: "t-1"), _Msg(), RecordingLock())
    assert OWNERSHIP_GUARD_CONFIG_KEY not in graph.calls[0][1]["config"]["configurable"]


# --- observer context --------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_observer_context_carries_trigger_and_correlation_identifiers(mode: Mode) -> None:
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER), RecordingLock(), Observer()
    msg = _Msg()
    msg.message_id = "msg-42"
    await _process(mode, _reg(graph, thread_id_factory=lambda _m: "t-1"), msg, lock, obs)
    assert obs.names == ["started", "completed"]
    started, completed = obs.events[0][1], obs.events[1][1]
    assert started.graph_name == "worker"
    assert started.endpoint == "invoke"
    assert started.trigger_type == TRIGGER_TYPE == "service_bus"
    assert started.invocation_id == "msg-42"
    assert started.thread_id == "t-1"
    assert started.has_checkpointer is True
    assert started.lock_backend == "RecordingLock"
    assert started.run_id
    assert (started.assistant_id, started.transport) == (None, "buffered")
    assert started.ended_at_ns is None
    assert completed.run_id == started.run_id
    assert completed.ended_at_ns is not None


@pytest.mark.parametrize("mode", MODES)
async def test_every_delivery_gets_a_distinct_run_id(mode: Mode) -> None:
    graph, obs = Graph(), Observer()
    for _ in range(2):
        await _process(mode, _reg(graph), _Msg(), RecordingLock(), obs)
    assert obs.events[0][1].run_id != obs.events[2][1].run_id


@pytest.mark.parametrize("mode", MODES)
async def test_contention_raises_without_any_observer_event(mode: Mode) -> None:
    """Unlike Native/Durable, a Service Bus lock collision is not ``on_run_rejected``."""
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER), RecordingLock(), Observer()
    held = lock.acquire("worker", "t-1")
    assert held is not None
    with pytest.raises(ThreadContentionError):
        await _process(mode, _reg(graph, thread_id_factory=lambda _m: "t-1"), _Msg(), lock, obs)
    assert (graph.calls, obs.events) == ([], [])
    lock.release("worker", "t-1", held)


@pytest.mark.parametrize("mode", MODES)
async def test_contention_error_message_names_no_message_content(mode: Mode) -> None:
    lock = RecordingLock()
    held = lock.acquire("worker", "t-1")
    assert held is not None
    reg = _reg(Graph(checkpointer=CHECKPOINTER), thread_id_factory=lambda _m: "t-1")
    with pytest.raises(ThreadContentionError) as caught:
        await _process(mode, reg, _Msg(b'{"secret": "payload"}'), lock)
    assert "payload" not in str(caught.value)
    assert str(caught.value) == (
        "Thread 't-1' for graph 'worker' is currently in use; message will be retried by the broker"
    )


# --- serialization and error handling ---------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_result_is_not_serialized_and_reaches_the_handler_by_identity(mode: Mode) -> None:
    result = {"unserializable": object(), "nan": float("nan")}
    seen: list[tuple[Any, Any]] = []
    msg = _Msg()
    reg = _reg(Graph(result=result), result_handler=lambda r, m: seen.append((r, m)))
    returned = await _process(mode, reg, msg, RecordingLock())
    assert returned is result
    assert seen == [(result, msg)]
    assert seen[0][0] is result


@pytest.mark.parametrize("mode", MODES)
async def test_graph_error_propagates_unchanged_and_skips_the_result_handler(mode: Mode) -> None:
    boom = RuntimeError("secret detail")
    called: list[Any] = []
    graph, lock, obs = Graph(checkpointer=CHECKPOINTER, error=boom), RecordingLock(), Observer()
    reg = _reg(
        graph,
        thread_id_factory=lambda _m: "t-1",
        result_handler=lambda r, m: called.append(r),
    )
    with pytest.raises(RuntimeError) as caught:
        await _process(mode, reg, _Msg(), lock, obs)
    assert caught.value is boom
    assert called == []
    assert obs.names == ["started", "failed"]
    assert lock.released == [("worker", "t-1")]


@pytest.mark.parametrize("mode", MODES)
async def test_result_handler_failure_propagates_after_the_lock_is_released(mode: Mode) -> None:
    def handler(result: Any, msg: Any) -> None:
        raise ValueError("sink down")

    lock, obs = RecordingLock(), Observer()
    reg = _reg(
        Graph(checkpointer=CHECKPOINTER), thread_id_factory=lambda _m: "t-1", result_handler=handler
    )
    with pytest.raises(ValueError, match="sink down"):
        await _process(mode, reg, _Msg(), lock, obs)
    assert obs.names == ["started", "completed"]
    assert lock.released == [("worker", "t-1")]
