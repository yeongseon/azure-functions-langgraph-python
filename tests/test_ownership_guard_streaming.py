"""Ownership-guard boundaries on the true-streaming transport (no Azure I/O)."""

from __future__ import annotations

# asyncio is intentional: these regressions exercise the transport's edge cancellation.
import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
import json
import threading
from typing import Final, TypedDict

import pytest
from starlette.responses import JSONResponse, StreamingResponse

from azure_functions_langgraph.contracts import StreamRequest
from azure_functions_langgraph.locks import InProcessThreadLock, ThreadLock
from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY,
    OwnershipGuard,
    ThreadLeaseLostError,
)
from azure_functions_langgraph.observability import new_run_context
from azure_functions_langgraph.streaming import StreamingLangGraphApp, _sse_event_stream
from tests.ownership_guard_support import (
    GuardableLock,
    ProbeGraph,
    RaisingProviderLock,
    RecordingObserver,
    ScriptedGuard,
    guard_in,
)
from tests.test_streaming_app import FakeRequest

# SIZE_OK: the requested single-file boundary matrix shares one harness; no runtime code.
_GRAPH: Final = "graph-SECRET-7f3a"
_THREAD: Final = "thread-SECRET-7f3a"
_RID: Final = "rid-7f3a"
_WAIT: Final = 2.0
_DATA: Final = [f'event: data\ndata: {{"n": {n}}}\n\n' for n in (1, 2, 3)]
_END: Final = "event: end\ndata: {}\n\n"


class _GuardKeyword(TypedDict, total=False):
    guard: OwnershipGuard


def _request(config: dict[str, object] | None = None) -> FakeRequest:
    request = FakeRequest({"input": {}, "config": config if config is not None else _config()})
    request.headers["X-Request-ID"] = _RID
    return request


def _config() -> dict[str, object]:
    return {"configurable": {"thread_id": _THREAD}}


class _Run:
    """Keep the registered graph, observer and actual lock together for each run."""

    def __init__(self, graph: ProbeGraph, lock: ThreadLock, is_async: bool) -> None:
        self.graph = graph
        self.lock = lock
        self.is_async = is_async
        self.observer = RecordingObserver()
        self.app = StreamingLangGraphApp(thread_lock=lock, observer=self.observer)
        self.app.register(graph, _GRAPH, async_mode=is_async)
        self.reg = self.app._registrations[_GRAPH]

    async def invoke(self, request: FakeRequest | None = None) -> JSONResponse:
        response: JSONResponse = await self.app._handle_invoke(
            request if request is not None else _request(),
            self.reg,
            self.is_async,
            JSONResponse,
            self.observer,
            self.lock,
        )
        return response

    async def stream(self) -> StreamingResponse:
        response: StreamingResponse = await self.app._handle_stream(
            _request(),
            self.reg,
            self.is_async,
            JSONResponse,
            StreamingResponse,
            self.observer,
            self.lock,
        )
        return response

    async def prepare(self) -> StreamingResponse | JSONResponse:
        response: StreamingResponse | JSONResponse = await self.app._prepare_stream_response(
            self.reg,
            StreamRequest(input={}),
            _config(),
            _THREAD,
            True,
            self.is_async,
            {},
            JSONResponse,
            StreamingResponse,
            self.observer,
            self.lock,
            new_run_context(_GRAPH, "stream", transport="streaming", run_id=_RID),
        )
        return response


async def _frames(response: StreamingResponse) -> list[str]:
    frames: list[str] = []
    async for frame in response.body_iterator:
        assert isinstance(frame, str)
        frames.append(frame)
    return frames


def _released(lock: GuardableLock | RaisingProviderLock) -> None:
    assert len(lock.acquired) == 1
    assert lock.released == lock.acquired


def _redacted(body: str, lock: GuardableLock | RaisingProviderLock) -> None:
    for secret in (_GRAPH, _THREAD, lock.acquired[0], "ThreadLeaseLostError"):
        assert secret not in body


