"""Deterministic tests for the true-streaming transport (``StreamingLangGraphApp``).

These tests exercise the streaming app without a live Azure Functions host. The
FastAPI extension (``azurefunctions-extensions-http-fastapi``) is installed in
the dev/CI environment, so ``function_app`` construction and the Starlette
request/response plumbing are covered here; only the real-Azure proof of
*incremental byte arrival* is deferred to the release-certification e2e.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Iterator
import warnings

import azure.functions as func
import pytest

from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.observability import (
    NoOpRunObserver,
    RunContext,
    new_run_context,
)
from azure_functions_langgraph.streaming import (
    StreamingLangGraphApp,
    _aiter_graph_events,
    _extract_thread_id,
    _format_data_event,
    _import_fastapi_extension,
    _JsonError,
    _read_json_body,
    _resolve_version_kwarg,
    _sse_event_stream,
    _validate_optional_model,
    _validate_request,
)

# The extension response/request types (installed via the ``streaming`` extra).
_ext = _import_fastapi_extension()
JSONResponse = _ext.JSONResponse
StreamingResponse = _ext.StreamingResponse


# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class FakeRequest:
    """Minimal duck-typed Starlette request — only ``body()`` is used."""

    def __init__(self, payload: Any = None, *, raw: bytes | None = None) -> None:
        if raw is not None:
            self._raw = raw
        elif payload is None:
            self._raw = b""
        else:
            self._raw = json.dumps(payload).encode("utf-8")

    async def body(self) -> bytes:
        return self._raw


class FakeSyncGraph:
    """Sync invoke + stream graph."""

    def __init__(
        self, checkpointer: Any = None, chunks: list[dict[str, Any]] | None = None
    ) -> None:
        self.checkpointer = checkpointer
        self._chunks = chunks or [
            {"messages": [{"role": "assistant", "content": "a"}]},
            {"messages": [{"role": "assistant", "content": "ab"}]},
        ]

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"messages": [{"role": "assistant", "content": "final"}]}

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield from self._chunks


class FakeAsyncGraph:
    """Async ainvoke + astream graph."""

    checkpointer = None

    def __init__(self, chunks: list[dict[str, Any]] | None = None) -> None:
        self._chunks = chunks or [{"n": 1}, {"n": 2}, {"n": 3}]

    async def ainvoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"ok": True}

    async def astream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> AsyncIterator[dict[str, Any]]:
        for chunk in self._chunks:
            yield chunk


class FakeInvokeOnlyGraph:
    """Graph without stream/astream."""

    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": True}


class FailingStreamGraph:
    """Streams one chunk then raises."""

    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": True}

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield {"messages": [{"role": "assistant", "content": "partial"}]}
        raise RuntimeError("boom")


class FailingInvokeGraph:
    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        raise RuntimeError("invoke boom")


class RecordingObserver:
    """Records lifecycle callbacks for assertions."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append("started")

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append("completed")

    def on_run_failed(self, ctx: RunContext, error: BaseException) -> None:
        self.events.append("failed")

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:
        self.events.append(f"rejected:{reason}")


class RecordingLock:
    """ThreadLock stub that records release calls."""

    def __init__(self, token: str | None = "tok") -> None:
        self._token = token
        self.acquired: list[tuple[str, str]] = []
        self.released: list[tuple[str, str, str]] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.acquired.append((graph_name, thread_id))
        return self._token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.released.append((graph_name, thread_id, token))


# ------------------------------------------------------------------
# Helpers to drive handlers / generators
# ------------------------------------------------------------------


async def _collect(gen: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in gen]


async def _read_stream_response(response: Any) -> list[str]:
    frames: list[str] = []
    async for chunk in response.body_iterator:
        frames.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return frames


def _ctx() -> RunContext:
    return new_run_context("g", "stream", transport="streaming", has_checkpointer=False)


# ------------------------------------------------------------------
# Pure helper unit tests
# ------------------------------------------------------------------


