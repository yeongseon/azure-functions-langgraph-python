import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.triggers.service_bus import (
    process_service_bus_message,
    process_service_bus_message_async,
)
from tests.test_service_bus import AsyncRecordingGraph, FakeServiceBusMessage, RecordingGraph


class SessionMessage(FakeServiceBusMessage):
    session_id = "thread-session"


def test_session_opt_in_routes_checkpoint_thread_and_preserves_lock() -> None:
    graph = RecordingGraph(checkpointer="checkpointed")
    app = LangGraphApp()
    app.register_service_bus(graph, "agent", connection="bus", queue_name="jobs", session=True)
    reg = app._sb_registrations["agent"]
    assert reg.binding_kwargs["is_sessions_enabled"] is True
    lock = InProcessThreadLock()
    owner = lock.acquire("agent", "thread-session")
    assert owner is not None
    reg.lock_timeout = 0
    with pytest.raises(RuntimeError):
        process_service_bus_message(reg, SessionMessage(b"{}"), thread_lock=lock)
    lock.release("agent", "thread-session", owner)
    process_service_bus_message(reg, SessionMessage(b"{}"), thread_lock=lock)
    assert graph.calls[0][1]["configurable"]["thread_id"] == "thread-session"


def test_session_mode_rejects_missing_session_before_graph() -> None:
    graph = RecordingGraph(checkpointer="checkpointed")
    app = LangGraphApp()
    app.register_service_bus(graph, "agent", connection="bus", queue_name="jobs", session=True)
    with pytest.raises(ValueError, match="session_id"):
        process_service_bus_message(
            app._sb_registrations["agent"],
            FakeServiceBusMessage(b"{}"),
            thread_lock=InProcessThreadLock(),
        )
    assert graph.calls == []


@pytest.mark.parametrize("topic", [False, True])
def test_session_binding_metadata_is_generated_for_queue_and_topic(topic: bool) -> None:
    app = LangGraphApp()
    graph = RecordingGraph()
    if topic:
        app.register_service_bus(
            graph,
            "agent",
            connection="bus",
            topic_name="events",
            subscription_name="worker",
            session=True,
        )
    else:
        app.register_service_bus(graph, "agent", connection="bus", queue_name="jobs", session=True)
    functions = app.function_app.get_functions()
    bindings = [binding.get_dict_repr() for fn in functions for binding in fn.get_bindings()]
    trigger = next(binding for binding in bindings if binding["type"] == "serviceBusTrigger")
    assert trigger["isSessionsEnabled"] is True


async def test_async_session_routing_rejects_mismatched_factory() -> None:
    graph = AsyncRecordingGraph(checkpointer="checkpointed")
    app = LangGraphApp()
    app.register_service_bus(
        graph,
        "agent",
        connection="bus",
        queue_name="jobs",
        session=True,
        thread_id_factory=lambda msg: "wrong-thread",
        async_mode=True,
    )
    with pytest.raises(ValueError, match="must match"):
        await process_service_bus_message_async(
            app._sb_registrations["agent"], SessionMessage(b"{}"), thread_lock=InProcessThreadLock()
        )
    assert graph.calls == []


async def test_async_session_routes_valid_id() -> None:
    graph = AsyncRecordingGraph(checkpointer="checkpointed")
    app = LangGraphApp()
    app.register_service_bus(
        graph, "agent", connection="bus", queue_name="jobs", session=True, async_mode=True
    )
    await process_service_bus_message_async(
        app._sb_registrations["agent"], SessionMessage(b"{}"), thread_lock=InProcessThreadLock()
    )
    assert graph.calls[0][1]["configurable"]["thread_id"] == "thread-session"


def test_conflicting_session_binding_is_rejected() -> None:
    app = LangGraphApp()
    with pytest.raises(ValueError, match="conflicts"):
        app.register_service_bus(
            RecordingGraph(),
            "agent",
            connection="bus",
            queue_name="jobs",
            session=True,
            binding_kwargs={"is_sessions_enabled": False},
        )
