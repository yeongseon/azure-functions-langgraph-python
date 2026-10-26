from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import types
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph._handlers import handle_invoke_async, handle_stream_async

_WAIT_SECONDS = 2.0


class _ControlledLock:
    def __init__(self, *, token: str | None = "owner-token") -> None:
        self.token = token
        self.acquire_entered = threading.Event()
        self.acquire_proceed = threading.Event()
        self.release_entered = threading.Event()
        self.release_proceed = threading.Event()
        self.release_proceed.set()
        self.release_calls: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.acquire_entered.set()
        if not self.acquire_proceed.wait(_WAIT_SECONDS):
            raise TimeoutError("acquire test synchronization failed")
        return self.token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.release_entered.set()
        if not self.release_proceed.wait(_WAIT_SECONDS):
            raise TimeoutError("release test synchronization failed")
        self.release_calls.append(token)


class _ControlledGraph:
    def __init__(self) -> None:
        self.checkpointer = types.SimpleNamespace()
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.calls = 0

    async def ainvoke(
        self, graph_input: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, bool]:
        self.calls += 1
        self.started.set()
        await self.proceed.wait()
        return {"ok": True}

    async def astream(
        self,
        graph_input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> AsyncIterator[dict[str, bool]]:
        self.calls += 1
        self.started.set()
        await self.proceed.wait()
        yield {"ok": True}


def _request(endpoint: str) -> func.HttpRequest:
    body = {
        "input": {"message": "safe test input"},
        "config": {"configurable": {"thread_id": "thread-1"}},
    }
    return func.HttpRequest(
        method="POST",
        url=f"https://example.test/api/graphs/agent/{endpoint}",
        body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


async def _invoke_handler(
    endpoint: str, graph: _ControlledGraph, thread_lock: _ControlledLock
) -> func.HttpResponse:
    reg = types.SimpleNamespace(name="agent", graph=graph, stream_enabled=True)
    if endpoint == "invoke":
        return await handle_invoke_async(
            _request(endpoint),
            reg,
            thread_lock=thread_lock,
            max_request_body_bytes=1_000_000,
            max_input_depth=20,
            max_input_nodes=1_000,
        )
    return await handle_stream_async(
        _request(endpoint),
        reg,
        thread_lock=thread_lock,
        max_stream_response_bytes=1_000_000,
        max_request_body_bytes=1_000_000,
        max_input_depth=20,
        max_input_nodes=1_000,
    )


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_cancelled_handler_releases_token_acquired_later(endpoint: str) -> None:
    """Cancellation must collect a late token without starting graph execution."""
    # Given a handler blocked inside synchronous lock acquisition.
    graph = _ControlledGraph()
    thread_lock = _ControlledLock()
    task = asyncio.create_task(_invoke_handler(endpoint, graph, thread_lock))
    assert await asyncio.to_thread(thread_lock.acquire_entered.wait, _WAIT_SECONDS)

    # When the handler is cancelled before acquisition completes.
    task.cancel()
    thread_lock.acquire_proceed.set()

    # Then cancellation propagates after the eventual owner token is released once.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT_SECONDS)
    assert thread_lock.release_calls == ["owner-token"]
    assert graph.calls == 0


async def test_cancelled_handler_before_worker_start_releases_eventual_token() -> None:
    """Queued worker acquisition still owns cleanup after its awaiting task is cancelled."""
    # Given the event loop's only worker is occupied before invoke queues acquisition.
    blocker_entered = threading.Event()
    blocker_proceed = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)

    def block_worker() -> None:
        blocker_entered.set()
        if not blocker_proceed.wait(_WAIT_SECONDS):
            raise TimeoutError("worker test synchronization failed")

    loop = asyncio.get_running_loop()
    loop.set_default_executor(executor)
    blocker = loop.run_in_executor(None, block_worker)
    assert blocker_entered.wait(_WAIT_SECONDS)
    graph = _ControlledGraph()
    thread_lock = _ControlledLock()
    thread_lock.acquire_proceed.set()
    task = asyncio.create_task(_invoke_handler("invoke", graph, thread_lock))

    # When cancellation arrives while acquire is queued but has not started.
    await asyncio.sleep(0)
    assert not thread_lock.acquire_entered.is_set()
    task.cancel()
    blocker_proceed.set()

    # Then queued acquisition is collected and its token is released exactly once.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT_SECONDS)
    await blocker
    assert thread_lock.release_calls == ["owner-token"]
    assert graph.calls == 0
    loop.set_default_executor(ThreadPoolExecutor())
    executor.shutdown(wait=True)


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_cancelled_running_handler_releases_lock_once(endpoint: str) -> None:
    # Given a handler that acquired its token and started the graph.
    graph = _ControlledGraph()
    thread_lock = _ControlledLock()
    thread_lock.acquire_proceed.set()
    task = asyncio.create_task(_invoke_handler(endpoint, graph, thread_lock))
    await asyncio.wait_for(graph.started.wait(), _WAIT_SECONDS)

    # When graph execution is cancelled.
    task.cancel()

    # Then the handler releases only its token and a later request can acquire.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT_SECONDS)
    assert thread_lock.release_calls == ["owner-token"]
    assert thread_lock.acquire("agent", "thread-1") == "owner-token"


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_repeated_cancellation_cannot_interrupt_lock_cleanup(endpoint: str) -> None:
    """A second cancellation must not abandon an already-owned release operation."""
    # Given cancellation has begun a deliberately blocked release.
    graph = _ControlledGraph()
    thread_lock = _ControlledLock()
    thread_lock.acquire_proceed.set()
    thread_lock.release_proceed.clear()
    task = asyncio.create_task(_invoke_handler(endpoint, graph, thread_lock))
    await asyncio.wait_for(graph.started.wait(), _WAIT_SECONDS)
    task.cancel()
    assert await asyncio.to_thread(thread_lock.release_entered.wait, _WAIT_SECONDS)

    # When cancellation is requested again during cleanup.
    task.cancel()
    thread_lock.release_proceed.set()

    # Then cleanup still completes exactly once before cancellation propagates.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT_SECONDS)
    assert thread_lock.release_calls == ["owner-token"]


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_lock_contention_never_releases_invalid_token(endpoint: str) -> None:
    # Given acquisition completes without an owner token.
    graph = _ControlledGraph()
    thread_lock = _ControlledLock(token=None)
    thread_lock.acquire_proceed.set()

    # When the actual async handler processes the request.
    response = await _invoke_handler(endpoint, graph, thread_lock)

    # Then the existing contention contract remains and no release occurs.
    assert response.status_code == 409
    assert thread_lock.release_calls == []
    assert graph.calls == 0


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_acquisition_exception_is_propagated(endpoint: str) -> None:
    # Given a protocol-compatible lock whose acquisition fails.
    graph = _ControlledGraph()

    class _FailingLock(_ControlledLock):
        def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
            raise RuntimeError("acquire failed")

    # When the actual async handler awaits acquisition, then the failure is retrieved.
    with pytest.raises(RuntimeError, match="acquire failed"):
        await _invoke_handler(endpoint, graph, _FailingLock())

    # Then graph execution never starts.
    assert graph.calls == 0


async def test_cancelled_acquisition_exception_is_collected() -> None:
    # Given acquisition is blocked and will fail after cancellation.
    graph = _ControlledGraph()

    class _FailingAfterCancellationLock(_ControlledLock):
        def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
            self.acquire_entered.set()
            if not self.acquire_proceed.wait(_WAIT_SECONDS):
                raise TimeoutError("acquire test synchronization failed")
            raise RuntimeError("late acquire failure")

    thread_lock = _FailingAfterCancellationLock()
    task = asyncio.create_task(_invoke_handler("invoke", graph, thread_lock))
    assert await asyncio.to_thread(thread_lock.acquire_entered.wait, _WAIT_SECONDS)

    # When cancellation wins the race with the acquisition failure.
    task.cancel()
    await asyncio.sleep(0)
    thread_lock.acquire_proceed.set()

    # Then cancellation propagates only after the worker exception is retrieved.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT_SECONDS)
    assert thread_lock.release_calls == []
    assert graph.calls == 0
