"""Native handlers turn a raising thread lock backend into the standard JSON 500 (#928)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
import json
import logging
import types
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph._handlers import (
    handle_invoke,
    handle_invoke_async,
    handle_stream,
    handle_stream_async,
)
from azure_functions_langgraph.observability import RunContext, RunRejectedReason

_SECRET = "secret-sentinel /private/path"
_LIMITS: dict[str, Any] = {
    "max_request_body_bytes": 1_000_000,
    "max_input_depth": 20,
    "max_input_nodes": 1_000,
}


class _Lock:
    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.releases: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        if self.error is not None:
            raise self.error
        return None

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.releases.append(token)


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
    def __init__(self) -> None:
        self.checkpointer = types.SimpleNamespace()
        self.calls = 0

    def invoke(self, graph_input: Any, config: Any = None) -> dict[str, bool]:
        self.calls += 1
        return {"ok": True}

    def stream(
        self, graph_input: Any, config: Any = None, stream_mode: str = "values"
    ) -> Iterator[dict[str, bool]]:
        self.calls += 1
        yield {"ok": True}

    async def ainvoke(self, graph_input: Any, config: Any = None) -> dict[str, bool]:
        self.calls += 1
        return {"ok": True}

    async def astream(
        self, graph_input: Any, config: Any = None, stream_mode: str = "values"
    ) -> AsyncIterator[dict[str, bool]]:
        self.calls += 1
        yield {"ok": True}


def _request(endpoint: str) -> func.HttpRequest:
    body = {"input": {"m": "x"}, "config": {"configurable": {"thread_id": "t1"}}}
    return func.HttpRequest(
        method="POST",
        url=f"https://example.test/api/graphs/agent/{endpoint}",
        body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


async def _call(kind: str, graph: _Graph, lock: _Lock, observer: _Observer) -> func.HttpResponse:
    reg = types.SimpleNamespace(name="agent", graph=graph, stream_enabled=True)
    common: dict[str, Any] = {"thread_lock": lock, "observer": observer, **_LIMITS}
    endpoint = "invoke" if "invoke" in kind else "stream"
    if kind == "invoke":
        return handle_invoke(_request(endpoint), reg, **common)
    if kind == "invoke_async":
        return await handle_invoke_async(_request(endpoint), reg, **common)
    common["max_stream_response_bytes"] = 1_000_000
    if kind == "stream":
        return handle_stream(_request(endpoint), reg, **common)
    return await handle_stream_async(_request(endpoint), reg, **common)


_KINDS = ["invoke", "stream", "invoke_async", "stream_async"]


@pytest.mark.parametrize("kind", _KINDS)
async def test_raising_acquire_returns_standard_500(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a lock backend whose acquire raises with a secret in its message
    graph, lock, observer = _Graph(), _Lock(RuntimeError(_SECRET)), _Observer()

    # When the handler runs
    with caplog.at_level(logging.ERROR):
        response = await _call(kind, graph, lock, observer)

    # Then the response is the standard JSON 500 without the exception text
    assert response.status_code == 500
    payload = json.loads(response.get_body())
    assert payload["error"] == "error"
    assert payload["detail"] == "Thread lock could not be acquired"
    assert payload["request_id"]
    assert response.headers["X-Request-ID"] == payload["request_id"]
    visible = response.get_body().decode() + repr(dict(response.headers))
    assert "secret-sentinel" not in visible
    assert "/private/path" not in visible
    # And the observer saw started then failed with the raised error
    assert observer.events == ["started", "failed"]
    ctx, exc = observer.failed[0]
    assert isinstance(exc, RuntimeError)
    assert ctx.ended_at_ns is not None
    # And nothing was released or executed, and the traceback was logged
    assert lock.releases == []
    assert graph.calls == 0
    assert any(r.exc_info is not None for r in caplog.records)


@pytest.mark.parametrize("kind", _KINDS)
async def test_lock_contention_still_returns_409(kind: str) -> None:
    # Given a lock backend whose acquire returns None
    graph, lock, observer = _Graph(), _Lock(None), _Observer()

    # When the handler runs
    response = await _call(kind, graph, lock, observer)

    # Then it is the unchanged 409 rejection
    assert response.status_code == 409
    assert observer.events == ["rejected"]
    assert observer.reasons == ["lock_contention"]
    assert graph.calls == 0


@pytest.mark.parametrize("kind", ["invoke_async", "stream_async"])
async def test_cancelled_acquire_propagates(kind: str) -> None:
    # Given an acquire that raises CancelledError
    graph, lock, observer = _Graph(), _Lock(asyncio.CancelledError()), _Observer()

    # When the handler runs
    # Then cancellation propagates and no failure is reported
    with pytest.raises(asyncio.CancelledError):
        await _call(kind, graph, lock, observer)
    assert observer.events == []
    assert graph.calls == 0