class TestPureHelpers:
    def test_format_data_event_dict(self) -> None:
        frame = _format_data_event({"a": 1})
        assert frame == 'event: data\ndata: {"a": 1}\n\n'

    def test_format_data_event_non_dict(self) -> None:
        assert _format_data_event("hi") == 'event: data\ndata: {"data": "hi"}\n\n'

    def test_extract_thread_id_none(self) -> None:
        assert _extract_thread_id({}) == (None, None)

    def test_extract_thread_id_configurable_not_dict(self) -> None:
        tid, err = _extract_thread_id({"configurable": "nope"})
        assert tid is None and err is not None

    def test_extract_thread_id_not_string(self) -> None:
        tid, err = _extract_thread_id({"configurable": {"thread_id": 5}})
        assert tid is None and err is not None

    def test_extract_thread_id_valid(self) -> None:
        assert _extract_thread_id({"configurable": {"thread_id": "t1"}}) == ("t1", None)

    def test_extract_thread_id_missing_thread(self) -> None:
        assert _extract_thread_id({"configurable": {}}) == (None, None)

    def test_validate_optional_model_rejects_non_model(self) -> None:
        with pytest.raises(TypeError):
            _validate_optional_model(dict, "request_model")

    def test_validate_optional_model_allows_none(self) -> None:
        _validate_optional_model(None, "request_model")

    def test_resolve_version_none(self) -> None:
        assert _resolve_version_kwarg(FakeSyncGraph().stream, None, "g") == {}

    def test_resolve_version_unsupported(self) -> None:
        result = _resolve_version_kwarg(FakeSyncGraph().stream, "v2", "g")
        assert isinstance(result, _JsonError)
        assert result.status_code == 422


class TestReadJsonBody:
    async def test_valid(self) -> None:
        body = await _read_json_body(FakeRequest({"input": {}}), max_request_body_bytes=1024)
        assert body == {"input": {}}

    async def test_empty_body_is_empty_dict(self) -> None:
        body = await _read_json_body(FakeRequest(raw=b""), max_request_body_bytes=1024)
        assert body == {}

    async def test_invalid_json(self) -> None:
        body = await _read_json_body(FakeRequest(raw=b"{bad"), max_request_body_bytes=1024)
        assert isinstance(body, _JsonError)
        assert body.status_code == 400

    async def test_oversize(self) -> None:
        body = await _read_json_body(FakeRequest({"x": "y" * 100}), max_request_body_bytes=4)
        assert isinstance(body, _JsonError)
        assert body.status_code == 400


class TestValidateRequest:
    def test_valid(self) -> None:
        from azure_functions_langgraph.contracts import InvokeRequest

        parsed = _validate_request(
            {"input": {"messages": []}}, InvokeRequest, max_input_depth=32, max_input_nodes=1000
        )
        assert not isinstance(parsed, _JsonError)

    def test_model_validation_error(self) -> None:
        from azure_functions_langgraph.contracts import InvokeRequest

        parsed = _validate_request(
            {"input": "not-a-dict"}, InvokeRequest, max_input_depth=32, max_input_nodes=1000
        )
        assert isinstance(parsed, _JsonError)
        assert parsed.status_code == 422

    def test_input_structure_too_deep(self) -> None:
        from azure_functions_langgraph.contracts import InvokeRequest

        parsed = _validate_request(
            {"input": {"a": {"b": {"c": {}}}}},
            InvokeRequest,
            max_input_depth=1,
            max_input_nodes=1000,
        )
        assert isinstance(parsed, _JsonError)
        assert parsed.status_code == 400

    def test_config_structure_too_deep(self) -> None:
        from azure_functions_langgraph.contracts import InvokeRequest

        parsed = _validate_request(
            {"input": {}, "config": {"a": {"b": {"c": {}}}}},
            InvokeRequest,
            max_input_depth=1,
            max_input_nodes=1000,
        )
        assert isinstance(parsed, _JsonError)
        assert parsed.status_code == 400


