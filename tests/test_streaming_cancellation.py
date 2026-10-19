from __future__ import annotations

import asyncio
from collections.abc import Iterator
import threading
from typing import Any

import pytest

from azure_functions_langgraph.observability import RunContext, new_run_context
from azure_functions_langgraph.streaming import _sse_event_stream

_WAIT_SECONDS = 2.0


class _BarrierIterator(Iterator[dict[str, int]]):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.close_raced = False

    def __iter__(self) -> _BarrierIterator:
        return self

    def __next__(self) -> dict[str, int]:
        self.entered.set()
        if not self.proceed.wait(_WAIT_SECONDS):
            raise TimeoutError("iterator test synchronization failed")
        self.finished.set()
        return {"frame": 1}

    def close(self) -> None:
        self.close_raced = not self.finished.is_set()
        self.closed.set()


class _BarrierGraph:
    checkpointer = object()

    def __init__(self, iterator: _BarrierIterator) -> None:
        self.iterator = iterator

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, int]]:
        return self.iterator


class _BetweenFramesIterator(_BarrierIterator):
    def __init__(self) -> None:
        super().__init__()
        self.pulls = 0

    def __next__(self) -> dict[str, int]:
        self.pulls += 1
        if self.pulls == 1:
            return {"frame": 1}
        return super().__next__()


class _RecordingLock:
    def __init__(self) -> None:
        self.release_entered = threading.Event()
        self.release_proceed = threading.Event()
        self.release_proceed.set()
        self.release_calls = 0

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str:
        return "owner-token"

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.release_calls += 1
        self.release_entered.set()
        if not self.release_proceed.wait(_WAIT_SECONDS):
            raise TimeoutError("release test synchronization failed")


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append("started")

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append("completed")

    def on_run_failed(self, ctx: RunContext, error: BaseException) -> None:
        self.events.append("failed")

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:
        self.events.append("rejected")


def _stream(
    graph: Any,
    lock: _RecordingLock,
    observer: _RecordingObserver,
) -> Any:
    return _sse_event_stream(
        graph=graph,
        graph_name="agent",
        input_={},
        config={},
        stream_mode="values",
        version_kwargs={},
        thread_id="thread-1",
        lock_token="owner-token",
        thread_lock=lock,
        observer=observer,
        ctx=new_run_context("agent", "stream", transport="streaming"),
        max_stream_events=10,
    )


async def _next_frame(stream: Any) -> str:
    return await anext(stream)


async def test_cancel_during_sync_pull_quiesces_before_close_and_release() -> None:
    # Given a real SSE stream blocked in its synchronous iterator's first next().
    iterator = _BarrierIterator()
    lock = _RecordingLock()
    observer = _RecordingObserver()
    stream = _stream(_BarrierGraph(iterator), lock, observer)
    pull = asyncio.create_task(_next_frame(stream))
    assert await asyncio.to_thread(iterator.entered.wait, _WAIT_SECONDS)

    # When cancellation arrives while that worker still owns graph execution.
    pull.cancel()
    await asyncio.sleep(0)

    # Then neither iterator cleanup nor lock release races the running worker.
    assert not iterator.closed.is_set()
    assert not lock.release_entered.is_set()
    iterator.proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pull, _WAIT_SECONDS)
    assert iterator.finished.is_set()
    assert iterator.closed.is_set()
    assert iterator.close_raced is False
    assert lock.release_calls == 1
    assert observer.events == ["failed"]


async def test_explicit_close_quiesces_sync_iterator_and_releases_once() -> None:
    # Given a stream whose first synchronous pull can complete.
    iterator = _BarrierIterator()
    iterator.proceed.set()
    lock = _RecordingLock()
    observer = _RecordingObserver()
    stream = _stream(_BarrierGraph(iterator), lock, observer)
    assert (await anext(stream)).startswith("event: data")

    # When the response consumer explicitly closes between frames.
    await stream.aclose()

    # Then the iterator closes before one release and the observer sees failure.
    assert iterator.closed.is_set()
    assert iterator.close_raced is False
    assert lock.release_calls == 1
    assert observer.events == ["failed"]


async def test_cancel_between_frames_waits_for_next_pull_before_release() -> None:
    # Given one frame was delivered and the next synchronous pull is blocked.
    iterator = _BetweenFramesIterator()
    lock = _RecordingLock()
    observer = _RecordingObserver()
    stream = _stream(_BarrierGraph(iterator), lock, observer)
    assert (await anext(stream)).startswith("event: data")
    pull = asyncio.create_task(_next_frame(stream))
    assert await asyncio.to_thread(iterator.entered.wait, _WAIT_SECONDS)

    # When cancellation arrives between delivered frames.
    pull.cancel()
    await asyncio.sleep(0)

    # Then lock release waits for the active pull and safe iterator close.
    assert not lock.release_entered.is_set()
    iterator.proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pull, _WAIT_SECONDS)
    assert iterator.closed.is_set()
    assert iterator.close_raced is False
    assert lock.release_calls == 1
    assert observer.events == ["failed"]


