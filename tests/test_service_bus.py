"""Deterministic tests for the Azure Service Bus trigger adapter (issue #409)."""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import MagicMock

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.observability import RunContext
from azure_functions_langgraph.triggers.service_bus import (
    ThreadContentionError,
    _ServiceBusRegistration,
    default_message_mapper,
    process_service_bus_message,
    process_service_bus_message_async,
)


class FakeServiceBusMessage:
    """Deterministic stand-in for ``azure.functions.ServiceBusMessage``."""

    def __init__(self, body: bytes, *, message_id: Optional[str] = "msg-1") -> None:
        self._body = body
        self.message_id = message_id

    def get_body(self) -> bytes:
        return self._body


class RecordingObserver:
    """Observer capturing lifecycle events for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, RunContext]] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append(("started", ctx))

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append(("completed", ctx))

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self.events.append(("failed", ctx))

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:  # pragma: no cover
        self.events.append(("rejected", ctx))


class RecordingGraph:
    """Graph capturing the input/config it was invoked with."""

    def __init__(self, *, checkpointer: Any = None, result: Any = None) -> None:
        self.checkpointer = checkpointer
        self._result = result if result is not None else {"ok": True}
        self.calls: list[tuple[Any, Any]] = []

    def invoke(self, input: Any, config: Any = None) -> Any:
        self.calls.append((input, config))
        return self._result

    def stream(self, input: Any, config: Any = None, stream_mode: str = "values") -> Any:
        yield {"chunk": 1}


class AsyncRecordingGraph:
    """Async graph capturing invocation args."""

    def __init__(self, *, checkpointer: Any = None, result: Any = None) -> None:
        self.checkpointer = checkpointer
        self._result = result if result is not None else {"ok": True}
        self.calls: list[tuple[Any, Any]] = []

    async def ainvoke(self, input: Any, config: Any = None) -> Any:
        self.calls.append((input, config))
        return self._result

    async def astream(self, input: Any, config: Any = None, stream_mode: str = "values") -> Any:
        yield {"chunk": 1}


class FailingGraph:
    checkpointer = None

    def invoke(self, input: Any, config: Any = None) -> Any:
        raise RuntimeError("boom")


def _reg(graph: Any, **kwargs: Any) -> _ServiceBusRegistration:
    kwargs.setdefault("connection", "SB_CONN")
    kwargs.setdefault("queue_name", "q")
    return _ServiceBusRegistration(graph=graph, name="agent", **kwargs)


# ---------------------------------------------------------------------------
# default_message_mapper
# ---------------------------------------------------------------------------


class TestDefaultMessageMapper:
    def test_json_object_passthrough(self) -> None:
        msg = FakeServiceBusMessage(b'{"messages": [{"role": "human", "content": "hi"}]}')
        assert default_message_mapper(msg) == {"messages": [{"role": "human", "content": "hi"}]}

    def test_json_non_object_is_wrapped(self) -> None:
        msg = FakeServiceBusMessage(b'["a", "b"]')
        assert default_message_mapper(msg) == {
            "messages": [{"role": "human", "content": '["a", "b"]'}]
        }

    def test_plain_text_is_wrapped(self) -> None:
        msg = FakeServiceBusMessage(b"hello world")
        assert default_message_mapper(msg) == {
            "messages": [{"role": "human", "content": "hello world"}]
        }

    def test_invalid_utf8_does_not_raise(self) -> None:
        msg = FakeServiceBusMessage(b"\xff\xfe bad bytes")
        result = default_message_mapper(msg)
        assert result["messages"][0]["role"] == "human"


# ---------------------------------------------------------------------------
# process_service_bus_message (sync)
# ---------------------------------------------------------------------------


class TestProcessSync:
    def test_threadless_success_no_config(self) -> None:
        graph = RecordingGraph()
        observer = RecordingObserver()
        lock = InProcessThreadLock()
        result = process_service_bus_message(
            _reg(graph), FakeServiceBusMessage(b"hi"), thread_lock=lock, observer=observer
        )
        assert result == {"ok": True}
        # No thread_id => no config forwarded.
        assert graph.calls[0][1] is None
        assert [e[0] for e in observer.events] == ["started", "completed"]
        ctx = observer.events[0][1]
        assert ctx.trigger_type == "service_bus"
        assert ctx.invocation_id == "msg-1"
        assert ctx.thread_id is None

    def test_custom_input_mapper(self) -> None:
        graph = RecordingGraph()
        reg = _reg(graph, input_mapper=lambda m: {"custom": m.get_body().decode()})
        process_service_bus_message(
            reg, FakeServiceBusMessage(b"payload"), thread_lock=InProcessThreadLock()
        )
        assert graph.calls[0][0] == {"custom": "payload"}

    def test_thread_id_with_checkpointer_forwards_config_and_locks(self) -> None:
        graph = RecordingGraph(checkpointer=MagicMock())
        lock = InProcessThreadLock()
        acquire_spy = MagicMock(wraps=lock.acquire)
        release_spy = MagicMock(wraps=lock.release)
        lock.acquire = acquire_spy  # type: ignore[method-assign]
        lock.release = release_spy  # type: ignore[method-assign]
        reg = _reg(graph, thread_id_factory=lambda m: "thread-A")
        process_service_bus_message(reg, FakeServiceBusMessage(b"hi"), thread_lock=lock)
        assert graph.calls[0][1] == {"configurable": {"thread_id": "thread-A"}}
        acquire_spy.assert_called_once_with("agent", "thread-A")
        release_spy.assert_called_once()

    def test_thread_id_without_checkpointer_does_not_lock(self) -> None:
        graph = RecordingGraph(checkpointer=None)
        lock = InProcessThreadLock()
        acquire_spy = MagicMock(wraps=lock.acquire)
        lock.acquire = acquire_spy  # type: ignore[method-assign]
        reg = _reg(graph, thread_id_factory=lambda m: "thread-A")
        process_service_bus_message(reg, FakeServiceBusMessage(b"hi"), thread_lock=lock)
        acquire_spy.assert_not_called()
        # thread_id still forwarded via config for a non-checkpointed graph.
        assert graph.calls[0][1] == {"configurable": {"thread_id": "thread-A"}}

    def test_lock_contention_raises(self) -> None:
        graph = RecordingGraph(checkpointer=MagicMock())
        lock = InProcessThreadLock()
        held = lock.acquire("agent", "thread-A")
        assert held is not None
        reg = _reg(graph, thread_id_factory=lambda m: "thread-A")
        with pytest.raises(ThreadContentionError):
            process_service_bus_message(reg, FakeServiceBusMessage(b"hi"), thread_lock=lock)
        # Graph never ran.
        assert graph.calls == []

    def test_graph_failure_reraised_and_observed(self) -> None:
        observer = RecordingObserver()
        reg = _reg(FailingGraph())
        with pytest.raises(RuntimeError, match="boom"):
            process_service_bus_message(
                reg,
                FakeServiceBusMessage(b"hi"),
                thread_lock=InProcessThreadLock(),
                observer=observer,
            )
        assert [e[0] for e in observer.events] == ["started", "failed"]

    def test_failure_releases_lock(self) -> None:
        lock = InProcessThreadLock()

        class FailingCheckpointedGraph:
            checkpointer = MagicMock()

            def invoke(self, input: Any, config: Any = None) -> Any:
                raise RuntimeError("boom")

        reg = _reg(FailingCheckpointedGraph(), thread_id_factory=lambda m: "thread-A")
        with pytest.raises(RuntimeError):
            process_service_bus_message(reg, FakeServiceBusMessage(b"hi"), thread_lock=lock)
        # Lock is free again — re-acquire succeeds.
        assert lock.acquire("agent", "thread-A") is not None

    def test_result_handler_called_on_success(self) -> None:
        seen: list[tuple[Any, Any]] = []
        graph = RecordingGraph(result={"answer": 42})
        reg = _reg(graph, result_handler=lambda r, m: seen.append((r, m.message_id)))
        process_service_bus_message(
            reg, FakeServiceBusMessage(b"hi"), thread_lock=InProcessThreadLock()
        )
        assert seen == [({"answer": 42}, "msg-1")]

    def test_result_handler_failure_propagates_without_second_terminal(self) -> None:
        observer = RecordingObserver()

        def boom(result: Any, msg: Any) -> None:
            raise ValueError("handler failed")

        reg = _reg(RecordingGraph(), result_handler=boom)
        with pytest.raises(ValueError, match="handler failed"):
            process_service_bus_message(
                reg,
                FakeServiceBusMessage(b"hi"),
                thread_lock=InProcessThreadLock(),
                observer=observer,
            )
        # Run already completed before the handler ran — exactly one terminal event.
        assert [e[0] for e in observer.events] == ["started", "completed"]


# ---------------------------------------------------------------------------
# process_service_bus_message_async
# ---------------------------------------------------------------------------


class TestProcessAsync:
    def test_async_success(self) -> None:
        graph = AsyncRecordingGraph()
        observer = RecordingObserver()
        reg = _reg(graph)
        result = asyncio.run(
            process_service_bus_message_async(
                reg,
                FakeServiceBusMessage(b"hi"),
                thread_lock=InProcessThreadLock(),
                observer=observer,
            )
        )
        assert result == {"ok": True}
        assert graph.calls[0][1] is None
        assert [e[0] for e in observer.events] == ["started", "completed"]

    def test_async_thread_id_locks_and_forwards_config(self) -> None:
        graph = AsyncRecordingGraph(checkpointer=MagicMock())
        lock = InProcessThreadLock()
        reg = _reg(graph, thread_id_factory=lambda m: "t")
        asyncio.run(
            process_service_bus_message_async(reg, FakeServiceBusMessage(b"hi"), thread_lock=lock)
        )
        assert graph.calls[0][1] == {"configurable": {"thread_id": "t"}}
        # Lock released — re-acquire works.
        assert lock.acquire("agent", "t") is not None

    def test_async_contention_raises(self) -> None:
        graph = AsyncRecordingGraph(checkpointer=MagicMock())
        lock = InProcessThreadLock()
        assert lock.acquire("agent", "t") is not None
        reg = _reg(graph, thread_id_factory=lambda m: "t")
        with pytest.raises(ThreadContentionError):
            asyncio.run(
                process_service_bus_message_async(
                    reg, FakeServiceBusMessage(b"hi"), thread_lock=lock
                )
            )

    def test_async_failure_reraised(self) -> None:
        class AsyncFailingGraph:
            checkpointer = None

            async def ainvoke(self, input: Any, config: Any = None) -> Any:
                raise RuntimeError("boom")

        observer = RecordingObserver()
        reg = _reg(AsyncFailingGraph())
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(
                process_service_bus_message_async(
                    reg,
                    FakeServiceBusMessage(b"hi"),
                    thread_lock=InProcessThreadLock(),
                    observer=observer,
                )
            )
        assert [e[0] for e in observer.events] == ["started", "failed"]

    def test_async_result_handler(self) -> None:
        seen: list[Any] = []
        reg = _reg(AsyncRecordingGraph(result={"a": 1}), result_handler=lambda r, m: seen.append(r))
        asyncio.run(
            process_service_bus_message_async(
                reg, FakeServiceBusMessage(b"hi"), thread_lock=InProcessThreadLock()
            )
        )
        assert seen == [{"a": 1}]


# ---------------------------------------------------------------------------
# LangGraphApp.register_service_bus
# ---------------------------------------------------------------------------


class TestRegisterServiceBus:
    def _app(self) -> LangGraphApp:
        return LangGraphApp(auth_level=func.AuthLevel.FUNCTION)

    def test_queue_registration_builds_function_app(self) -> None:
        app = self._app()
        app.register_service_bus(RecordingGraph(), "sb_agent", connection="SB", queue_name="q")
        # Building the function app wires the trigger without raising.
        assert app.function_app is not None
        assert "sb_agent" in app._sb_registrations

    def test_topic_registration_builds_function_app(self) -> None:
        app = self._app()
        app.register_service_bus(
            RecordingGraph(),
            "sb_topic",
            connection="SB",
            topic_name="t",
            subscription_name="s",
        )
        assert app.function_app is not None
        assert app._sb_registrations["sb_topic"].is_topic is True

    def test_async_graph_auto_detected(self) -> None:
        app = self._app()
        app.register_service_bus(AsyncRecordingGraph(), "sb_async", connection="SB", queue_name="q")
        assert app._sb_registrations["sb_async"].async_mode is True
        assert app.function_app is not None

    def test_explicit_async_mode(self) -> None:
        app = self._app()
        app.register_service_bus(
            AsyncRecordingGraph(), "sb_a", connection="SB", queue_name="q", async_mode=True
        )
        assert app._sb_registrations["sb_a"].async_mode is True

    def test_binding_kwargs_forwarded(self) -> None:
        app = self._app()
        app.register_service_bus(
            RecordingGraph(),
            "sb_b",
            connection="SB",
            queue_name="q",
            binding_kwargs={"is_sessions_enabled": True},
        )
        assert app.function_app is not None

    def test_queue_and_topic_both_rejected(self) -> None:
        app = self._app()
        with pytest.raises(ValueError, match="not both"):
            app.register_service_bus(
                RecordingGraph(), "x", connection="SB", queue_name="q", topic_name="t"
            )

    def test_neither_queue_nor_topic_rejected(self) -> None:
        app = self._app()
        with pytest.raises(ValueError, match="Provide queue_name"):
            app.register_service_bus(RecordingGraph(), "x", connection="SB")

    def test_topic_without_subscription_rejected(self) -> None:
        app = self._app()
        with pytest.raises(ValueError, match="requires both"):
            app.register_service_bus(RecordingGraph(), "x", connection="SB", topic_name="t")

    def test_duplicate_name_rejected(self) -> None:
        app = self._app()
        app.register_service_bus(RecordingGraph(), "dup", connection="SB", queue_name="q")
        with pytest.raises(ValueError, match="already registered"):
            app.register_service_bus(RecordingGraph(), "dup", connection="SB", queue_name="q2")

    def test_invalid_name_rejected(self) -> None:
        app = self._app()
        with pytest.raises(ValueError):
            app.register_service_bus(RecordingGraph(), "bad name!", connection="SB", queue_name="q")

    def test_async_mode_without_ainvoke_rejected(self) -> None:
        app = self._app()
        with pytest.raises(TypeError, match="ainvoke"):
            app.register_service_bus(
                RecordingGraph(), "x", connection="SB", queue_name="q", async_mode=True
            )

    def test_non_graph_rejected(self) -> None:
        app = self._app()
        with pytest.raises(TypeError, match="invoke"):
            app.register_service_bus(object(), "x", connection="SB", queue_name="q")

    def test_public_error_exported(self) -> None:
        import azure_functions_langgraph as pkg

        assert pkg.ThreadContentionError is ThreadContentionError
        assert pkg.default_message_mapper is default_message_mapper