class TestAiterGraphEvents:
    async def test_sync_graph(self) -> None:
        events = [e async for e in _aiter_graph_events(FakeSyncGraph(), {}, {}, "values", {})]
        assert len(events) == 2

    async def test_async_graph(self) -> None:
        events = [e async for e in _aiter_graph_events(FakeAsyncGraph(), {}, {}, "values", {})]
        assert events == [{"n": 1}, {"n": 2}, {"n": 3}]


# ------------------------------------------------------------------
# _sse_event_stream generator
# ------------------------------------------------------------------


class TestSseEventStream:
    async def test_normal_completion(self) -> None:
        observer = RecordingObserver()
        lock = RecordingLock()
        frames = await _collect(
            _sse_event_stream(
                graph=FakeSyncGraph(),
                graph_name="g",
                input_={},
                config={},
                stream_mode="values",
                version_kwargs={},
                thread_id="t1",
                lock_token="tok",
                thread_lock=lock,
                observer=observer,
                ctx=_ctx(),
                max_stream_events=100,
            )
        )
        assert frames[0].startswith("event: data\n")
        assert frames[-1] == "event: end\ndata: {}\n\n"
        assert observer.events == ["completed"]
        assert lock.released == [("g", "t1", "tok")]

    async def test_no_lock_no_release(self) -> None:
        lock = RecordingLock()
        frames = await _collect(
            _sse_event_stream(
                graph=FakeSyncGraph(),
                graph_name="g",
                input_={},
                config={},
                stream_mode="values",
                version_kwargs={},
                thread_id=None,
                lock_token=None,
                thread_lock=lock,
                observer=NoOpRunObserver(),
                ctx=_ctx(),
                max_stream_events=100,
            )
        )
        assert frames[-1] == "event: end\ndata: {}\n\n"
        assert lock.released == []

    async def test_max_events_cap(self) -> None:
        observer = RecordingObserver()
        frames = await _collect(
            _sse_event_stream(
                graph=FakeSyncGraph(),
                graph_name="g",
                input_={},
                config={},
                stream_mode="values",
                version_kwargs={},
                thread_id=None,
                lock_token=None,
                thread_lock=RecordingLock(),
                observer=observer,
                ctx=_ctx(),
                max_stream_events=1,
            )
        )
        assert any(f.startswith("event: error\n") and "max events" in f for f in frames)
        assert frames[-1] == "event: end\ndata: {}\n\n"
        assert observer.events == ["failed"]

    async def test_graph_error_after_first_frame(self) -> None:
        observer = RecordingObserver()
        lock = RecordingLock()
        frames = await _collect(
            _sse_event_stream(
                graph=FailingStreamGraph(),
                graph_name="g",
                input_={},
                config={},
                stream_mode="values",
                version_kwargs={},
                thread_id="t1",
                lock_token="tok",
                thread_lock=lock,
                observer=observer,
                ctx=_ctx(),
                max_stream_events=100,
            )
        )
        assert frames[0].startswith("event: data\n")
        assert any(f.startswith("event: error\n") for f in frames)
        assert frames[-1] == "event: end\ndata: {}\n\n"
        assert observer.events == ["failed"]
        assert lock.released == [("g", "t1", "tok")]

    async def test_client_disconnect_releases_lock_and_reraises(self) -> None:
        observer = RecordingObserver()
        lock = RecordingLock()

        class SlowGraph:
            checkpointer = None

            def stream(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
                stream_mode: str = "values",
            ) -> Iterator[dict[str, Any]]:
                yield {"a": 1}
                raise asyncio.CancelledError()

        gen = _sse_event_stream(
            graph=SlowGraph(),
            graph_name="g",
            input_={},
            config={},
            stream_mode="values",
            version_kwargs={},
            thread_id="t1",
            lock_token="tok",
            thread_lock=lock,
            observer=observer,
            ctx=_ctx(),
            max_stream_events=100,
        )
        with pytest.raises(asyncio.CancelledError):
            await _collect(gen)
        # Released exactly once (in the cancellation arm, not double-released).
        assert lock.released == [("g", "t1", "tok")]
        assert observer.events == ["failed"]