def _invoke_failure(response: JSONResponse) -> None:
    assert response.status_code == 500
    assert json.loads(bytes(response.body)) == {
        "error": "error",
        "detail": "Graph execution failed",
        "request_id": _RID,
    }


def _loss(observer: RecordingObserver) -> None:
    assert observer.events == ["started", "failed"]
    assert len(observer.failures) == 1
    error = observer.failures[0]
    assert isinstance(error, ThreadLeaseLostError)
    assert (error.reason, error.graph_name, error.thread_id) == ("lost", _GRAPH, _THREAD)


def _error_frame(error: str = "stream processing failed") -> str:
    return f"event: error\ndata: {json.dumps({'error': error, 'request_id': _RID})}\n\n"


@pytest.mark.parametrize("is_async", [False, True])
async def test_invoke_hands_acquired_guard_to_graph(is_async: bool) -> None:
    # Given a guard-capable real lock and a checkpointed graph.
    lock = GuardableLock()
    run = _Run(ProbeGraph(), lock, is_async)
    # When the true-stream invoke endpoint executes the graph.
    response = await run.invoke()
    # Then exactly the acquired guard protects the unchanged thread identity.
    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {"output": {"ok": True}}
    assert len(run.graph.configs) == len(lock.guards) == 1
    assert guard_in(run.graph.configs[0]) is lock.guards[0]
    assert run.graph.configs[0]["configurable"]["thread_id"] == _THREAD
    assert lock.issued == [(_GRAPH, _THREAD, lock.acquired[0])]
    assert lock.guards[0].checks == 1
    assert run.observer.events == ["started", "completed"]
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_invoke_reports_terminal_ownership_loss(is_async: bool) -> None:
    # Given ownership is lost when the completed invocation is checked.
    lock = GuardableLock(trip_on=1)
    run = _Run(ProbeGraph(), lock, is_async)
    # When a graph result would otherwise have succeeded.
    response = await run.invoke()
    # Then the ordinary sanitized failure replaces success and retires ownership.
    _invoke_failure(response)
    assert lock.guards[0].checks == 1
    _loss(run.observer)
    _redacted(bytes(response.body).decode(), lock)
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("capability", [False, True], ids=["absent", "disabled"])
async def test_invoke_preserves_config_without_guard(is_async: bool, capability: bool) -> None:
    # Given either no provider capability or a provider explicitly switched off.
    lock = GuardableLock(enabled=False) if capability else InProcessThreadLock()
    run = _Run(ProbeGraph(), lock, is_async)
    # When the invocation completes without guarding enabled.
    response = await run.invoke()
    # Then the exact pre-feature config and successful lifecycle are preserved.
    assert response.status_code == 200
    assert run.graph.configs == [_config()]
    assert run.observer.events == ["started", "completed"]
    if capability:
        assert isinstance(lock, GuardableLock)
        assert lock.guards == []
        assert lock.issued == []
        _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_invoke_releases_token_when_provider_raises(is_async: bool) -> None:
    # Given the optional provider fails inside the lock scope.
    lock = RaisingProviderLock()
    run = _Run(ProbeGraph(), lock, is_async)
    # When invoke attempts to construct the guarded config.
    response = await run.invoke()
    # Then the graph is not run and the provider error takes the existing 500 path.
    _invoke_failure(response)
    assert run.graph.configs == []
    assert run.observer.events == ["started", "failed"]
    assert len(run.observer.failures) == 1
    assert type(run.observer.failures[0]) is RuntimeError
    assert str(run.observer.failures[0]) == "guard provider failed"
    _redacted(bytes(response.body).decode(), lock)
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("top_level", [False, True])
async def test_invoke_rejects_client_guard_alias_before_lock(
    is_async: bool,
    top_level: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an untrusted alias at either forbidden config location.
    lock = GuardableLock()
    run = _Run(ProbeGraph(), lock, is_async)
    config = _config()
    if top_level:
        config[OWNERSHIP_GUARD_CONFIG_KEY] = "client-guard-canary"
    else:
        config["configurable"] = {
            "thread_id": _THREAD,
            OWNERSHIP_GUARD_CONFIG_KEY: "client-guard-canary",
        }

    def forbidden_acquire(graph_name: str, thread_id: str, timeout: float = 0.0) -> str:
        pytest.fail("config rejection must precede every lock acquisition attempt")

    monkeypatch.setattr(lock, "acquire", forbidden_acquire)
    # When the client submits a private guard key.
    response = await run.invoke(_request(config))
    # Then policy rejects it before provider, graph or lock work.
    assert response.status_code == 400
    assert json.loads(bytes(response.body)) == {
        "error": "error",
        "request_id": _RID,
        "detail": "Only config.configurable is client configurable"
        if top_level
        else "Internal configurable keys are not permitted",
    }
    assert "client-guard-canary" not in bytes(response.body).decode()
    assert run.graph.configs == []
    assert lock.acquired == lock.released == []
    assert lock.guards == []
    assert run.observer.events == ["rejected:invalid_request"]


@pytest.mark.parametrize("is_async", [False, True])
async def test_invoke_skips_terminal_check_when_graph_raises(is_async: bool) -> None:
    # Given an execution error occurs before a result is produced.
    failure = ValueError("private graph failure")

    def fail(config: object) -> None:
        raise failure

    lock = GuardableLock(trip_on=1)
    run = _Run(ProbeGraph(during_run=fail), lock, is_async)
    # When graph execution fails while a guard is installed.
    response = await run.invoke()
    # Then the original graph error, not a terminal guard error, is observed.
    _invoke_failure(response)
    assert guard_in(run.graph.configs[0]) is lock.guards[0]
    assert lock.guards[0].checks == 0
    assert run.observer.events == ["started", "failed"]
    assert run.observer.failures == [failure]
    _redacted(bytes(response.body).decode(), lock)
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_invoke_cancellation_retains_token_until_graph_finishes(
    is_async: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given execution blocked in either a sync worker or an async graph method.
    entered, proceed, finished = threading.Event(), threading.Event(), threading.Event()
    async_entered, async_proceed = asyncio.Event(), asyncio.Event()

    def block(config: object) -> None:
        entered.set()
        try:
            assert proceed.wait(_WAIT), "controller did not release sync worker"
        finally:
            finished.set()

    graph = ProbeGraph(during_run=None if is_async else block)

    async def ainvoke(input: object, config: object = None) -> dict[str, bool]:
        graph.configs.append(config)
        async_entered.set()
        try:
            await asyncio.wait_for(async_proceed.wait(), _WAIT)
            return {"ok": True}
        finally:
            finished.set()

    monkeypatch.setattr(graph, "ainvoke", ainvoke)
    lock = GuardableLock(trip_on=1)
    release_after_finish: list[bool] = []
    original_release = lock.release

    def release(graph_name: str, thread_id: str, token: str) -> None:
        release_after_finish.append(finished.is_set())
        original_release(graph_name, thread_id, token)

    monkeypatch.setattr(lock, "release", release)
    run = _Run(graph, lock, is_async)
    task = asyncio.create_task(run.invoke())
    try:
        if is_async:
            await asyncio.wait_for(async_entered.wait(), _WAIT)
        else:
            assert await asyncio.to_thread(entered.wait, _WAIT)
        assert guard_in(graph.configs[0]) is lock.guards[0]
        # When cancellation interrupts the owner, unblock sync work only afterward.
        task.cancel()
        assert lock.released == []
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _WAIT)
        # Then no terminal check runs and release never races graph execution.
        assert release_after_finish == [True]
        assert lock.guards[0].checks == 0
        assert run.observer.events == ["started", "failed"]
        assert len(run.observer.failures) == 1
        assert isinstance(run.observer.failures[0], asyncio.CancelledError)
        _released(lock)
    finally:
        proceed.set()
        async_proceed.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, _WAIT)


