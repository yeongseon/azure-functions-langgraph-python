"""A cancellation during a pending lock acquisition survives a failing cleanup release."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
from azure_functions_langgraph.locks._async import acquire_thread_lock
from tests.test_durable import FakeAsyncGraph, FakeReg, RecordingObserver, _payload


class _BlockingLock:
    """Acquisition blocks in its worker thread until released; the release then fails."""

    def __init__(self, release_error: Exception | None) -> None:
        self.release_error = release_error
        self.started = threading.Event()
        self.go = threading.Event()
        self.releases: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.started.set()
        self.go.wait(5)
        return "owner-token"

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.releases.append(token)
        if self.release_error is not None:
            raise self.release_error


async def _cancel_while_acquiring(task: asyncio.Task[object], lock: _BlockingLock) -> None:
    await asyncio.to_thread(lock.started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    lock.go.set()


@pytest.mark.parametrize("release_error", [None, OSError("backend release failed")])
async def test_the_cancellation_wins_over_a_failing_cleanup_release(
    release_error: Exception | None, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a caller cancelled while the worker thread is still acquiring the lock.
    lock = _BlockingLock(release_error)
    task = asyncio.create_task(acquire_thread_lock(lock, "g", "t1"))
    with caplog.at_level(logging.ERROR):
        await _cancel_while_acquiring(task, lock)
        # When the acquisition completes and its cleanup release raises.
        # Then the caller still sees the cancellation, and the token was released exactly once.
        with pytest.raises(asyncio.CancelledError):
            await task
    assert lock.releases == ["owner-token"]
    failed = [r for r in caplog.records if "release failed after caller cancellation" in r.message]
    assert len(failed) == (1 if release_error is not None else 0)
    if release_error is not None:
        record = failed[0]
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None
        assert record.exc_info[1] is release_error
        assert record.exc_info[2] is not None
        assert record.__dict__["graph_name"] == "g"
        assert record.__dict__["thread_id"] == "t1"


async def test_the_activity_is_cancelled_not_failed_when_cleanup_release_raises() -> None:
    # Given a Durable Activity cancelled by its host during a blocked lock acquisition.
    lock = _BlockingLock(OSError("backend release failed"))
    observer = RecordingObserver()
    registration = FakeReg(FakeAsyncGraph(checkpointer=object()), "echo")
    task = asyncio.create_task(
        execute_langgraph_run_impl(
            _payload(thread_id="t1"),
            registry={"echo": registration},
            thread_lock=lock,
            observer=observer,
        )
    )
    await _cancel_while_acquiring(task, lock)
    # When the cleanup release of the late acquisition raises.
    # Then the Activity is cancelled instead of returning a normal failure result.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock.releases == ["owner-token"]
    assert observer.events == ["failed"]
