"""Transport ownership regressions for responses abandoned before iteration."""

import anyio
import pytest
from starlette.requests import Request
from starlette.types import Message

from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.streaming import StreamingLangGraphApp, _import_fastapi_extension
from tests.test_streaming_app import FakeAsyncGraph, FakeSyncGraph, RecordingObserver


def request() -> Request:
    async def receive() -> Message:
        return {
            "type": "http.request",
            "body": b'{"input":{},"config":{"configurable":{"thread_id":"t"}}}',
        }

    return Request({"type": "http", "headers": []}, receive)


@pytest.mark.parametrize("async_mode", [False, True])
async def test_abandoned_response_owns_no_lock_or_observer_span(async_mode: bool) -> None:
    # Given a checkpointed graph and a response the transport will never execute.
    lock = InProcessThreadLock()
    observer = RecordingObserver()
    app = StreamingLangGraphApp(thread_lock=lock, observer=observer)
    graph = FakeAsyncGraph(checkpointer=True) if async_mode else FakeSyncGraph(checkpointer=True)
    app.register(graph, "agent", async_mode=async_mode)
    ext = _import_fastapi_extension()

    # When the handler constructs, but nobody iterates or calls, the response.
    response = await app._handle_stream(
        request(),
        app._registrations["agent"],
        async_mode,
        ext.JSONResponse,
        ext.StreamingResponse,
        observer,
        lock,
    )

    # Then nothing needing asynchronous cleanup was acquired or started.
    token = lock.acquire("agent", "t")
    assert token is not None
    lock.release("agent", "t", token)
    assert observer.events == []
    await response.body_iterator.aclose()


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_transport_failure_before_body_releases_ownership(failure: str) -> None:
    # Given a real ASGI response whose header send fails before the body is pulled.
    lock = InProcessThreadLock()
    observer = RecordingObserver()
    app = StreamingLangGraphApp(thread_lock=lock, observer=observer)
    app.register(FakeSyncGraph(checkpointer=True), "agent")
    ext = _import_fastapi_extension()
    response = await app._handle_stream(
        request(),
        app._registrations["agent"],
        False,
        ext.JSONResponse,
        ext.StreamingResponse,
        observer,
        lock,
    )

    async def send(message: Message) -> None:
        assert message["type"] == "http.response.start"
        assert lock.acquire("agent", "t") is None
        if failure == "cancel":
            raise anyio.get_cancelled_exc_class()()
        raise RuntimeError("header send failed")

    async def receive() -> Message:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    # When ASGI fails before it ever iterates the response body.
    expected = anyio.get_cancelled_exc_class() if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    # Then the transport lifetime retires the token and observer exactly once.
    token = lock.acquire("agent", "t")
    assert token is not None
    lock.release("agent", "t", token)
    assert observer.events == ["started", "failed"]
