"""Regression tests: buffered async stream must finish iterator cleanup
before the thread lock is released (#650).

``handle_stream_async`` breaks out of ``graph.astream()`` on byte-cap
overflow or an exception without explicitly awaiting the async generator's
``aclose()``. Garbage-collector-driven cleanup can then run after the
handler has already released the thread lock and returned, so a second
caller can acquire the same thread while the first graph's own ``finally``
block is still executing against stale state.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import threading
import types
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph._handlers import handle_stream_async

_WAIT_SECONDS = 2.0


class _SingleOwnerLock:
    """A real single-owner lock: a second acquire fails while the first
    owner's token is still held, unlike the fixed-token test double used
    elsewhere in this test suite."""

    def __init__(self) -> None:
        self._owner: str | None = None
        self._counter = 0
        self._guard = threading.Lock()
        self.release_calls: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        with self._guard:
            if self._owner is not None:
                return None
            self._counter += 1
            self._owner = f"owner-{self._counter}"
            return self._owner

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        with self._guard:
            if self._owner == token:
                self._owner = None
            self.release_calls.append(token)

    @property
    def owner(self) -> str | None:
        with self._guard:
            return self._owner


class _CleanupControlledGraph:
    """Graph whose astream() finally-block awaits a controllable event
    before finishing, so a test can observe whether the handler released
    the thread lock before or after that cleanup completes."""

    def __init__(self, *, fail_during_iteration: bool = False) -> None:
        self.checkpointer = types.SimpleNamespace()
        self.cleanup_started = asyncio.Event()
        self.cleanup_proceed = asyncio.Event()
        self.cleanup_finished = asyncio.Event()
        self.owner_seen_during_cleanup: str | None = None
        self.aclose_call_count = 0
        self._lock: _SingleOwnerLock | None = None
        self._fail_during_iteration = fail_during_iteration

    def attach_lock(self, lock: _SingleOwnerLock) -> None:
        self._lock = lock

    async def astream(
        self,
        graph_input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"payload": "x" * 1000}
            if self._fail_during_iteration:
                raise RuntimeError("boom")
        finally:
            self.cleanup_started.set()
            try:
                await asyncio.wait_for(self.cleanup_proceed.wait(), _WAIT_SECONDS)
                if self._lock is not None:
                    self.owner_seen_during_cleanup = self._lock.owner
            finally:
                self.cleanup_finished.set()


class _NoAcloseIterator:
    """Structural async iterator without an aclose() method, simulating a
    non-generator AsyncStreamableGraph implementation."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _NoAcloseIterator:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


class _NoAcloseGraph:
    checkpointer = types.SimpleNamespace()

    def astream(
        self,
        graph_input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> _NoAcloseIterator:
        return _NoAcloseIterator([{"ok": True}])


def _request(*, max_bytes_hint: str = "thread-one") -> func.HttpRequest:
    body = {
        "input": {"message": "x"},
        "config": {"configurable": {"thread_id": max_bytes_hint}},
    }
    return func.HttpRequest(
        method="POST",
        url="https://example.test/api/graphs/agent/stream",
        body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


async def _run_stream_handler(
    graph: Any, thread_lock: Any, *, max_stream_response_bytes: int = 1_000_000
) -> func.HttpResponse:
    reg = types.SimpleNamespace(name="agent", graph=graph, stream_enabled=True)
    return await handle_stream_async(
        _request(),
        reg,
        thread_lock=thread_lock,
        max_stream_response_bytes=max_stream_response_bytes,
        max_request_body_bytes=1_000_000,
        max_input_depth=20,
        max_input_nodes=1_000,
    )


async def test_byte_cap_break_finishes_cleanup_before_lock_release() -> None:
    lock = _SingleOwnerLock()
    graph = _CleanupControlledGraph()
    graph.attach_lock(lock)

    task = asyncio.create_task(_run_stream_handler(graph, lock, max_stream_response_bytes=64))
    await asyncio.wait_for(graph.cleanup_started.wait(), _WAIT_SECONDS)

    # A second same-thread writer must not be able to acquire the lock while
    # the first graph's cleanup is still pending.
    assert lock.acquire("agent", "thread-one") is None
    assert not task.done()

    graph.cleanup_proceed.set()
    response = await asyncio.wait_for(task, _WAIT_SECONDS)

    assert response.status_code == 200
    assert graph.cleanup_finished.is_set()
    assert graph.owner_seen_during_cleanup == lock.release_calls[0]
    assert lock.release_calls == [lock.release_calls[0]]
    # The lock must now be free for a subsequent acquisition.
    assert lock.acquire("agent", "thread-one") is not None


async def test_exception_during_iteration_finishes_cleanup_before_lock_release() -> None:
    lock = _SingleOwnerLock()
    graph = _CleanupControlledGraph(fail_during_iteration=True)
    graph.attach_lock(lock)

    task = asyncio.create_task(_run_stream_handler(graph, lock))
    await asyncio.wait_for(graph.cleanup_started.wait(), _WAIT_SECONDS)

    assert lock.acquire("agent", "thread-one") is None
    assert not task.done()

    graph.cleanup_proceed.set()
    response = await asyncio.wait_for(task, _WAIT_SECONDS)

    assert response.status_code == 200
    assert "stream processing failed" in response.get_body().decode()
    assert graph.cleanup_finished.is_set()
    assert len(lock.release_calls) == 1
    assert graph.owner_seen_during_cleanup == lock.release_calls[0]


async def test_normal_completion_finishes_cleanup_before_lock_release() -> None:
    lock = _SingleOwnerLock()
    graph = _CleanupControlledGraph()
    graph.attach_lock(lock)

    task = asyncio.create_task(_run_stream_handler(graph, lock))
    await asyncio.wait_for(graph.cleanup_started.wait(), _WAIT_SECONDS)
    assert lock.acquire("agent", "thread-one") is None

    graph.cleanup_proceed.set()
    response = await asyncio.wait_for(task, _WAIT_SECONDS)

    assert response.status_code == 200
    assert len(lock.release_calls) == 1


async def test_structural_iterator_without_aclose_is_a_safe_no_op() -> None:
    """A graph whose astream() returns a plain async iterator (no aclose())
    must not crash the handler."""
    lock = _SingleOwnerLock()
    graph = _NoAcloseGraph()

    response = await _run_stream_handler(graph, lock)

    assert response.status_code == 200
    assert lock.release_calls == list(lock.release_calls)  # released without raising


async def test_cancelled_stream_releases_owner_exactly_once() -> None:
    """Cancelling the handler mid-stream must still release the owner token
    exactly once, even while the graph's cleanup is pending."""
    lock = _SingleOwnerLock()
    graph = _CleanupControlledGraph()
    graph.attach_lock(lock)

    task = asyncio.create_task(_run_stream_handler(graph, lock))
    await asyncio.wait_for(graph.cleanup_started.wait(), _WAIT_SECONDS)

    task.cancel()
    graph.cleanup_proceed.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _WAIT_SECONDS)

    assert len(lock.release_calls) == 1
