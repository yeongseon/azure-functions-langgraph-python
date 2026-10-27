"""Regression tests: StreamingLangGraphApp invoke/stream must not leak a
thread-lock token acquired after the caller was cancelled (#651).

Both handlers previously awaited ``asyncio.to_thread(thread_lock.acquire,
...)`` directly, with no cancellation-safe collection. Cancelling the
awaiting task could not stop the already-running synchronous acquire; the
worker would later return a token nothing ever collected or released, so
the graph never executed but the thread was left permanently 'locked' with
no owner tracking it. #620 already fixed the analogous native async-handler
path; this covers the separate StreamingLangGraphApp surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import json
import threading
from typing import Any
import warnings

import pytest

from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.streaming import StreamingLangGraphApp, _import_fastapi_extension

_ext = _import_fastapi_extension()
JSONResponse = _ext.JSONResponse
StreamingResponse = _ext.StreamingResponse

_WAIT_SECONDS = 2.0


class FakeRequest:
    def __init__(self, payload: Any) -> None:
        self.headers: dict[str, str] = {}
        self._raw = json.dumps(payload).encode("utf-8")

    async def body(self) -> bytes:
        return self._raw


class _ControlledAcquireLock:
    """Wraps a real InProcessThreadLock, but blocks inside acquire() on a
    controllable barrier -- simulating a slow synchronous acquisition that a
    caller may get cancelled while awaiting."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.finished = threading.Event()
        self.inner = InProcessThreadLock()
        self.release_calls: list[str] = []
        self.acquired_token: str | None = None

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.entered.set()
        if not self.proceed.wait(_WAIT_SECONDS):
            raise TimeoutError("acquire test synchronization failed")
        token = self.inner.acquire(graph_name, thread_id)
        self.acquired_token = token
        self.finished.set()
        return token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.inner.release(graph_name, thread_id, token)
        self.release_calls.append(token)


class _NeverCalledGraph:
    """Graph whose invoke/stream must never be called when cancellation
    happens during acquisition."""

    checkpointer = object()

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        return input

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        self.calls += 1
        yield input


def _app_with_lock(graph: Any, lock: Any, name: str = "agent") -> StreamingLangGraphApp:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        app = StreamingLangGraphApp(thread_lock=lock)
    app.register(graph=graph, name=name)
    return app


def _user_fn(app: StreamingLangGraphApp, fn_name: str) -> Any:
    fa = app.function_app
    fa.functions_bindings = {}
    for fn in fa.get_functions():
        if fn.get_function_name() == fn_name:
            return fn.get_user_function()
    raise AssertionError(f"function {fn_name!r} not found")


async def _wait(event: threading.Event) -> None:
    deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
    while not event.is_set():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("probe event deadline")
        await asyncio.sleep(0.001)


@pytest.mark.parametrize("fn_name", ["aflg_agent_invoke", "aflg_agent_stream"])
async def test_cancellation_during_acquire_collects_and_releases_token(fn_name: str) -> None:
    lock = _ControlledAcquireLock()
    graph = _NeverCalledGraph()
    app = _app_with_lock(graph, lock)
    handler = _user_fn(app, fn_name)

    req = FakeRequest({"input": {}, "config": {"configurable": {"thread_id": "thread-one"}}})

    async def execute() -> None:
        response = await handler(req)
        if fn_name == "aflg_agent_stream":
            await anext(response.body_iterator)

    task = asyncio.create_task(execute())
    await _wait(lock.entered)

    task.cancel()
    lock.proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT_SECONDS)
    await _wait(lock.finished)

    assert graph.calls == 0, "the graph must never execute for a cancelled acquisition"
    assert lock.release_calls == [lock.acquired_token], (
        "a token acquired after cancellation must be collected and released exactly once"
    )
    # The thread must now be free for a subsequent acquisition.
    second = lock.inner.acquire("agent", "thread-one")
    assert second is not None
    lock.inner.release("agent", "thread-one", second)


async def test_lock_contention_returns_409_without_releasing() -> None:
    class _AlwaysBusyLock:
        def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> None:
            return None

        def release(self, graph_name: str, thread_id: str, token: str) -> None:
            raise AssertionError("release must not be called for a contention (None) acquire")

    app = _app_with_lock(_NeverCalledGraph(), _AlwaysBusyLock())
    handler = _user_fn(app, "aflg_agent_invoke")

    resp = await handler(
        FakeRequest({"input": {}, "config": {"configurable": {"thread_id": "thread-one"}}})
    )

    assert resp.status_code == 409


async def test_successful_stream_handoff_keeps_lock_held_until_generator_releases() -> None:
    """On a normal (non-cancelled) stream request, ownership transfers to the
    SSE generator, which releases it on its own lifecycle -- not before."""
    lock = InProcessThreadLock()

    class _OneShotGraph:
        checkpointer = object()

        def invoke(
            self, input: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return input

        def stream(
            self,
            input: dict[str, Any],
            config: dict[str, Any] | None = None,
            stream_mode: str = "values",
        ) -> Iterator[dict[str, Any]]:
            # The lock must still be held while the generator is consumed.
            assert lock.acquire("agent", "thread-one") is None
            yield {"ok": True}

    app = _app_with_lock(_OneShotGraph(), lock)
    handler = _user_fn(app, "aflg_agent_stream")

    resp = await handler(
        FakeRequest({"input": {}, "config": {"configurable": {"thread_id": "thread-one"}}})
    )
    assert resp.status_code == 200

    body_iter = resp.body_iterator
    async for _ in body_iter:
        pass

    # After the generator is fully consumed, the lock must be released.
    token = lock.acquire("agent", "thread-one")
    assert token is not None
    lock.release("agent", "thread-one", token)