@pytest.mark.parametrize("is_async", [False, True])
async def test_stream_hands_guard_over_and_checks_between_data_and_end(
    is_async: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a prepared response has acquired ownership but not pulled graph data.
    order: list[str] = []
    lock = GuardableLock()
    run = _Run(ProbeGraph(during_run=lambda config: order.append("graph-done")), lock, is_async)
    response = await run.prepare()
    assert isinstance(response, StreamingResponse)
    assert run.graph.configs == []
    assert lock.released == []
    guard = lock.guards[0]
    original_check = guard.check

    def check() -> None:
        order.append("check")
        original_check()

    monkeypatch.setattr(guard, "check", check)
    # When the body is consumed incrementally, recording each delivered frame.
    frames: list[str] = []
    async for frame in response.body_iterator:
        assert isinstance(frame, str)
        frames.append(frame)
        order.append(frame.splitlines()[0].removeprefix("event: "))
    # Then the graph receives the acquired guard and only normal exhaustion checks it.
    assert frames == [*_DATA, _END]
    assert order == ["data", "data", "data", "graph-done", "check", "end"]
    assert len(run.graph.configs) == len(lock.guards) == 1
    assert guard_in(run.graph.configs[0]) is guard
    assert run.graph.configs[0]["configurable"]["thread_id"] == _THREAD
    assert lock.issued == [(_GRAPH, _THREAD, lock.acquired[0])]
    assert guard.checks == 1
    assert run.observer.events == ["started", "completed"]
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_stream_reports_terminal_loss_after_all_data(is_async: bool) -> None:
    # Given the prepared guard will reject its first terminal check.
    lock = GuardableLock(trip_on=1)
    run = _Run(ProbeGraph(), lock, is_async)
    # When the deferred handler response is fully consumed.
    frames = await _frames(await run.stream())
    # Then delivered data is retained, followed by sanitized error and end frames.
    assert frames == [*_DATA, _error_frame(), _END]
    assert guard_in(run.graph.configs[0]) is lock.guards[0]
    assert lock.guards[0].checks == 1
    _loss(run.observer)
    _redacted("".join(frames), lock)
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_stream_event_cap_bypasses_terminal_guard(is_async: bool) -> None:
    # Given more graph chunks than the event cap and a guard that would trip.
    lock = GuardableLock(trip_on=1)
    run = _Run(ProbeGraph(), lock, is_async)
    run.app.max_stream_events = 1
    # When the response stops early at the cap's break.
    frames = await _frames(await run.stream())
    # Then the cap error wins without treating the truncated stream as exhausted.
    assert frames == [_DATA[0], _error_frame("stream exceeded max events (1)"), _END]
    assert lock.guards[0].checks == 0
    assert run.observer.events == ["started", "failed"]
    assert len(run.observer.failures) == 1
    assert type(run.observer.failures[0]) is RuntimeError
    assert str(run.observer.failures[0]) == "stream exceeded max events (1)"
    _redacted("".join(frames), lock)
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_stream_explicit_close_bypasses_terminal_guard(is_async: bool) -> None:
    # Given a deferred body paused immediately after its first data frame.
    lock = GuardableLock(trip_on=1)
    run = _Run(ProbeGraph(), lock, is_async)
    response = await run.stream()
    body = response.body_iterator
    assert isinstance(body, AsyncGenerator)
    assert await anext(body) == _DATA[0]
    # When the client closes without exhausting the graph.
    await asyncio.wait_for(body.aclose(), _WAIT)
    # Then GeneratorExit fails the run, skips the check and releases exactly once.
    assert lock.guards[0].checks == 0
    assert run.observer.events == ["started", "failed"]
    assert len(run.observer.failures) == 1
    assert isinstance(run.observer.failures[0], GeneratorExit)
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_stream_cancelled_consumer_bypasses_terminal_guard(
    is_async: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given one delivered frame followed by a blocked pull from the actual graph.
    entered, proceed, finished = threading.Event(), threading.Event(), threading.Event()
    async_entered, async_proceed = asyncio.Event(), asyncio.Event()
    graph = ProbeGraph()

    def sync_stream(
        input: object,
        config: object = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, int]]:
        graph.configs.append(config)
        try:
            yield {"n": 1}
            entered.set()
            assert proceed.wait(_WAIT), "controller did not release stream worker"
            yield {"n": 2}
        finally:
            finished.set()

    async def astream(
        input: object,
        config: object = None,
        stream_mode: str = "values",
    ) -> AsyncIterator[dict[str, int]]:
        graph.configs.append(config)
        try:
            yield {"n": 1}
            async_entered.set()
            await asyncio.wait_for(async_proceed.wait(), _WAIT)
            yield {"n": 2}
        finally:
            finished.set()

    monkeypatch.setattr(graph, "stream", sync_stream)
    monkeypatch.setattr(graph, "astream", astream)
    lock = GuardableLock(trip_on=1)
    release_after_finish: list[bool] = []
    original_release = lock.release

    def release(graph_name: str, thread_id: str, token: str) -> None:
        release_after_finish.append(finished.is_set())
        original_release(graph_name, thread_id, token)

    monkeypatch.setattr(lock, "release", release)
    run = _Run(graph, lock, is_async)
    response = await run.stream()
    body = response.body_iterator
    assert isinstance(body, AsyncGenerator)
    assert await anext(body) == _DATA[0]

    async def consume() -> list[str]:
        return await _frames(response)

    task = asyncio.create_task(consume())
    try:
        if is_async:
            await asyncio.wait_for(async_entered.wait(), _WAIT)
        else:
            assert await asyncio.to_thread(entered.wait, _WAIT)
        # When the consumer task is cancelled during its next graph pull.
        task.cancel()
        assert lock.released == []
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _WAIT)
        # Then cancellation, not loss, is observed after safe worker cleanup.
        assert release_after_finish == [True]
        assert lock.guards[0].checks == 0
        assert run.observer.events == ["started", "failed"]
        assert len(run.observer.failures) == 1
        assert isinstance(run.observer.failures[0], asyncio.CancelledError)
        _released(lock)
    finally:
        proceed.set()
        async_proceed.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, _WAIT)
        await asyncio.wait_for(body.aclose(), _WAIT)


