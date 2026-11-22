"""Native async invoke and buffered stream report a raising lock release as a failed run (#958)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import logging
import types
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph._handlers import handle_invoke_async, handle_stream_async
from azure_functions_langgraph.locks._async import async_thread_lock
from azure_functions_langgraph.observability import RunContext, RunRejectedReason

_SECRET = "secret-sentinel /private/path"
_GRAPH_SECRET = "graph-secret-sentinel"
_LIMITS: dict[str, Any] = {
    "max_request_body_bytes": 1_000_000,
    "max_input_depth": 20,
    "max_input_nodes": 1_000,
}
_KINDS = ["invoke", "stream"]


class _Stop(BaseException):
    pass


class _Lock:
    def __init__(
        self, release_error: BaseException | None = None, *, token: str | None = "owner-token"
    ) -> None:
        self.release_error = release_error
        self.token = token
        self.acquires = 0
        self.releases: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.acquires += 1
        return self.token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.releases.append(token)
        if self.release_error is not None:
            raise self.release_error


class _Observer:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.failed: list[tuple[RunContext, BaseException]] = []
        self.reasons: list[RunRejectedReason] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append("started")

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append("completed")

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self.events.append("failed")
        self.failed.append((ctx, exc))

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None:
        self.events.append("rejected")
        self.reasons.append(reason)


class _Graph:
    def __init__(self, error: BaseException | None = None, *, checkpointer: bool = True) -> None:
        self.checkpointer = types.SimpleNamespace() if checkpointer else None
        self.error = error
        self.calls = 0

    async def ainvoke(self, graph_input: Any, config: Any = None) -> dict[str, bool]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"ok": True}

    async def astream(
        self, graph_input: Any, config: Any = None, stream_mode: str = "values"
    ) -> AsyncIterator[dict[str, int]]:
        self.calls += 1
        yield {"step": 1}
        if self.error is not None:
            raise self.error
        yield {"step": 2}


class _BlockingGraph(_Graph):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def ainvoke(self, graph_input: Any, config: Any = None) -> dict[str, bool]:
        self.started.set()
        await asyncio.Event().wait()
        return {"ok": True}

    async def astream(
        self, graph_input: Any, config: Any = None, stream_mode: str = "values"
    ) -> AsyncIterator[dict[str, int]]:
        self.started.set()
        await asyncio.Event().wait()
        yield {"step": 1}


def _request(endpoint: str) -> func.HttpRequest:
    body = {"input": {"m": "x"}, "config": {"configurable": {"thread_id": "t1"}}}
    return func.HttpRequest(
        method="POST",
        url=f"https://example.test/api/graphs/agent/{endpoint}",
        body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


async def _call(
    kind: str, graph: _Graph, lock: _Lock, observer: _Observer, *, stream_cap: int = 1_000_000
) -> func.HttpResponse:
    reg = types.SimpleNamespace(name="agent", graph=graph, stream_enabled=True)
    common: dict[str, Any] = {"thread_lock": lock, "observer": observer, **_LIMITS}
    if kind == "invoke":
        return await handle_invoke_async(_request("invoke"), reg, **common)
    return await handle_stream_async(
        _request("stream"), reg, max_stream_response_bytes=stream_cap, **common
    )


@pytest.mark.parametrize("kind", _KINDS)
async def test_raising_release_after_a_successful_run_returns_the_standard_500(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a graph that succeeds and a lock backend whose release raises with a secret
    graph, lock, observer = _Graph(), _Lock(RuntimeError(_SECRET)), _Observer()

    # When the async handler runs
    with caplog.at_level(logging.ERROR):
        response = await _call(kind, graph, lock, observer)

    # Then the caller gets the standard JSON 500, not the computed result or an SSE success
    assert response.status_code == 500
    assert response.mimetype == "application/json"
    payload = json.loads(response.get_body())
    assert payload["error"] == "error"
    assert payload["detail"] == "Thread lock could not be released"
    assert payload["request_id"]
    assert response.headers["X-Request-ID"] == payload["request_id"]
    visible = response.get_body().decode() + repr(dict(response.headers))
    assert "secret-sentinel" not in visible and "/private/path" not in visible
    assert '"ok"' not in visible and '"step"' not in visible
    # And the run is reported as started then failed, once each, with the release error
    assert observer.events == ["started", "failed"]
    ctx, exc = observer.failed[0]
    assert isinstance(exc, RuntimeError) and str(exc) == _SECRET
    assert ctx.ended_at_ns is not None
    # And the lock was neither re-acquired nor released again, and the traceback was logged
    assert (lock.acquires, lock.releases, graph.calls) == (1, ["owner-token"], 1)
    logged = [r.exc_info[1] for r in caplog.records if r.exc_info is not None and r.exc_info[1]]
    assert [type(e) for e in logged] == [RuntimeError]


@pytest.mark.parametrize("kind", _KINDS)
async def test_a_failed_graph_keeps_its_own_error_when_the_release_also_raises(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a graph that fails and a lock backend whose release raises as well
    graph = _Graph(ValueError(_GRAPH_SECRET))
    lock, observer = _Lock(RuntimeError(_SECRET)), _Observer()

    # When the async handler runs
    with caplog.at_level(logging.ERROR):
        response = await _call(kind, graph, lock, observer)

    # Then the graph's failure is what is reported, never a success
    visible = response.get_body().decode() + repr(dict(response.headers))
    assert "secret-sentinel" not in visible and _GRAPH_SECRET not in visible
    if kind == "invoke":
        assert response.status_code == 500
        assert json.loads(response.get_body())["detail"] == "Graph execution failed"
    else:
        assert response.mimetype == "text/event-stream"
        assert "event: error" in response.get_body().decode()
    # And the observer saw one started and one failed event carrying the GRAPH error
    assert observer.events == ["started", "failed"]
    assert isinstance(observer.failed[0][1], ValueError)
    # And release was attempted exactly once, and both tracebacks reached the log
    assert (lock.acquires, lock.releases) == (1, ["owner-token"])
    logged = {type(r.exc_info[1]) for r in caplog.records if r.exc_info is not None}
    assert logged == {ValueError, RuntimeError}


async def test_a_stream_size_cap_error_is_kept_when_the_release_raises() -> None:
    # Given a stream that exceeds the buffered size cap and a lock whose release raises
    graph, lock, observer = _Graph(), _Lock(RuntimeError(_SECRET)), _Observer()

    # When the async stream handler runs
    response = await _call("stream", graph, lock, observer, stream_cap=10)

    # Then the size-cap error frame is the outcome, not the release failure
    body = response.get_body().decode()
    assert response.mimetype == "text/event-stream"
    assert "stream response exceeded max buffered size" in body
    assert "secret-sentinel" not in body
    # And the observer saw the cap error once, and the release was still attempted once
    assert observer.events == ["started", "failed"]
    assert "exceeded max buffered size" in str(observer.failed[0][1])
    assert lock.releases == ["owner-token"]


@pytest.mark.parametrize("kind", _KINDS)
async def test_a_clean_run_and_release_are_unchanged(kind: str) -> None:
    # Given a graph and a lock that both succeed
    graph, lock, observer = _Graph(), _Lock(), _Observer()

    # When the async handler runs
    response = await _call(kind, graph, lock, observer)

    # Then it is the unchanged success
    assert response.status_code == 200
    assert response.headers["X-Request-ID"]
    assert observer.events == ["started", "completed"]
    assert lock.releases == ["owner-token"]
    if kind == "invoke":
        assert json.loads(response.get_body())["output"] == {"ok": True}
    else:
        body = response.get_body().decode()
        assert body.count("event: data") == 2 and body.endswith("event: end\ndata: {}\n\n")


@pytest.mark.parametrize("kind", _KINDS)
async def test_lock_contention_still_returns_409_without_a_release(kind: str) -> None:
    # Given a lock held elsewhere (acquire returns None) whose release would raise
    graph, lock, observer = _Graph(), _Lock(RuntimeError(_SECRET), token=None), _Observer()

    # When the async handler runs
    response = await _call(kind, graph, lock, observer)

    # Then it is the unchanged 409 rejection and nothing was released or executed
    assert response.status_code == 409
    assert observer.events == ["rejected"] and observer.reasons == ["lock_contention"]
    assert (lock.releases, graph.calls) == ([], 0)


@pytest.mark.parametrize("kind", _KINDS)
async def test_a_graph_without_a_checkpointer_takes_no_lock(kind: str) -> None:
    # Given a graph without a checkpointer and a lock whose release would raise
    graph, lock, observer = _Graph(checkpointer=False), _Lock(RuntimeError(_SECRET)), _Observer()

    # When the async handler runs
    response = await _call(kind, graph, lock, observer)

    # Then the run succeeds and the lock was never touched
    assert response.status_code == 200
    assert (lock.acquires, lock.releases) == (0, [])
    assert observer.events == ["started", "completed"]


@pytest.mark.parametrize("kind", _KINDS)
async def test_cancelling_the_run_is_not_masked_by_a_raising_release(kind: str) -> None:
    # Given a run that is still executing and a release that raises an ordinary error
    graph, lock = _BlockingGraph(), _Lock(RuntimeError(_SECRET))
    task = asyncio.create_task(_call(kind, graph, lock, _Observer()))
    await asyncio.wait_for(graph.started.wait(), timeout=5)

    # When the caller cancels the run
    task.cancel()

    # Then the cancellation still propagates (the release error is logged, not raised over it)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock.releases == ["owner-token"]


@pytest.mark.parametrize("kind", _KINDS)
async def test_a_base_exception_raised_by_the_release_is_not_swallowed(kind: str) -> None:
    # Given a release that raises a BaseException
    lock = _Lock(_Stop())

    # When the async handler runs
    # Then it propagates: only ordinary exceptions are converted to a failed run
    with pytest.raises(_Stop):
        await _call(kind, _Graph(), lock, _Observer())
    assert lock.releases == ["owner-token"]


async def test_async_thread_lock_still_raises_a_release_failure_by_default() -> None:
    # Given a lock whose release raises and no callback (what Durable, Service Bus and the
    # true-streaming surface rely on)
    lock = _Lock(RuntimeError(_SECRET))

    # When the scope exits
    # Then the failure propagates exactly as before
    with pytest.raises(RuntimeError, match="secret-sentinel"):
        async with async_thread_lock(lock, "agent", "t1") as token:
            assert token == "owner-token"
    assert lock.releases == ["owner-token"]


async def test_async_thread_lock_default_keeps_the_release_failure_over_a_failing_body() -> None:
    # Given a lock whose release raises, no callback, and protected work that fails first
    lock = _Lock(RuntimeError(_SECRET))

    # When the scope exits
    # Then the release failure propagates as before, with the work's failure as its context
    with pytest.raises(RuntimeError, match="secret-sentinel") as raised:
        async with async_thread_lock(lock, "agent", "t1"):
            raise ValueError("work failed")
    assert isinstance(raised.value.__context__, ValueError)
    assert lock.releases == ["owner-token"]


async def test_async_thread_lock_passes_a_release_failure_to_the_callback_instead() -> None:
    # Given a lock whose release raises and a callback that records it
    lock = _Lock(RuntimeError(_SECRET))
    seen: list[Exception] = []

    # When the scope exits normally
    async with async_thread_lock(lock, "agent", "t1", on_release_error=seen.append):
        pass

    # Then nothing is raised and the callback received the failure
    assert [str(e) for e in seen] == [_SECRET]

    # And a failure of the protected work still propagates over a callback-handled release error
    with pytest.raises(ValueError, match="work failed"):
        async with async_thread_lock(lock, "agent", "t1", on_release_error=seen.append):
            raise ValueError("work failed")
    assert len(seen) == 2


async def test_async_thread_lock_never_passes_a_base_exception_to_the_callback() -> None:
    # Given a release that raises a BaseException and a callback
    lock = _Lock(_Stop())
    seen: list[Exception] = []

    # When the scope exits
    # Then the BaseException propagates and the callback is not used
    with pytest.raises(_Stop):
        async with async_thread_lock(lock, "agent", "t1", on_release_error=seen.append):
            pass
    assert seen == []
