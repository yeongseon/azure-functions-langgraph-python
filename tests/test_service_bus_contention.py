from __future__ import annotations

import asyncio  # Existing asyncio runtime: exercise raw task cancellation from the Functions host.
from concurrent.futures import ThreadPoolExecutor
import threading
from typing import Literal

import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.triggers.service_bus import ThreadContentionError
from tests.test_service_bus import (
    AsyncRecordingGraph,
    FakeServiceBusMessage,
    RecordingGraph,
    RecordingObserver,
)


class SignalingLock(InProcessThreadLock):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = threading.Event()
        self.timeouts: list[float] = []
        self.released: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.timeouts.append(timeout)
        if len(self.timeouts) == 2:
            self.waiting.set()
        return super().acquire(graph_name, thread_id, timeout)

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.released.append(token)
        super().release(graph_name, thread_id, token)


def test_sync_delivery_waits_for_first_run_then_succeeds() -> None:
    # Given a real registered trigger whose first run owns the checkpoint lock.
    entered = threading.Event()
    proceed = threading.Event()

    class HoldingGraph(RecordingGraph):
        def invoke(self, input: str, config: None = None) -> str:
            if input == "first":
                entered.set()
                assert proceed.wait(3)
            return input

    lock = SignalingLock()
    app = LangGraphApp(thread_lock=lock)
    app.register_service_bus(
        HoldingGraph(checkpointer=True),
        "jobs",
        connection="SB",
        queue_name="q",
        input_mapper=lambda msg: msg.get_body().decode(),
        thread_id_factory=lambda msg: "thread",
    )
    handler = next(
        f.get_user_function()
        for f in app.function_app.get_functions()
        if f.get_function_name() == "aflg_sb_jobs"
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(handler, FakeServiceBusMessage(b"first"))
        try:
            assert entered.wait(3)
            # When a follow-up delivery waits while the first run finishes.
            second = pool.submit(handler, FakeServiceBusMessage(b"second"))
            assert lock.waiting.wait(3)
        finally:
            proceed.set()
        first.result(timeout=3)
        second.result(timeout=3)

    # Then both deliveries complete without a broker retry, each releasing once.
    assert lock.timeouts == [5.0, 5.0]
    assert len(set(lock.released)) == 2


@pytest.mark.asyncio
async def test_async_delivery_waits_without_blocking_loop() -> None:
    # Given a registered async trigger with an in-flight checkpointed run.
    entered = asyncio.Event()
    proceed = asyncio.Event()

    class HoldingGraph(AsyncRecordingGraph):
        async def ainvoke(self, input: str, config: None = None) -> str:
            if input == "first":
                entered.set()
                await proceed.wait()
            return input

    lock = SignalingLock()
    app = LangGraphApp(thread_lock=lock)
    app.register_service_bus(
        HoldingGraph(checkpointer=True),
        "jobs",
        connection="SB",
        queue_name="q",
        input_mapper=lambda msg: msg.get_body().decode(),
        thread_id_factory=lambda msg: "thread",
        lock_timeout=0.75,
    )
    handler = next(
        f.get_user_function()
        for f in app.function_app.get_functions()
        if f.get_function_name() == "aflg_sb_jobs"
    )
    first = asyncio.create_task(handler(FakeServiceBusMessage(b"first")))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        # When a second delivery contends, the loop can still release the first.
        second = asyncio.create_task(handler(FakeServiceBusMessage(b"second")))
        assert await asyncio.to_thread(lock.waiting.wait, 3)
    finally:
        proceed.set()
        await first
        if second is not None:
            await second

    # Then the configured budget is forwarded, and both deliveries release once.
    assert lock.timeouts == [0.75, 0.75]
    assert len(set(lock.released)) == 2


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("budget", [0.0, 0.02])
def test_timeout_fails_once_without_running_graph(
    mode: Literal["sync", "async"], budget: float
) -> None:
    # Given a lock held beyond the delivery's finite wait budget.
    lock = SignalingLock()
    owner = lock.acquire("jobs", "thread")
    assert owner is not None
    observer = RecordingObserver()
    graph = RecordingGraph(checkpointer=True)
    async_graph = AsyncRecordingGraph(checkpointer=True)
    app = LangGraphApp(thread_lock=lock, observer=observer)
    app.register_service_bus(
        async_graph if mode == "async" else graph,
        "jobs",
        connection="SB",
        queue_name="q",
        thread_id_factory=lambda msg: "thread",
        lock_timeout=budget,
    )
    handler = next(
        f.get_user_function()
        for f in app.function_app.get_functions()
        if f.get_function_name() == "aflg_sb_jobs"
    )

    # When acquisition times out, one contention failure reaches the host.
    try:
        with pytest.raises(ThreadContentionError):
            if mode == "async":
                asyncio.run(handler(FakeServiceBusMessage(b"hi")))
            else:
                handler(FakeServiceBusMessage(b"hi"))
        # Then there was one acquisition attempt, no graph run or owner release.
        assert lock.timeouts == [0.0, budget]
        assert lock.released == []
        assert graph.calls == async_graph.calls == observer.events == []
    finally:
        lock.release("jobs", "thread", owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_token", [True, False])
async def test_cancelled_delivery_releases_late_token(late_token: bool) -> None:
    # Given an acquisition blocked until after host cancellation.
    entered = threading.Event()
    proceed = threading.Event()

    class LateLock(SignalingLock):
        def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
            entered.set()
            assert proceed.wait(3)
            if late_token:
                return super().acquire(graph_name, thread_id, timeout)
            return None

    lock = LateLock()
    graph = AsyncRecordingGraph(checkpointer=True)
    app = LangGraphApp(thread_lock=lock)
    app.register_service_bus(
        graph,
        "jobs",
        connection="SB",
        queue_name="q",
        thread_id_factory=lambda msg: "thread",
        lock_timeout=0.5,
    )
    handler = next(
        f.get_user_function()
        for f in app.function_app.get_functions()
        if f.get_function_name() == "aflg_sb_jobs"
    )
    delivery = asyncio.create_task(handler(FakeServiceBusMessage(b"hi")))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        # When cancellation arrives before the worker returns its token.
        delivery.cancel()
        await asyncio.sleep(0)
        delivery.cancel()
    finally:
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await delivery

    # Then cleanup precedes cancellation propagation, without invoking the graph.
    assert len(lock.released) == int(late_token)
    assert graph.calls == []


@pytest.mark.parametrize("budget", [-1.0, float("inf"), float("-inf"), float("nan")])
def test_registration_rejects_unbounded_or_negative_wait(budget: float) -> None:
    # Given an invalid wait ceiling.
    app = LangGraphApp()
    # When registration parses the timeout, it rejects it before binding a trigger.
    with pytest.raises(ValueError, match="lock_timeout"):
        app.register_service_bus(
            RecordingGraph(), "jobs", connection="SB", queue_name="q", lock_timeout=budget
        )
    # Then no unusable registration remains.
    assert app._sb_registrations == {}
