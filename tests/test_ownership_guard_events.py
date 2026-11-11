"""Ownership-guard contracts at event-driven boundaries, without an Azure host."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from typing import Final

import azure.functions as func
import pytest

from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
from azure_functions_langgraph.durable._runtime import (
    DurableRuntimeDeps,
    create_durable_function_app,
    register_durable_runtime,
)
from azure_functions_langgraph.durable._state import DurableRunPayload, DurableRunResult
from azure_functions_langgraph.locks import InProcessThreadLock, ThreadLock
from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY as KEY,
)
from azure_functions_langgraph.locks.guard import (
    ThreadLeaseLostError,
)
from azure_functions_langgraph.triggers.service_bus import (
    ServiceBusMessageLike,
    ThreadContentionError,
    _ServiceBusRegistration,
    process_service_bus_message,
    process_service_bus_message_async,
)
from tests.ownership_guard_support import (
    GuardableLock,
    ProbeGraph,
    RaisingProviderLock,
    RecordingObserver,
    guard_in,
)
from tests.test_durable import FakeReg
from tests.test_service_bus import FakeServiceBusMessage

# allow: SIZE_OK - the requested deliverable is one file covering both event boundaries.
# asyncio is intentional: this regression exercises host Task.cancel(), not AnyIO cancellation.
THREAD: Final = "thread-SECRET-7f3a"
GRAPH: Final = "agent"


class DurableBoundary:
    """Mutable per-test activity setup, sharing only construction and invocation."""

    def __init__(self, async_mode: bool) -> None:
        self.async_mode = async_mode
        self.graph = ProbeGraph()
        self.lock = GuardableLock()
        self.observer = RecordingObserver()
        self.payload = DurableRunPayload("rid-7f3a", GRAPH, {"x": 1}, thread_id=THREAD)

    async def run(self, lock: ThreadLock | None = None) -> DurableRunResult:
        return await execute_langgraph_run_impl(
            self.payload,
            registry={GRAPH: FakeReg(self.graph, GRAPH, async_mode=self.async_mode)},
            thread_lock=self.lock if lock is None else lock,
            observer=self.observer,
        )


class BusBoundary:
    """Mutable per-test message setup exercising either real processor."""

    def __init__(self, async_mode: bool) -> None:
        self.async_mode = async_mode
        self.graph = ProbeGraph()
        self.lock = GuardableLock()
        self.observer = RecordingObserver()
        self.message = FakeServiceBusMessage(b'{"x": 1}', message_id="msg-7f3a")
        self.handled: list[tuple[object, ServiceBusMessageLike]] = []
        self.reg = _ServiceBusRegistration(
            graph=self.graph,
            name=GRAPH,
            connection="BUS",
            queue_name="jobs",
            thread_id_factory=lambda msg: THREAD,
            result_handler=lambda result, msg: self.handled.append((result, msg)),
            async_mode=async_mode,
            lock_timeout=0.0,
        )

    async def run(self, lock: ThreadLock | None = None) -> object:
        backend = self.lock if lock is None else lock
        if self.async_mode:
            return await process_service_bus_message_async(
                self.reg, self.message, thread_lock=backend, observer=self.observer
            )
        return process_service_bus_message(
            self.reg, self.message, thread_lock=backend, observer=self.observer
        )


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def durable(request: pytest.FixtureRequest) -> DurableBoundary:
    return DurableBoundary(async_mode=bool(request.param))


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def bus(request: pytest.FixtureRequest) -> BusBoundary:
    return BusBoundary(async_mode=bool(request.param))


def assert_released(lock: GuardableLock | RaisingProviderLock) -> None:
    assert len(lock.acquired) == 1
    assert lock.released == lock.acquired


async def test_durable_hands_guard_to_graph_without_serializing_it(
    durable: DurableBoundary,
) -> None:
    # Given client configuration that must survive guard injection.
    durable.payload.config.update({"configurable": {"user": "u1"}, "tags": ["t"]})
    original = deepcopy(durable.payload.to_dict())
    # When the activity invokes a checkpointed graph.
    result = await durable.run()
    # Then only the graph config carries the token-bound guard.
    config = durable.graph.configs[0]
    guard = durable.lock.guards[0]
    assert guard_in(config) is guard
    assert config == {
        "configurable": {"user": "u1", "thread_id": THREAD, KEY: guard},
        "tags": ["t"],
    }
    assert durable.lock.issued == [(GRAPH, THREAD, durable.lock.acquired[0])]
    assert guard.checks == 1
    assert result.activity_status == "success"
    assert result.result == {"ok": True}
    assert durable.observer.events == ["started", "completed"]
    assert_released(durable.lock)
    serialized = json.dumps(result.to_dict())
    assert "OwnershipGuard" not in serialized
    assert durable.lock.acquired[0] not in serialized
    assert KEY not in serialized
    assert durable.payload.to_dict() == original


async def test_durable_normalizes_terminal_loss_without_rollback(durable: DurableBoundary) -> None:
    # Given a guard reporting loss only after the graph has executed.
    durable.lock = GuardableLock(trip_on=1)
    effects: list[str] = []
    durable.graph.during_run = lambda config: effects.append("committed side effect")
    # When the terminal check fails.
    result = await durable.run()
    # Then Durable receives a safe error, not success or a retry-triggering exception.
    assert result.activity_status == "error"
    assert result.result is None
    assert result.error == {
        "type": "ThreadLeaseLostError",
        "message": "The run failed.",
    }
    serialized = json.dumps(result.to_dict())
    for secret in (THREAD, GRAPH, durable.lock.acquired[0]):
        assert secret not in serialized
    assert durable.observer.events == ["started", "failed"]
    assert len(durable.observer.failures) == 1
    failure = durable.observer.failures[0]
    assert isinstance(failure, ThreadLeaseLostError)
    assert (failure.reason, failure.graph_name, failure.thread_id) == ("lost", GRAPH, THREAD)
    assert len(durable.graph.configs) == 1
    assert effects == ["committed side effect"]
    assert durable.lock.guards[0].checks == 1
    assert_released(durable.lock)


@pytest.mark.parametrize("capable", [False, True], ids=["legacy", "disabled"])
async def test_durable_preserves_config_when_guard_unavailable(
    durable: DurableBoundary, capable: bool
) -> None:
    # Given an old lock or a capable provider switched off.
    lock = GuardableLock(enabled=False) if capable else InProcessThreadLock()
    durable.payload.config.update({"configurable": {"user": "u1"}, "tags": ["t"]})
    # When the activity executes.
    result = await durable.run(lock)
    # Then the exact pre-feature config reaches the graph, with no private alias.
    assert durable.graph.configs == [
        {"configurable": {"user": "u1", "thread_id": THREAD}, "tags": ["t"]}
    ]
    assert result.activity_status == "success"
    assert durable.observer.events == ["started", "completed"]
    if capable:
        assert isinstance(lock, GuardableLock)
        assert lock.guards == []
        assert_released(lock)


@pytest.mark.parametrize("capable", [False, True], ids=["legacy", "guarded"])
async def test_durable_discards_client_smuggled_aliases(
    durable: DurableBoundary, capable: bool
) -> None:
    # Given malicious aliases at both levels of the client config.
    lock = durable.lock if capable else InProcessThreadLock()
    durable.payload.config.update({"configurable": {KEY: "evil"}, KEY: "evil2"})
    original = deepcopy(durable.payload.to_dict())
    # When the activity builds its config from that payload.
    result = await durable.run(lock)
    # Then only a real acquired guard may survive; client data stays unmodified.
    config = durable.graph.configs[0]
    expected = (
        {"thread_id": THREAD, KEY: durable.lock.guards[0]} if capable else {"thread_id": THREAD}
    )
    assert config == {"configurable": expected}
    assert KEY not in config
    assert durable.payload.to_dict() == original
    assert result.activity_status == "success"
    if capable:
        assert guard_in(config) is durable.lock.guards[0]
        assert_released(durable.lock)


async def test_durable_normalizes_provider_failure_and_releases(durable: DurableBoundary) -> None:
    # Given a provider that raises while the token is held.
    lock = RaisingProviderLock()
    # When guard construction fails.
    result = await durable.run(lock)
    # Then no graph runs and the ordinary activity failure path releases ownership.
    assert result.activity_status == "error"
    assert result.result is None
    assert result.error == {"type": "RuntimeError", "message": "The run failed."}
    assert durable.graph.configs == []
    assert durable.observer.events == ["started", "failed"]
    assert len(durable.observer.failures) == 1
    assert isinstance(durable.observer.failures[0], RuntimeError)
    assert_released(lock)


async def test_durable_skips_terminal_check_when_graph_raises(durable: DurableBoundary) -> None:
    # Given a graph failure and a guard that would otherwise trip.
    failure = ValueError("graph failed")

    def fail(config: object) -> None:
        raise failure

    durable.graph.during_run = fail
    durable.lock = GuardableLock(trip_on=1)
    # When graph execution fails before returning normally.
    result = await durable.run()
    # Then the graph error wins and the guard is not checked in cleanup.
    assert result.activity_status == "error"
    assert result.error == {"type": "ValueError", "message": "The run failed."}
    assert result.result is None
    assert durable.lock.guards[0].checks == 0
    assert durable.observer.events == ["started", "failed"]
    assert durable.observer.failures == [failure]
    assert_released(durable.lock)


@pytest.mark.parametrize("checkpointed", [False, True], ids=["no-checkpointer", "no-thread"])
async def test_durable_issues_no_guard_without_lock_prerequisites(checkpointed: bool) -> None:
    # Given either a non-checkpointed graph or a threadless checkpointed graph.
    boundary = DurableBoundary(async_mode=checkpointed)
    boundary.graph.checkpointer = "checkpointer" if checkpointed else None
    thread_id = None if checkpointed else THREAD
    boundary.payload = DurableRunPayload("rid-7f3a", GRAPH, {}, thread_id=thread_id)
    # When the activity executes without acquiring a token.
    result = await boundary.run()
    # Then normal execution retains its old config and never asks for a guard.
    assert result.activity_status == "success"
    assert boundary.graph.configs == [
        None if thread_id is None else {"configurable": {"thread_id": THREAD}}
    ]
    assert boundary.lock.acquired == []
    assert boundary.lock.released == []
    assert boundary.lock.guards == []
    assert boundary.observer.events == ["started", "completed"]


async def test_durable_contention_rejects_without_issuing_guard() -> None:
    # Given another execution holding the same thread.
    boundary = DurableBoundary(async_mode=True)
    held = boundary.lock.acquire(GRAPH, THREAD)
    assert held is not None
    try:
        # When a second execution attempts the activity.
        result = await boundary.run()
        # Then it is rejected before graph execution or guard creation.
        assert result.activity_status == "rejected"
        assert result.error is not None
        assert result.error["type"] == "ThreadContentionError"
        assert boundary.graph.configs == boundary.lock.guards == []
        assert boundary.observer.events == ["rejected:lock_contention"]
        assert boundary.lock.acquired == [held]
        assert boundary.lock.released == []
    finally:
        boundary.lock.release(GRAPH, THREAD, held)


async def test_durable_host_cancellation_propagates_without_guard_check() -> None:
    # Given an async graph blocked after it has received its guard.
    entered, unblock = asyncio.Event(), asyncio.Event()
    configs: list[object] = []

    class BlockingGraph:
        checkpointer = "checkpointed"

        async def ainvoke(self, input: object, config: object = None) -> None:
            configs.append(config)
            entered.set()
            await asyncio.wait_for(unblock.wait(), timeout=5)

    lock, observer = GuardableLock(trip_on=1), RecordingObserver()
    task = asyncio.create_task(
        execute_langgraph_run_impl(
            DurableRunPayload("rid-7f3a", GRAPH, {}, thread_id=THREAD),
            registry={GRAPH: FakeReg(BlockingGraph(), GRAPH, async_mode=True)},
            thread_lock=lock,
            observer=observer,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        # When the host cancels an in-flight invocation.
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, timeout=5)
        # Then cancellation escapes, not a normalized success/error or terminal lease check.
        assert guard_in(configs[0]) is lock.guards[0]
        assert lock.guards[0].checks == 0
        assert observer.events == ["started", "failed"]
        assert observer.failures == [caught.value]
        assert_released(lock)
    finally:
        unblock.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)


@pytest.mark.parametrize("trip", [False, True], ids=["success", "loss"])
async def test_registered_durable_activity_wires_guard_and_normalizes_output(trip: bool) -> None:
    # The registered activity needs the optional durable extra, which the
    # minimum-dependency lane deliberately does not install.
    pytest.importorskip("azure.durable_functions")
    # Given the actual DFApp activity registration and a capable backend.
    graph, lock, observer = (
        ProbeGraph(),
        GuardableLock(trip_on=1 if trip else None),
        RecordingObserver(),
    )
    app = create_durable_function_app(func.AuthLevel.FUNCTION)
    register_durable_runtime(
        app,
        DurableRuntimeDeps(
            registry={GRAPH: FakeReg(graph, GRAPH, async_mode=True)},
            thread_lock=lock,
            observer=observer,
        ),
    )
    activity = next(
        fn for fn in app.get_functions() if fn.get_function_name() == "execute_langgraph_run"
    )
    payload = DurableRunPayload("rid-7f3a", GRAPH, {}, thread_id=THREAD).to_dict()
    original = deepcopy(payload)
    # When the registered closure rehydrates a JSON-only payload and executes.
    output = await activity.get_user_function()(json.loads(json.dumps(payload)))
    # Then wiring preserves the guard and returns persistable, sanitized activity output.
    assert guard_in(graph.configs[0]) is lock.guards[0]
    assert lock.guards[0].checks == 1
    assert output["activity_status"] == ("error" if trip else "success")
    assert output["result"] == (None if trip else {"ok": True})
    assert output["error"] == (
        {"type": "ThreadLeaseLostError", "message": "The run failed."} if trip else None
    )
    assert observer.events == ["started", "failed" if trip else "completed"]
    serialized = json.dumps(output)
    for secret in (THREAD, GRAPH, lock.acquired[0], KEY, "OwnershipGuard"):
        assert secret not in serialized
    assert payload == original
    assert_released(lock)


async def test_bus_hands_guard_to_graph_before_success_handler(bus: BusBoundary) -> None:
    # Given a checkpointed message registration with an output sink.
    # When the processor completes normally.
    result = await bus.run()
    # Then the real guard reaches the graph and is checked before publishing the result.
    guard = bus.lock.guards[0]
    assert bus.graph.configs == [{"configurable": {"thread_id": THREAD, KEY: guard}}]
    assert guard_in(bus.graph.configs[0]) is guard
    assert bus.lock.issued == [(GRAPH, THREAD, bus.lock.acquired[0])]
    assert result is bus.graph.output
    assert bus.handled == [(result, bus.message)]
    assert guard.checks == 1
    assert bus.observer.events == ["started", "completed"]
    assert_released(bus.lock)


async def test_bus_reraises_terminal_loss_without_publishing_or_rollback(bus: BusBoundary) -> None:
    # Given loss detected after a graph has already performed its execution.
    bus.lock = GuardableLock(trip_on=1)
    effects: list[str] = []
    bus.graph.during_run = lambda config: effects.append("committed side effect")
    # When the terminal guard rejects completion.
    with pytest.raises(ThreadLeaseLostError) as caught:
        await bus.run()
    # Then the exact observed exception escapes for broker retry, without an output sink call.
    assert (caught.value.reason, caught.value.graph_name, caught.value.thread_id) == (
        "lost",
        GRAPH,
        THREAD,
    )
    assert bus.observer.events == ["started", "failed"]
    assert bus.observer.failures == [caught.value]
    assert bus.observer.failures[0] is caught.value
    assert bus.handled == []
    assert len(bus.graph.configs) == 1
    assert effects == ["committed side effect"]
    assert bus.lock.guards[0].checks == 1
    for secret in (THREAD, GRAPH, bus.lock.acquired[0]):
        assert secret not in str(caught.value)
    assert_released(bus.lock)


@pytest.mark.parametrize("capable", [False, True], ids=["legacy", "disabled"])
async def test_bus_preserves_config_when_guard_unavailable(bus: BusBoundary, capable: bool) -> None:
    # Given either a legacy lock or a disabled provider.
    lock = GuardableLock(enabled=False) if capable else InProcessThreadLock()
    # When processing a checkpointed thread message.
    result = await bus.run(lock)
    # Then exactly the pre-feature config reaches the graph and output publishing still runs.
    assert bus.graph.configs == [{"configurable": {"thread_id": THREAD}}]
    assert result == {"ok": True}
    assert bus.handled == [(result, bus.message)]
    assert bus.observer.events == ["started", "completed"]
    if capable:
        assert isinstance(lock, GuardableLock)
        assert lock.guards == []
        assert_released(lock)


async def test_bus_session_exclusive_execution_never_acquires_guard(bus: BusBoundary) -> None:
    # Given session exclusivity supplied by the broker instead of the thread lock.
    class SessionMessage(FakeServiceBusMessage):
        session_id = THREAD

    bus.message = SessionMessage(b"{}")
    bus.reg.session = bus.reg.session_exclusive = True
    # When processing the session message.
    result = await bus.run()
    # Then the graph still receives the session thread id, without a token or guard.
    assert bus.graph.configs == [{"configurable": {"thread_id": THREAD}}]
    assert bus.lock.acquired == []
    assert bus.lock.released == []
    assert bus.lock.guards == []
    assert result == {"ok": True}
    assert bus.handled == [(result, bus.message)]
    assert bus.observer.events == ["started", "completed"]


@pytest.mark.parametrize("checkpointed", [False, True], ids=["no-checkpointer", "no-thread"])
async def test_bus_issues_no_guard_without_lock_prerequisites(
    bus: BusBoundary, checkpointed: bool
) -> None:
    # Given either no checkpointer or no thread factory.
    bus.graph.checkpointer = "checkpointer" if checkpointed else None
    threadless_inputs: list[object] = []

    class ThreadlessGraph:
        checkpointer = "checkpointer"

        def invoke(self, input: object) -> dict[str, bool]:
            threadless_inputs.append(input)
            return {"ok": True}

        async def ainvoke(self, input: object) -> dict[str, bool]:
            threadless_inputs.append(input)
            return {"ok": True}

    if checkpointed:
        bus.reg.thread_id_factory = None
        bus.reg.graph = ThreadlessGraph()
    # When the graph runs without locking.
    result = await bus.run()
    # Then a factory-derived thread id is still forwarded even without a checkpointer.
    if checkpointed:
        assert threadless_inputs == [{"x": 1}]
    else:
        assert bus.graph.configs == [{"configurable": {"thread_id": THREAD}}]
    assert bus.lock.acquired == []
    assert bus.lock.released == []
    assert bus.lock.guards == []
    assert result == {"ok": True}
    assert bus.handled == [(result, bus.message)]


async def test_bus_provider_failure_propagates_and_releases(bus: BusBoundary) -> None:
    # Given a provider that fails while the token is owned.
    lock = RaisingProviderLock()
    # When provider construction raises.
    with pytest.raises(RuntimeError, match="guard provider failed") as caught:
        await bus.run(lock)
    # Then the failure is observed unchanged and cannot leak a token or publish a result.
    assert bus.graph.configs == []
    assert bus.handled == []
    assert bus.observer.events == ["started", "failed"]
    assert bus.observer.failures[0] is caught.value
    assert len(bus.observer.failures) == 1
    assert_released(lock)


async def test_bus_graph_failure_bypasses_terminal_guard_check(bus: BusBoundary) -> None:
    # Given a graph exception and an independently tripping guard.
    failure = LookupError("graph failed")

    def fail(config: object) -> None:
        raise failure

    bus.graph.during_run = fail
    bus.lock = GuardableLock(trip_on=1)
    # When invocation itself fails.
    with pytest.raises(LookupError) as caught:
        await bus.run()
    # Then cleanup never replaces the graph error with a guard error.
    assert caught.value is failure
    assert bus.lock.guards[0].checks == 0
    assert bus.observer.events == ["started", "failed"]
    assert bus.observer.failures == [failure]
    assert bus.handled == []
    assert_released(bus.lock)


async def test_bus_contention_raises_without_issuing_guard(bus: BusBoundary) -> None:
    # Given another owner for this graph/thread key.
    held = bus.lock.acquire(GRAPH, THREAD)
    assert held is not None
    try:
        # When a colliding delivery is processed with no wait.
        with pytest.raises(ThreadContentionError):
            await bus.run()
        # Then neither invocation, guard creation, nor a result handler happens.
        assert bus.graph.configs == []
        assert bus.lock.guards == []
        assert bus.handled == []
        assert bus.observer.events == []
        assert bus.lock.acquired == [held]
        assert bus.lock.released == []
    finally:
        bus.lock.release(GRAPH, THREAD, held)


async def test_bus_result_handler_failure_keeps_success_terminal_event(bus: BusBoundary) -> None:
    # Given a sink failing after the graph and guard have completed.
    failure = ValueError("sink failed")

    def fail(result: object, msg: ServiceBusMessageLike) -> None:
        bus.handled.append((result, msg))
        assert bus.lock.guards[0].checks == 1
        assert_released(bus.lock)
        raise failure

    bus.reg.result_handler = fail
    # When output delivery fails.
    with pytest.raises(ValueError) as caught:
        await bus.run()
    # Then it propagates without relabeling the graph run or checking the guard twice.
    assert caught.value is failure
    assert bus.handled == [(bus.graph.output, bus.message)]
    assert bus.lock.guards[0].checks == 1
    assert bus.observer.events == ["started", "completed"]
    assert bus.observer.failures == []
    assert_released(bus.lock)
