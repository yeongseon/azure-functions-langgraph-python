"""release_thread_lock keeps a cancellation when its release then raises (#960)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import json
import logging
import threading
import types
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph._handlers import handle_invoke_async
from azure_functions_langgraph.locks._async import async_thread_lock, release_thread_lock

_SECRET = "secret-sentinel /private/path"
_LIMITS: dict[str, Any] = {
    "max_request_body_bytes": 1_000_000,
    "max_input_depth": 20,
    "max_input_nodes": 1_000,
}


class _Stop(BaseException):
    pass


class _Lock:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.entered = threading.Event()
        self.go = threading.Event()
        self.finished = threading.Event()
        self.releases: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        return "owner-token"

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.releases.append(token)
        self.entered.set()
        self.go.wait(5)
        self.finished.set()
        if self.error is not None:
            raise self.error


class _Graph:
    checkpointer = types.SimpleNamespace()

    async def ainvoke(self, graph_input: Any, config: Any = None) -> dict[str, bool]:
        return {"ok": True}


def _request() -> func.HttpRequest:
    body = {"input": {"m": "x"}, "config": {"configurable": {"thread_id": "t1"}}}
    return func.HttpRequest(
        method="POST",
        url="https://example.test/api/graphs/agent/invoke",
        body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


async def _cancel_during_release(
    run: Coroutine[Any, Any, Any], lock: _Lock, *, cancels: int = 1
) -> asyncio.Task[Any]:
    task = asyncio.create_task(run)
    await asyncio.to_thread(lock.entered.wait, 5)
    for _ in range(cancels):
        task.cancel()
        await asyncio.sleep(0.02)
    lock.go.set()
    return task


async def test_a_cancellation_is_kept_when_the_release_then_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a release that is running when the caller is cancelled, and that then raises
    lock = _Lock(RuntimeError(_SECRET))

    # When the caller is cancelled during the release
    with caplog.at_level(logging.WARNING):
        task = await _cancel_during_release(
            release_thread_lock(lock, "agent", "t1", "owner-token"), lock
        )
        with pytest.raises(asyncio.CancelledError):
            await task

    # Then the cancellation is what propagates, after the worker finished, and the failure is logged
    assert task.cancelled()
    assert lock.finished.is_set() and lock.releases == ["owner-token"]
    logged = [r for r in caplog.records if r.exc_info is not None and r.exc_info[1] is not None]
    assert [type(r.exc_info[1]) for r in logged if r.exc_info] == [RuntimeError]
    assert "release failed after caller cancellation" in logged[0].getMessage()


async def test_repeated_cancellation_still_waits_for_the_failing_release() -> None:
    # Given a failing release and a caller that is cancelled more than once
    lock = _Lock(RuntimeError(_SECRET))

    # When the caller is cancelled twice while the release is running
    task = await _cancel_during_release(
        release_thread_lock(lock, "agent", "t1", "owner-token"), lock, cancels=2
    )
    with pytest.raises(asyncio.CancelledError):
        await task

    # Then the release was neither abandoned nor repeated
    assert lock.finished.is_set() and lock.releases == ["owner-token"]


async def test_a_base_exception_from_the_release_is_not_replaced_by_the_cancellation() -> None:
    # Given a release that raises a BaseException after the caller was cancelled
    lock = _Lock(_Stop())

    # When the caller is cancelled during the release
    task = await _cancel_during_release(
        release_thread_lock(lock, "agent", "t1", "owner-token"), lock
    )

    # Then only ordinary exceptions give way to the cancellation: the BaseException propagates
    with pytest.raises(_Stop):
        await task
    assert lock.releases == ["owner-token"]


async def test_a_cancellation_during_a_successful_release_is_unchanged() -> None:
    # Given a release that succeeds
    lock = _Lock()

    # When the caller is cancelled during it
    task = await _cancel_during_release(
        release_thread_lock(lock, "agent", "t1", "owner-token"), lock
    )

    # Then the cancellation propagates as before
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock.releases == ["owner-token"]


async def test_a_failing_release_without_a_cancellation_still_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a failing release and a caller that is never cancelled
    lock = _Lock(RuntimeError(_SECRET))
    lock.go.set()

    # When the release runs
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="secret-sentinel"):
        await release_thread_lock(lock, "agent", "t1", "owner-token")

    # Then the exception propagates unchanged and the cancellation-path warning is not logged
    assert lock.releases == ["owner-token"]
    assert caplog.records == []


@pytest.mark.parametrize("with_callback", [True, False])
async def test_async_thread_lock_keeps_a_cancellation_during_a_failing_release(
    with_callback: bool,
) -> None:
    # Given a scope whose work succeeds and whose release fails while the caller is cancelled
    lock = _Lock(RuntimeError(_SECRET))
    seen: list[Exception] = []

    async def scope() -> None:
        callback = seen.append if with_callback else None
        async with async_thread_lock(lock, "agent", "t1", on_release_error=callback):
            pass

    # When the caller is cancelled during the release
    task = await _cancel_during_release(scope(), lock)

    # Then the cancellation propagates, and a cancellation is never passed to the callback
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [] and lock.releases == ["owner-token"]


async def test_a_native_async_handler_is_cancelled_not_answered_when_the_release_fails() -> None:
    # Given a run that succeeded and whose lock release fails while the caller is cancelled
    lock = _Lock(RuntimeError(_SECRET))
    reg = types.SimpleNamespace(name="agent", graph=_Graph(), stream_enabled=True)

    # When the caller is cancelled during the release
    task = await _cancel_during_release(
        handle_invoke_async(_request(), reg, thread_lock=lock, **_LIMITS), lock
    )

    # Then the handler is cancelled instead of returning the release-failure 500
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock.releases == ["owner-token"]