@pytest.mark.parametrize("is_async", [False, True])
async def test_prepare_propagates_provider_error_and_releases_token(is_async: bool) -> None:
    # Given a provider failure before ownership can be handed to the generator.
    lock = RaisingProviderLock()
    run = _Run(ProbeGraph(), lock, is_async)
    # When prepare itself attempts to construct the guard.
    with pytest.raises(RuntimeError, match="^guard provider failed$") as raised:
        await run.prepare()
    # Then prepare re-raises that error after exactly one release and failure callback.
    assert run.graph.configs == []
    assert run.observer.events == ["started", "failed"]
    assert run.observer.failures == [raised.value]
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("capability", [False, True], ids=["absent", "disabled"])
async def test_stream_preserves_config_and_frames_without_guard(
    is_async: bool,
    capability: bool,
) -> None:
    # Given the provider capability is absent or explicitly disabled.
    lock = GuardableLock(enabled=False) if capability else InProcessThreadLock()
    run = _Run(ProbeGraph(), lock, is_async)
    # When the complete deferred stream is consumed.
    frames = await _frames(await run.stream())
    # Then config and wire framing are identical to the pre-feature behavior.
    assert frames == [*_DATA, _END]
    assert run.graph.configs == [_config()]
    assert run.observer.events == ["started", "completed"]
    if capability:
        assert isinstance(lock, GuardableLock)
        assert lock.guards == []
        assert lock.issued == []
        _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_prepare_contention_creates_no_guard(is_async: bool) -> None:
    # Given another owner already holds the same graph/thread lock.
    lock = GuardableLock()
    token = lock.acquire(_GRAPH, _THREAD)
    assert token is not None
    run = _Run(ProbeGraph(), lock, is_async)
    try:
        # When the response's preparation attempts to acquire that thread.
        response = await run.prepare()
        # Then contention stays a JSON 409, without a guard or a started run.
        assert isinstance(response, JSONResponse)
        assert response.status_code == 409
        assert json.loads(bytes(response.body)) == {
            "error": "error",
            "request_id": _RID,
            "detail": f"Thread {_THREAD!r} is currently in use by another request",
        }
        assert lock.guards == []
        assert lock.issued == []
        assert run.graph.configs == []
        assert lock.acquired == [token]
        assert lock.released == []
        assert run.observer.events == ["rejected:lock_contention"]
    finally:
        lock.release(_GRAPH, _THREAD, token)


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("guarded", [False, True], ids=["default-none", "explicit-guard"])
async def test_direct_generator_preserves_frames_with_optional_guard(
    is_async: bool,
    guarded: bool,
) -> None:
    # Given a direct generator call omits guard or passes a non-tripping guard.
    lock = GuardableLock()
    token = lock.acquire(_GRAPH, _THREAD)
    observer = RecordingObserver()
    guard = ScriptedGuard(graph_name=_GRAPH, thread_id=_THREAD)
    keywords: _GuardKeyword = {"guard": guard} if guarded else {}
    # When the public-to-tests keyword calling convention drains the stream.
    frames = [
        frame
        async for frame in _sse_event_stream(
            graph=ProbeGraph(),
            graph_name=_GRAPH,
            input_={},
            config=_config(),
            stream_mode="values",
            version_kwargs={},
            thread_id=_THREAD,
            lock_token=token,
            thread_lock=lock,
            observer=observer,
            ctx=new_run_context(_GRAPH, "stream", transport="streaming", run_id=_RID),
            max_stream_events=10,
            async_mode=is_async,
            **keywords,
        )
    ]
    # Then default compatibility and one explicit terminal check are distinguishable.
    assert frames == [*_DATA, _END]
    assert guard.checks == (1 if guarded else 0)
    assert observer.events == ["completed"]
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_direct_generator_loss_emits_error_before_end(is_async: bool) -> None:
    # Given a directly supplied guard reports loss after graph exhaustion.
    lock = GuardableLock()
    token = lock.acquire(_GRAPH, _THREAD)
    observer = RecordingObserver()
    guard = ScriptedGuard(trip_on=1, graph_name=_GRAPH, thread_id=_THREAD)
    # When the direct SSE generator is drained normally.
    frames = [
        frame
        async for frame in _sse_event_stream(
            graph=ProbeGraph(),
            graph_name=_GRAPH,
            input_={},
            config=_config(),
            stream_mode="values",
            version_kwargs={},
            thread_id=_THREAD,
            lock_token=token,
            thread_lock=lock,
            observer=observer,
            ctx=new_run_context(_GRAPH, "stream", transport="streaming", run_id=_RID),
            max_stream_events=10,
            async_mode=is_async,
            guard=guard,
        )
    ]
    # Then loss uses the existing streaming error vocabulary, never raw exception text.
    assert frames == [*_DATA, _error_frame(), _END]
    assert guard.checks == 1
    assert observer.events == ["failed"]
    assert len(observer.failures) == 1
    error = observer.failures[0]
    assert isinstance(error, ThreadLeaseLostError)
    assert (error.reason, error.graph_name, error.thread_id) == ("lost", _GRAPH, _THREAD)
    _redacted("".join(frames), lock)
    _released(lock)


@pytest.mark.parametrize("is_async", [False, True])
async def test_direct_exact_cap_still_checks_terminal_guard(is_async: bool) -> None:
    # Given the cap equals, rather than truncates, the complete chunk count.
    lock = GuardableLock()
    token = lock.acquire(_GRAPH, _THREAD)
    observer = RecordingObserver()
    guard = ScriptedGuard()
    # When the iterator exhausts normally exactly at the configured cap.
    frames = [
        frame
        async for frame in _sse_event_stream(
            graph=ProbeGraph(),
            graph_name=_GRAPH,
            input_={},
            config=_config(),
            stream_mode="values",
            version_kwargs={},
            thread_id=_THREAD,
            lock_token=token,
            thread_lock=lock,
            observer=observer,
            ctx=new_run_context(_GRAPH, "stream", transport="streaming", run_id=_RID),
            max_stream_events=3,
            async_mode=is_async,
            guard=guard,
        )
    ]
    # Then the for/else terminal check runs, without a false cap error.
    assert frames == [*_DATA, _END]
    assert guard.checks == 1
    assert observer.events == ["completed"]
    _released(lock)