# ------------------------------------------------------------------
# register() validation
# ------------------------------------------------------------------


class TestRegister:
    def test_duplicate_name(self) -> None:
        app = StreamingLangGraphApp()
        app.register(graph=FakeSyncGraph(), name="a")
        with pytest.raises(ValueError):
            app.register(graph=FakeSyncGraph(), name="a")

    def test_invalid_name(self) -> None:
        app = StreamingLangGraphApp()
        with pytest.raises(ValueError):
            app.register(graph=FakeSyncGraph(), name="bad name!")

    def test_async_mode_without_ainvoke(self) -> None:
        app = StreamingLangGraphApp()
        with pytest.raises(TypeError):
            app.register(graph=FakeSyncGraph(), name="a", async_mode=True)

    def test_graph_without_invoke(self) -> None:
        app = StreamingLangGraphApp()

        class Empty:
            pass

        with pytest.raises(TypeError):
            app.register(graph=Empty(), name="a")

    def test_bad_request_model(self) -> None:
        app = StreamingLangGraphApp()
        with pytest.raises(TypeError):
            app.register(graph=FakeSyncGraph(), name="a", request_model=dict)


# ------------------------------------------------------------------
# Construction
# ------------------------------------------------------------------


class TestConstruction:
    def test_anonymous_warns(self) -> None:
        with pytest.warns(UserWarning):
            StreamingLangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)

    def test_route_prefix_normalized(self) -> None:
        app = StreamingLangGraphApp(route_prefix="v1/")
        assert app.route_prefix == "/v1"

    def test_empty_route_prefix(self) -> None:
        app = StreamingLangGraphApp(route_prefix="")
        assert app.route_prefix == "/"

    def test_defaults_set(self) -> None:
        app = StreamingLangGraphApp()
        assert isinstance(app.thread_lock, InProcessThreadLock)
        assert isinstance(app.observer, NoOpRunObserver)

    def test_lock_backend_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "distributed")
        with pytest.raises(RuntimeError):
            StreamingLangGraphApp()

    def test_lock_backend_guard_allows_inprocess_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "inprocess")
        StreamingLangGraphApp()


# ------------------------------------------------------------------
# function_app construction (exercises the FastAPI extension import path)
# ------------------------------------------------------------------


class TestFunctionApp:
    def test_builds_expected_functions(self) -> None:
        app = StreamingLangGraphApp()
        app.register(graph=FakeSyncGraph(), name="agent")
        fa = app.function_app
        fa.functions_bindings = {}
        names = {f.get_function_name() for f in fa.get_functions()}
        assert {
            "aflg_health",
            "aflg_health_details",
            "aflg_agent_invoke",
            "aflg_agent_stream",
        } <= names

    def test_stream_disabled_skips_stream_route(self) -> None:
        app = StreamingLangGraphApp()
        app.register(graph=FakeSyncGraph(), name="agent", stream=False)
        fa = app.function_app
        fa.functions_bindings = {}
        names = {f.get_function_name() for f in fa.get_functions()}
        assert "aflg_agent_invoke" in names
        assert "aflg_agent_stream" not in names

    def test_invoke_only_graph_skips_stream_route(self) -> None:
        app = StreamingLangGraphApp()
        app.register(graph=FakeInvokeOnlyGraph(), name="agent")
        fa = app.function_app
        fa.functions_bindings = {}
        names = {f.get_function_name() for f in fa.get_functions()}
        assert "aflg_agent_invoke" in names

    def test_function_app_cached(self) -> None:
        app = StreamingLangGraphApp()
        app.register(graph=FakeSyncGraph(), name="agent")
        assert app.function_app is app.function_app

    def test_register_invalidates_cache(self) -> None:
        app = StreamingLangGraphApp()
        app.register(graph=FakeSyncGraph(), name="a")
        first = app.function_app
        app.register(graph=FakeSyncGraph(), name="b")
        assert app.function_app is not first

    def test_per_graph_auth_override(self) -> None:
        app = StreamingLangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(graph=FakeSyncGraph(), name="pub", auth_level=func.AuthLevel.ANONYMOUS)
        fa = app.function_app
        fa.functions_bindings = {}
        for f in fa.get_functions():
            if f.get_function_name() == "aflg_pub_invoke":
                assert f.get_trigger().auth_level == func.AuthLevel.ANONYMOUS  # type: ignore[union-attr]
                break
        else:
            pytest.fail("invoke function not found")

    def test_health_details_defaults_to_app_auth(self) -> None:
        app = StreamingLangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        assert app._resolved_health_details_auth_level == func.AuthLevel.FUNCTION

    def test_health_details_explicit_auth(self) -> None:
        app = StreamingLangGraphApp(
            auth_level=func.AuthLevel.FUNCTION,
            health_details_auth_level=func.AuthLevel.ANONYMOUS,
        )
        assert app._resolved_health_details_auth_level == func.AuthLevel.ANONYMOUS


