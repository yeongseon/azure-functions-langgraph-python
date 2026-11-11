"""Behavior parity for request-to-execution planning across adapters (#861).

These tests pin the observable behavior of the Native (classic ``HttpRequest``),
Streaming (FastAPI/ASGI) and Platform adapters so shared planning helpers cannot
drift: thread/config admission, version negotiation, optional-model validation,
authorization ordering and SSE semantics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from functools import partial
import json
from typing import Any, Literal

import anyio
import azure.functions as func
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
import pytest
from starlette.requests import Request
from starlette.types import Message

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.streaming import StreamingLangGraphApp

Endpoint = Literal["invoke", "stream"]


class _Base:
    def __init__(self, *, checkpointed: bool) -> None:
        self.checkpointer = InMemorySaver() if checkpointed else None
        self.calls: list[dict[str, Any]] = []

    def _record(self, config: Any, version: str | None) -> None:
        self.calls.append({"config": config, "version": version})


class _PlainGraph(_Base):
    """Structural graph whose methods do not accept ``version``."""

    def invoke(self, input: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self._record(config, None)
        return {"ok": True}

    async def ainvoke(self, input: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self._record(config, None)
        return {"ok": True}

    def stream(
        self, input: dict[str, Any], config: Any = None, stream_mode: str = "values"
    ) -> Iterator[dict[str, Any]]:
        self._record(config, None)
        yield {"chunk": 1}
        yield {"chunk": 2}

    async def astream(
        self, input: dict[str, Any], config: Any = None, stream_mode: str = "values"
    ) -> AsyncIterator[dict[str, Any]]:
        self._record(config, None)
        yield {"chunk": 1}
        yield {"chunk": 2}


class _VersionedGraph(_Base):
    """Structural graph whose methods accept ``version``."""

    def invoke(
        self, input: dict[str, Any], config: Any = None, version: str | None = None
    ) -> dict[str, Any]:
        self._record(config, version)
        return {"ok": True}

    async def ainvoke(
        self, input: dict[str, Any], config: Any = None, version: str | None = None
    ) -> dict[str, Any]:
        self._record(config, version)
        return {"ok": True}

    def stream(
        self,
        input: dict[str, Any],
        config: Any = None,
        stream_mode: str = "values",
        version: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        self._record(config, version)
        yield {"chunk": 1}
        yield {"chunk": 2}

    async def astream(
        self,
        input: dict[str, Any],
        config: Any = None,
        stream_mode: str = "values",
        version: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self._record(config, version)
        yield {"chunk": 1}
        yield {"chunk": 2}


_Graph = _PlainGraph | _VersionedGraph


def _graph(*, checkpointed: bool, versioned: bool) -> _Graph:
    cls = _VersionedGraph if versioned else _PlainGraph
    return cls(checkpointed=checkpointed)


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


async def _run_native(
    graph: _Graph,
    payload: dict[str, Any],
    endpoint: Endpoint,
    async_mode: bool,
    **register: Any,
) -> tuple[int, bytes]:
    app = LangGraphApp()
    app.register(graph=graph, name="agent", **register)
    reg = app._registrations["agent"]
    req = func.HttpRequest(method="POST", url="https://example.test/x", body=_body(payload))
    if endpoint == "invoke":
        if async_mode:
            resp = await app._handle_invoke_async(req, reg)
        else:
            resp = await anyio.to_thread.run_sync(partial(app._handle_invoke, req, reg))
    elif async_mode:
        resp = await app._handle_stream_async(req, reg)
    else:
        resp = await anyio.to_thread.run_sync(partial(app._handle_stream, req, reg))
    return resp.status_code, resp.get_body()


async def _run_streaming(
    graph: _Graph,
    payload: dict[str, Any],
    endpoint: Endpoint,
    async_mode: bool,
    **register: Any,
) -> tuple[int, bytes]:
    app = StreamingLangGraphApp()
    app.register(graph, "agent", async_mode=async_mode, **register)
    handlers = {
        f.get_function_name(): f.get_user_function() for f in app.function_app.get_functions()
    }
    raw = _body(payload)

    async def receive() -> Message:
        return {"type": "http.request", "body": raw}

    response = await handlers[f"aflg_agent_{endpoint}"](
        Request({"type": "http", "headers": []}, receive)
    )
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    return messages[0]["status"], b"".join(m.get("body", b"") for m in messages)


_RUNNERS = {"native": _run_native, "streaming": _run_streaming}
_TRANSPORTS = pytest.mark.parametrize("transport", ["native", "streaming"])
_ENDPOINTS = pytest.mark.parametrize("endpoint", ["invoke", "stream"])
_MODES = pytest.mark.parametrize("async_mode", [False, True])


@_TRANSPORTS
@_ENDPOINTS
@_MODES
@pytest.mark.parametrize(
    ("config", "detail"),
    [
        ({"configurable": "nope"}, "config.configurable must be an object"),
        ({"configurable": {"thread_id": 5}}, "config.configurable.thread_id must be a string"),
        ({"configurable": {"thread_id": ""}}, None),
        ({"configurable": {}}, "config.configurable.thread_id is required for checkpointed graphs"),
        ({}, "config.configurable.thread_id is required for checkpointed graphs"),
    ],
)
async def test_invalid_thread_config_rejected_identically(
    transport: str, endpoint: Endpoint, async_mode: bool, config: dict[str, Any], detail: str | None
) -> None:
    graph = _graph(checkpointed=True, versioned=True)
    status, body = await _RUNNERS[transport](
        graph, {"input": {"v": 1}, "config": config}, endpoint, async_mode
    )
    assert status == 400
    if detail is not None:
        assert json.loads(body)["detail"] == detail
    assert graph.calls == []


@_TRANSPORTS
@_ENDPOINTS
@_MODES
async def test_threadless_unchecked_graph_executes_without_thread(
    transport: str, endpoint: Endpoint, async_mode: bool
) -> None:
    graph = _graph(checkpointed=False, versioned=True)
    status, _ = await _RUNNERS[transport](graph, {"input": {"v": 1}}, endpoint, async_mode)
    assert status == 200
    assert len(graph.calls) == 1


@_TRANSPORTS
@_ENDPOINTS
@_MODES
async def test_version_forwarded_when_supported(
    transport: str, endpoint: Endpoint, async_mode: bool
) -> None:
    graph = _graph(checkpointed=False, versioned=True)
    status, _ = await _RUNNERS[transport](
        graph, {"input": {"v": 1}, "version": "v2"}, endpoint, async_mode
    )
    assert status == 200
    assert graph.calls[0]["version"] == "v2"


@_TRANSPORTS
@_ENDPOINTS
@_MODES
async def test_version_rejected_with_422_when_unsupported(
    transport: str, endpoint: Endpoint, async_mode: bool
) -> None:
    graph = _graph(checkpointed=False, versioned=False)
    status, body = await _RUNNERS[transport](
        graph, {"input": {"v": 1}, "version": "v2"}, endpoint, async_mode
    )
    assert status == 422
    assert json.loads(body)["detail"] == (
        "Graph 'agent' does not accept a 'version' argument (requires langgraph>=1.1)"
    )
    assert graph.calls == []


@_TRANSPORTS
@_ENDPOINTS
@_MODES
async def test_no_version_runs_on_unversioned_graph(
    transport: str, endpoint: Endpoint, async_mode: bool
) -> None:
    graph = _graph(checkpointed=False, versioned=False)
    status, _ = await _RUNNERS[transport](graph, {"input": {"v": 1}}, endpoint, async_mode)
    assert status == 200
    assert len(graph.calls) == 1


@_TRANSPORTS
@_ENDPOINTS
@_MODES
async def test_denied_authorization_precedes_version_and_execution(
    transport: str, endpoint: Endpoint, async_mode: bool
) -> None:
    # Authorization precedes version negotiation: an unauthorized caller gets
    # 403 even when the requested version would be rejected with 422.
    graph = _graph(checkpointed=False, versioned=False)
    seen: list[Any] = []

    def deny(context: Any) -> bool:
        seen.append(context)
        return False

    if transport == "native":
        app: Any = LangGraphApp(authorize=deny)
        app.register(graph=graph, name="agent")
        reg = app._registrations["agent"]
        req = func.HttpRequest(
            method="POST",
            url="https://example.test/x",
            body=_body({"input": {}, "version": "v2"}),
        )
        if endpoint == "invoke":
            resp = (
                await app._handle_invoke_async(req, reg)
                if async_mode
                else await anyio.to_thread.run_sync(partial(app._handle_invoke, req, reg))
            )
        else:
            resp = (
                await app._handle_stream_async(req, reg)
                if async_mode
                else await anyio.to_thread.run_sync(partial(app._handle_stream, req, reg))
            )
        status, body = resp.status_code, resp.get_body()
    else:
        sapp = StreamingLangGraphApp(authorize=deny)
        sapp.register(graph, "agent", async_mode=async_mode)
        handlers = {
            f.get_function_name(): f.get_user_function() for f in sapp.function_app.get_functions()
        }
        raw = _body({"input": {}, "version": "v2"})

        async def receive() -> Message:
            return {"type": "http.request", "body": raw}

        response = await handlers[f"aflg_agent_{endpoint}"](
            Request({"type": "http", "headers": []}, receive)
        )
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        status, body = sent[0]["status"], b"".join(m.get("body", b"") for m in sent)
    assert status == 403
    assert json.loads(body)["detail"] == "Access denied"
    assert len(seen) == 1
    assert graph.calls == []


class _NotModel:
    pass


class _Model(BaseModel):
    x: int = 0


@pytest.mark.parametrize("app_factory", [LangGraphApp, StreamingLangGraphApp])
@pytest.mark.parametrize("label", ["request_model", "response_model"])
def test_optional_model_validation_parity(app_factory: Any, label: str) -> None:
    app = app_factory()
    graph = _graph(checkpointed=False, versioned=True)
    with pytest.raises(TypeError) as exc:
        app.register(graph, "agent", **{label: _NotModel})
    assert str(exc.value) == f"{label} must be a Pydantic BaseModel subclass, got {_NotModel!r}"
    app.register(graph, "agent", request_model=_Model, response_model=_Model)
    app2 = app_factory()
    app2.register(graph, "agent", request_model=None, response_model=None)


async def test_sse_stream_semantics_match_across_transports() -> None:
    native_graph = _graph(checkpointed=False, versioned=True)
    streaming_graph = _graph(checkpointed=False, versioned=True)
    n_status, n_body = await _run_native(native_graph, {"input": {}}, "stream", False)
    s_status, s_body = await _run_streaming(streaming_graph, {"input": {}}, "stream", False)
    assert n_status == s_status == 200

    def frames(body: bytes) -> list[str]:
        return [f for f in body.decode().split("\n\n") if f]

    assert frames(n_body) == frames(s_body)
    assert frames(n_body)[-1].startswith("event: end")


def test_uninspectable_method_is_treated_as_not_accepting_version() -> None:
    from azure_functions_langgraph._execution_plan import negotiate_version

    class Opaque:
        @property
        def __signature__(self) -> Any:
            raise ValueError("no signature")

        def __call__(self, *args: Any) -> None: ...

    kwargs, error = negotiate_version(Opaque(), "v2", "agent")
    assert kwargs == {}
    assert error is not None and "does not accept a 'version'" in error