async def test_repeated_cancellation_cannot_interrupt_stream_release() -> None:
    # Given cancellation has quiesced the iterator and entered a blocked release.
    iterator = _BarrierIterator()
    lock = _RecordingLock()
    lock.release_proceed.clear()
    observer = _RecordingObserver()
    pull = asyncio.create_task(_next_frame(_stream(_BarrierGraph(iterator), lock, observer)))
    assert await asyncio.to_thread(iterator.entered.wait, _WAIT_SECONDS)
    pull.cancel()
    iterator.proceed.set()
    assert await asyncio.to_thread(lock.release_entered.wait, _WAIT_SECONDS)

    # When cancellation is requested again during token cleanup.
    pull.cancel()
    lock.release_proceed.set()

    # Then cleanup completes exactly once before cancellation propagates.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pull, _WAIT_SECONDS)
    assert lock.release_calls == 1
    assert observer.events == ["failed"]


async def test_async_graph_cancellation_closes_without_sync_worker_wait() -> None:
    # Given an async graph blocked before its first frame.
    entered = asyncio.Event()
    closed = asyncio.Event()

    class _AsyncGraph:
        async def astream(
            self,
            input: dict[str, Any],
            config: dict[str, Any] | None = None,
            stream_mode: str = "values",
        ) -> Any:
            try:
                entered.set()
                await asyncio.Event().wait()
                yield {"frame": 1}
            finally:
                closed.set()

    lock = _RecordingLock()
    observer = _RecordingObserver()
    stream = _sse_event_stream(
        graph=_AsyncGraph(),
        graph_name="agent",
        input_={},
        config={},
        stream_mode="values",
        version_kwargs={},
        thread_id="thread-1",
        lock_token="owner-token",
        thread_lock=lock,
        observer=observer,
        ctx=new_run_context("agent", "stream", transport="streaming"),
        max_stream_events=10,
        async_mode=True,
    )
    pull = asyncio.create_task(_next_frame(stream))
    await asyncio.wait_for(entered.wait(), _WAIT_SECONDS)

    # When the async path is cancelled.
    pull.cancel()

    # Then native async cleanup runs and cancellation propagates without a thread bridge.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pull, _WAIT_SECONDS)
    assert closed.is_set()
    assert lock.release_calls == 1
    assert observer.events == ["failed"]


async def test_async_graph_finally_raise_still_releases_lock_once() -> None:
    """An async graph whose astream() finally-block raises during cleanup
    (e.g. on the max_stream_events cap exit) must not skip lock release;
    the token must be released exactly once and the stream must still
    terminate with an 'event: end' frame rather than an escaped
    exception (#656)."""

    class _RaisingCleanupAsyncGraph:
        async def astream(
            self,
            input: dict[str, Any],
            config: dict[str, Any] | None = None,
            stream_mode: str = "values",
        ) -> Any:
            try:
                yield {"frame": 1}
                yield {"frame": 2}
            finally:
                raise RuntimeError("dummy graph cleanup failure")

    lock = _RecordingLock()
    observer = _RecordingObserver()
    stream = _sse_event_stream(
        graph=_RaisingCleanupAsyncGraph(),
        graph_name="agent",
        input_={},
        config={},
        stream_mode="values",
        version_kwargs={},
        thread_id="thread-1",
        lock_token="owner-token",
        thread_lock=lock,
        observer=observer,
        ctx=new_run_context("agent", "stream", transport="streaming"),
        max_stream_events=1,
        async_mode=True,
    )
    frames = [frame async for frame in stream]

    assert any(frame.startswith("event: data") for frame in frames)
    assert any(frame.startswith("event: error") for frame in frames)
    assert frames[-1] == "event: end\ndata: {}\n\n"
    assert lock.release_calls == 1
    assert observer.events == ["failed"]


async def test_sync_iterator_close_raise_still_releases_lock_once() -> None:
    """A synchronous iterator whose close() raises must not skip lock
    release either; same exactly-once release and terminal framing
    guarantee as the async case (#656)."""

    class _RaisingCloseIterator(Iterator[dict[str, int]]):
        def __init__(self) -> None:
            self.pulls = 0

        def __iter__(self) -> _RaisingCloseIterator:
            return self

        def __next__(self) -> dict[str, int]:
            self.pulls += 1
            if self.pulls == 1:
                return {"frame": 1}
            raise StopIteration

        def close(self) -> None:
            raise RuntimeError("dummy iterator close failure")

    class _RaisingCloseGraph:
        checkpointer = object()

        def __init__(self, iterator: _RaisingCloseIterator) -> None:
            self.iterator = iterator

        def stream(
            self,
            input: dict[str, Any],
            config: dict[str, Any] | None = None,
            stream_mode: str = "values",
        ) -> Iterator[dict[str, int]]:
            return self.iterator

    iterator = _RaisingCloseIterator()
    lock = _RecordingLock()
    observer = _RecordingObserver()
    stream = _stream(_RaisingCloseGraph(iterator), lock, observer)
    frames = [frame async for frame in stream]

    assert any(frame.startswith("event: data") for frame in frames)
    assert frames[-1] == "event: end\ndata: {}\n\n"
    assert lock.release_calls == 1
    assert observer.events == ["failed"]