# ------------------------------------------------------------------
# _handle_invoke
# ------------------------------------------------------------------


def _app_with(graph: Any, name: str = "agent", **kwargs: Any) -> tuple[StreamingLangGraphApp, Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        app = StreamingLangGraphApp(**kwargs)
    app.register(graph=graph, name=name)
    reg = app._registrations[name]
    return app, reg


def _lock(app: StreamingLangGraphApp) -> Any:
    """Return the app's thread lock, narrowed from ``Optional`` for the type checker."""
    assert app.thread_lock is not None
    return app.thread_lock



class TestHandleInvoke:
    async def test_sync_success(self) -> None:
        app, reg = _app_with(FakeSyncGraph())
        observer = RecordingObserver()
        resp = await app._handle_invoke(
            FakeRequest({"input": {}}), reg, False, JSONResponse, observer, _lock(app)
        )
        assert resp.status_code == 200
        assert observer.events == ["started", "completed"]

    async def test_async_success(self) -> None:
        app, reg = _app_with(FakeAsyncGraph())
        observer = RecordingObserver()
        resp = await app._handle_invoke(
            FakeRequest({"input": {}}), reg, True, JSONResponse, observer, _lock(app)
        )
        assert resp.status_code == 200
        assert "completed" in observer.events

    async def test_invalid_json(self) -> None:
        app, reg = _app_with(FakeSyncGraph())
        observer = RecordingObserver()
        resp = await app._handle_invoke(
            FakeRequest(raw=b"{bad"), reg, False, JSONResponse, observer, _lock(app)
        )
        assert resp.status_code == 400
        assert observer.events == ["rejected:invalid_request"]

    async def test_validation_error(self) -> None:
        app, reg = _app_with(FakeSyncGraph())
        resp = await app._handle_invoke(
            FakeRequest({"input": "bad"}),
            reg,
            False,
            JSONResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        assert resp.status_code == 422

    async def test_invalid_config(self) -> None:
        app, reg = _app_with(FakeSyncGraph())
        resp = await app._handle_invoke(
            FakeRequest({"input": {}, "config": {"configurable": "nope"}}),
            reg,
            False,
            JSONResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        assert resp.status_code == 400

    async def test_lock_contention(self) -> None:
        lock = InProcessThreadLock()
        app, reg = _app_with(FakeSyncGraph(checkpointer=object()))
        app.thread_lock = lock
        lock.acquire("agent", "t1")  # pre-hold
        resp = await app._handle_invoke(
            FakeRequest({"input": {}, "config": {"configurable": {"thread_id": "t1"}}}),
            reg,
            False,
            JSONResponse,
            NoOpRunObserver(),
            lock,
        )
        assert resp.status_code == 409

    async def test_graph_execution_error(self) -> None:
        app, reg = _app_with(FailingInvokeGraph())
        observer = RecordingObserver()
        resp = await app._handle_invoke(
            FakeRequest({"input": {}}), reg, False, JSONResponse, observer, _lock(app)
        )
        assert resp.status_code == 500
        assert "failed" in observer.events

    async def test_lock_acquired_and_released_on_success(self) -> None:
        lock = RecordingLock()
        app, reg = _app_with(FakeSyncGraph(checkpointer=object()))
        app.thread_lock = lock
        resp = await app._handle_invoke(
            FakeRequest({"input": {}, "config": {"configurable": {"thread_id": "t1"}}}),
            reg,
            False,
            JSONResponse,
            NoOpRunObserver(),
            lock,
        )
        assert resp.status_code == 200
        assert lock.acquired == [("agent", "t1")]
        assert lock.released == [("agent", "t1", "tok")]


# ------------------------------------------------------------------
# _handle_stream
# ------------------------------------------------------------------


class TestHandleStream:
    async def test_streaming_unsupported(self) -> None:
        app, reg = _app_with(FakeInvokeOnlyGraph())
        observer = RecordingObserver()
        resp = await app._handle_stream(
            FakeRequest({"input": {}}),
            reg,
            False,
            JSONResponse,
            StreamingResponse,
            observer,
            _lock(app),
        )
        assert resp.status_code == 501
        assert observer.events == ["rejected:streaming_unsupported"]

    async def test_invalid_json(self) -> None:
        app, reg = _app_with(FakeSyncGraph())
        resp = await app._handle_stream(
            FakeRequest(raw=b"{bad"),
            reg,
            False,
            JSONResponse,
            StreamingResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        assert resp.status_code == 400

    async def test_invalid_config(self) -> None:
        app, reg = _app_with(FakeSyncGraph())
        resp = await app._handle_stream(
            FakeRequest({"input": {}, "config": {"configurable": "nope"}}),
            reg,
            False,
            JSONResponse,
            StreamingResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        assert resp.status_code == 400

    async def test_lock_contention(self) -> None:
        lock = InProcessThreadLock()
        app, reg = _app_with(FakeSyncGraph(checkpointer=object()))
        app.thread_lock = lock
        lock.acquire("agent", "t1")
        resp = await app._handle_stream(
            FakeRequest({"input": {}, "config": {"configurable": {"thread_id": "t1"}}}),
            reg,
            False,
            JSONResponse,
            StreamingResponse,
            NoOpRunObserver(),
            lock,
        )
        assert resp.status_code == 409

    async def test_success_streams_frames(self) -> None:
        app, reg = _app_with(FakeSyncGraph())
        observer = RecordingObserver()
        resp = await app._handle_stream(
            FakeRequest({"input": {}}),
            reg,
            False,
            JSONResponse,
            StreamingResponse,
            observer,
            _lock(app),
        )
        assert resp.media_type == "text/event-stream"
        assert resp.headers["X-Accel-Buffering"] == "no"
        frames = await _read_stream_response(resp)
        assert any(f.startswith("event: data\n") for f in frames)
        assert frames[-1] == "event: end\ndata: {}\n\n"
        # started emitted before headers; completed emitted as the stream drains.
        assert observer.events == ["started", "completed"]

    async def test_async_success_streams_frames(self) -> None:
        app, reg = _app_with(FakeAsyncGraph())
        resp = await app._handle_stream(
            FakeRequest({"input": {}}),
            reg,
            True,
            JSONResponse,
            StreamingResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        frames = await _read_stream_response(resp)
        assert len([f for f in frames if f.startswith("event: data\n")]) == 3


class VersionGraph:
    """Graph whose stream/invoke accept a ``version`` kwarg (via **kwargs)."""

    checkpointer = None

    def invoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        return {"ok": True, "version": kw.get("version")}

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
        **kw: Any,
    ) -> Iterator[dict[str, Any]]:
        yield {"version": kw.get("version")}


def _user_fn(app: StreamingLangGraphApp, fn_name: str) -> Any:
    fa = app.function_app
    fa.functions_bindings = {}
    for fn in fa.get_functions():
        if fn.get_function_name() == fn_name:
            return fn.get_user_function()
    raise AssertionError(f"function {fn_name!r} not found")


class TestWiredHandlers:
    """Cover the health + per-graph closures wired inside ``_build_function_app``."""

    async def test_health(self) -> None:
        app, _ = _app_with(FakeSyncGraph())
        resp = await _user_fn(app, "aflg_health")(FakeRequest())
        assert json.loads(bytes(resp.body)) == {"status": "ok"}

    async def test_health_details(self) -> None:
        app, _ = _app_with(FakeSyncGraph(checkpointer=object()))
        resp = await _user_fn(app, "aflg_health_details")(FakeRequest())
        body = json.loads(bytes(resp.body))
        assert body["status"] == "ok"
        assert body["graphs"][0]["name"] == "agent"
        assert body["graphs"][0]["has_checkpointer"] is True

    async def test_wired_invoke(self) -> None:
        app, _ = _app_with(FakeSyncGraph())
        resp = await _user_fn(app, "aflg_agent_invoke")(FakeRequest({"input": {}}))
        assert resp.status_code == 200

    async def test_wired_stream(self) -> None:
        app, _ = _app_with(FakeSyncGraph())
        resp = await _user_fn(app, "aflg_agent_stream")(FakeRequest({"input": {}}))
        frames = await _read_stream_response(resp)
        assert frames[-1] == "event: end\ndata: {}\n\n"


class TestVersionAndThreadIdBranches:
    async def test_invoke_version_forwarded(self) -> None:
        app, reg = _app_with(VersionGraph())
        resp = await app._handle_invoke(
            FakeRequest({"input": {}, "version": "v2"}),
            reg,
            False,
            JSONResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        assert resp.status_code == 200

    async def test_invoke_version_rejected(self) -> None:
        app, reg = _app_with(FakeSyncGraph())
        resp = await app._handle_invoke(
            FakeRequest({"input": {}, "version": "v2"}),
            reg,
            False,
            JSONResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        assert resp.status_code == 422

    async def test_stream_version_rejected(self) -> None:
        app, reg = _app_with(FakeSyncGraph())
        resp = await app._handle_stream(
            FakeRequest({"input": {}, "version": "v2"}),
            reg,
            False,
            JSONResponse,
            StreamingResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        assert resp.status_code == 422

    async def test_invoke_invalid_thread_id(self) -> None:
        app, reg = _app_with(FakeSyncGraph(checkpointer=object()))
        resp = await app._handle_invoke(
            FakeRequest({"input": {}, "config": {"configurable": {"thread_id": "bad\x01id"}}}),
            reg,
            False,
            JSONResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        assert resp.status_code == 400


class TestExtraPaths:
    async def test_stream_success_with_lock(self) -> None:
        lock = RecordingLock()
        app, reg = _app_with(FakeSyncGraph(checkpointer=object()))
        app.thread_lock = lock
        resp = await app._handle_stream(
            FakeRequest({"input": {}, "config": {"configurable": {"thread_id": "t1"}}}),
            reg,
            False,
            JSONResponse,
            StreamingResponse,
            NoOpRunObserver(),
            lock,
        )
        frames = await _read_stream_response(resp)
        assert frames[-1] == "event: end\ndata: {}\n\n"
        assert lock.acquired == [("agent", "t1")]
        assert lock.released == [("agent", "t1", "tok")]

    def test_explicit_lock_and_observer_preserved(self) -> None:
        lock = InProcessThreadLock()
        observer = RecordingObserver()
        app = StreamingLangGraphApp(thread_lock=lock, observer=observer)
        assert app.thread_lock is lock
        assert app.observer is observer

    async def test_stream_input_structure_rejected(self) -> None:
        app, reg = _app_with(FakeSyncGraph(), max_input_depth=1)
        resp = await app._handle_stream(
            FakeRequest({"input": {"a": {"b": {"c": {}}}}}),
            reg,
            False,
            JSONResponse,
            StreamingResponse,
            NoOpRunObserver(),
            _lock(app),
        )
        assert resp.status_code == 400
