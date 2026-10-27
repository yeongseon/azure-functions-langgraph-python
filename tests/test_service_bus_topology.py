from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.triggers.service_bus import (
    process_service_bus_message,
    process_service_bus_message_async,
)
from tests.test_service_bus import AsyncRecordingGraph, RecordingGraph
from tests.test_service_bus_sessions import SessionMessage


class ForbiddenLock(InProcessThreadLock):
    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        raise AssertionError("exclusive session must not acquire the application lock")


class FakeSessionBroker:
    """Models entity-scoped session ownership, not Azure lease renewal or failover."""

    def __init__(self) -> None:
        self.owners: set[str] = set()
        self.attempts = 0
        self.completed = 0

    @contextmanager
    def delivery(self, session_id: str) -> Iterator[SessionMessage]:
        if session_id in self.owners:
            raise RuntimeError("session already owned")
        self.owners.add(session_id)
        self.attempts += 1
        try:
            message = SessionMessage(b"{}")
            message.session_id = session_id
            yield message
            self.completed += 1
        finally:
            self.owners.remove(session_id)


def test_fake_broker_excludes_same_session_but_allows_other_sessions() -> None:
    broker = FakeSessionBroker()
    with broker.delivery("one"):
        with pytest.raises(RuntimeError, match="already owned"):
            with broker.delivery("one"):
                pytest.fail("overlapping session ownership")
        with broker.delivery("two"):
            assert broker.owners == {"one", "two"}
    assert broker.completed == 2


@pytest.mark.parametrize("async_mode", [False, True])
async def test_exclusive_session_bypasses_lock_with_broker_ownership(async_mode: bool) -> None:
    # Given an isolated session-only app and an explicitly session-exclusive broker double.
    graph = (
        AsyncRecordingGraph(checkpointer=True) if async_mode else RecordingGraph(checkpointer=True)
    )
    app = LangGraphApp()
    app.register_service_bus(
        graph,
        "agent",
        connection="bus",
        queue_name="jobs",
        session=True,
        session_exclusive=True,
        async_mode=async_mode,
    )
    reg = app._sb_registrations["agent"]
    broker = FakeSessionBroker()
    # When a session-owned delivery executes without an application lock.
    with broker.delivery("thread-session") as message:
        if async_mode:
            await process_service_bus_message_async(reg, message, thread_lock=ForbiddenLock())
        else:
            process_service_bus_message(reg, message, thread_lock=ForbiddenLock())
    # Then exactly that session is used as the checkpoint thread and acknowledged.
    assert graph.calls[0][1]["configurable"]["thread_id"] == "thread-session"
    assert broker.completed == 1


@pytest.mark.parametrize("http_first", [False, True])
def test_exclusive_topology_rejects_http_in_either_registration_order(http_first: bool) -> None:
    app = LangGraphApp()
    graph = RecordingGraph(checkpointer=True)
    with pytest.raises(ValueError, match="session-exclusive"):
        if http_first:
            app.register(graph, "http")
        app.register_service_bus(
            graph,
            "agent",
            connection="bus",
            queue_name="jobs",
            session=True,
            session_exclusive=True,
        )
        if not http_first:
            app.register(graph, "http")


@pytest.mark.parametrize("other", ["non_session", "platform", "durable", "second_entity", "batch"])
def test_exclusive_topology_rejects_unsafe_combinations(other: str) -> None:
    app = LangGraphApp(
        platform_compat=other == "platform", async_runs="durable" if other == "durable" else False
    )
    graph = RecordingGraph(checkpointer=True)
    with pytest.raises(ValueError, match="session-exclusive"):
        app.register_service_bus(
            graph,
            "agent",
            connection="bus",
            queue_name="jobs",
            session=other != "non_session",
            session_exclusive=True,
            binding_kwargs={"cardinality": "many"} if other == "batch" else {},
        )
        if other == "second_entity":
            app.register_service_bus(graph, "other", connection="bus", queue_name="other")


@pytest.mark.parametrize("async_mode", [False, True])
async def test_failed_delivery_can_be_redelivered_under_session_ownership(async_mode: bool) -> None:
    # Given an exclusive session graph that fails its first attempt.
    attempts: list[str] = []

    class RetryGraph:
        checkpointer = True

        def invoke(
            self, input: dict[str, int], config: dict[str, dict[str, str]] | None = None
        ) -> dict[str, int]:
            attempts.append("invoke")
            if len(attempts) == 1:
                raise RuntimeError("retry this delivery")
            return input

        async def ainvoke(
            self, input: dict[str, int], config: dict[str, dict[str, str]] | None = None
        ) -> dict[str, int]:
            return self.invoke(input)

    app = LangGraphApp()
    app.register_service_bus(
        RetryGraph(),
        "agent",
        connection="bus",
        queue_name="jobs",
        session=True,
        session_exclusive=True,
        async_mode=async_mode,
    )
    reg = app._sb_registrations["agent"]
    broker = FakeSessionBroker()

    async def deliver() -> None:
        with broker.delivery("thread-session") as message:
            if async_mode:
                await process_service_bus_message_async(reg, message, thread_lock=ForbiddenLock())
            else:
                process_service_bus_message(reg, message, thread_lock=ForbiddenLock())

    # When failure abandons the first delivery and a new owner receives it again.
    with pytest.raises(RuntimeError, match="retry this delivery"):
        await deliver()
    await deliver()
    # Then only the successful attempt is acknowledged; duplicate execution remains possible.
    assert broker.attempts == 2
    assert broker.completed == 1
    assert len(attempts) == 2
