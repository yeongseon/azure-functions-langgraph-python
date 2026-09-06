"""Integration tests using real LangGraph StateGraph + InMemorySaver.

These tests drive real compiled graphs through the native HTTP handler layer
(``/api/graphs/{name}/invoke``, ``stream``, ``state``).  All node logic is
deterministic — no LLM calls.

Issue: #41
"""

from __future__ import annotations

import asyncio
from importlib.metadata import version as _pkg_version
import json
import operator
import types
from typing import Annotated, Any, TypedDict

import azure.functions as func
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
import pytest

from azure_functions_langgraph._handlers import handle_stream_async
from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock


def _langgraph_supports_v2() -> bool:
    """``version='v2'`` requires langgraph>=1.1 (GraphOutput / StreamPart)."""
    parts = _pkg_version("langgraph").split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):  # pragma: no cover - defensive
        return False
    return (major, minor) >= (1, 1)


_requires_v2 = pytest.mark.skipif(
    not _langgraph_supports_v2(),
    reason="langgraph<1.1 does not support version='v2'",
)



# ---------------------------------------------------------------------------
# Graph state & deterministic nodes
# ---------------------------------------------------------------------------


class ChatState(TypedDict, total=False):
    user_text: str
    history: Annotated[list[str], operator.add]
    turn_count: int
    last_reply: str


def greet(state: ChatState) -> dict[str, Any]:
    """First node — build a greeting from *user_text*."""
    text = state.get("user_text", "")
    reply = f"Hello, {text}!" if text else "Hello!"
    return {"history": [reply], "last_reply": reply}


def count(state: ChatState) -> dict[str, Any]:
    """Second node — increment *turn_count*."""
    return {"turn_count": (state.get("turn_count") or 0) + 1}


def _build_graph(*, checkpointer: Any = None) -> Any:
    """Compile a two-node deterministic graph.

    ``greet`` → ``count`` with optional *checkpointer* for persistence.
    """
    builder = StateGraph(ChatState)
    builder.add_node("greet", greet)
    builder.add_node("count", count)
    builder.add_edge(START, "greet")
    builder.add_edge("greet", "count")
    builder.add_edge("count", END)
    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(graph: Any, *, name: str = "agent") -> LangGraphApp:
    """Build a LangGraphApp with a single registered graph."""
    app = LangGraphApp()
    app.register(graph=graph, name=name)
    return app


def _get_fn(fa: func.FunctionApp, fn_name: str) -> Any:
    """Retrieve a registered function handler by name."""
    fa.functions_bindings = {}
    for fn in fa.get_functions():
        if fn.get_function_name() == fn_name:
            return fn.get_user_function()
    raise AssertionError(f"Function {fn_name!r} not found")


def _post(url: str, body: dict[str, Any], **route_params: str) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url=url,
        body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        route_params=route_params,
    )


def _raw_post(url: str, raw: bytes, **route_params: str) -> func.HttpRequest:
    """POST with an arbitrary (possibly malformed) raw body."""
    return func.HttpRequest(
        method="POST",
        url=url,
        body=raw,
        headers={"Content-Type": "application/json"},
        route_params=route_params,
    )


def _get(url: str, **route_params: str) -> func.HttpRequest:
    return func.HttpRequest(
        method="GET",
        url=url,
        body=b"",
        route_params=route_params,
    )


def _parse_sse_frames(body: str) -> list[dict[str, Any]]:
    """Parse SSE body into structured frames.

    Each frame is ``{"event": ..., "data": ...}`` where *data* is the
    parsed JSON payload (or ``None`` when the data line is empty/absent).
    Frames are delimited by blank lines per the SSE specification.
    """
    frames: list[dict[str, Any]] = []
    current_event: str | None = None
    data_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
        elif line == "":
            if current_event is not None or data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload = json.loads(raw) if raw.strip() else None
                except json.JSONDecodeError:
                    payload = raw
                frames.append({"event": current_event, "data": payload})
                current_event = None
                data_lines = []
    # Flush last frame if no trailing blank line
    if current_event is not None or data_lines:
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            payload = raw
        frames.append({"event": current_event, "data": payload})
    return frames


# ---------------------------------------------------------------------------
# Tests — Native routes with real LangGraph graphs
# ---------------------------------------------------------------------------


class TestNativeInvoke:
    """Invoke endpoint with real compiled graph."""

    def test_single_turn_invoke(self) -> None:
        """Single invoke returns expected state from deterministic nodes."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Alice", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "t1"}},
            },
        )
        resp = handler(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        output = data["output"]

        assert output["last_reply"] == "Hello, Alice!"
        assert "Hello, Alice!" in output["history"]
        assert output["turn_count"] == 1

    def test_multi_turn_invoke_accumulates_state(self) -> None:
        """Two sequential invokes on the same thread accumulate history."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        # Turn 1
        req1 = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Alice", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "t-multi"}},
            },
        )
        resp1 = handler(req1)
        assert resp1.status_code == 200
        out1 = json.loads(resp1.get_body())["output"]
        assert out1["turn_count"] == 1

        # Turn 2 — same thread_id, state accumulates via reducer
        req2 = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Bob"},
                "config": {"configurable": {"thread_id": "t-multi"}},
            },
        )
        resp2 = handler(req2)
        assert resp2.status_code == 200
        out2 = json.loads(resp2.get_body())["output"]

        assert out2["turn_count"] == 2
        assert out2["history"] == ["Hello, Alice!", "Hello, Bob!"]
        assert out2["last_reply"] == "Hello, Bob!"

    def test_different_threads_are_isolated(self) -> None:
        """Different thread_ids maintain independent state."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        for tid, name in [("iso-a", "Alpha"), ("iso-b", "Beta")]:
            req = _post(
                "/api/graphs/agent/invoke",
                {
                    "input": {"user_text": name, "history": [], "turn_count": 0},
                    "config": {"configurable": {"thread_id": tid}},
                },
            )
            resp = handler(req)
            assert resp.status_code == 200
            out = json.loads(resp.get_body())["output"]
            assert out["turn_count"] == 1
            assert out["history"] == [f"Hello, {name}!"]


class TestNativeStream:
    """Stream endpoint with real compiled graph."""

    def test_stream_values_mode(self) -> None:
        """stream_mode='values' yields intermediate + final state snapshots."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _post(
            "/api/graphs/agent/stream",
            {
                "input": {"user_text": "Eve", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "stream-v"}},
                "stream_mode": "values",
            },
        )
        resp = handler(req)
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"

        # Parse SSE frames using proper frame-based parser
        body = resp.get_body().decode()
        frames = _parse_sse_frames(body)

        # Filter to data-bearing frames (skip empty payloads)
        data_frames = [f for f in frames if f["data"] and isinstance(f["data"], dict)]

        # At least 2 data events (intermediate snapshots + final)
        assert len(data_frames) >= 2
        # Final event should have the completed state
        final = data_frames[-1]["data"]
        assert final["turn_count"] == 1
        assert final["history"] == ["Hello, Eve!"]
        assert final["last_reply"] == "Hello, Eve!"

    def test_stream_updates_mode(self) -> None:
        """stream_mode='updates' yields per-node update dicts."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _post(
            "/api/graphs/agent/stream",
            {
                "input": {"user_text": "Frank", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "stream-u"}},
                "stream_mode": "updates",
            },
        )
        resp = handler(req)
        assert resp.status_code == 200

        body = resp.get_body().decode()
        frames = _parse_sse_frames(body)

        # Filter to data-bearing frames
        data_frames = [f for f in frames if f["data"] and isinstance(f["data"], dict)]

        # updates mode yields node-keyed dicts: {"greet": {...}}, {"count": {...}}
        node_names = set()
        for frame in data_frames:
            node_names.update(frame["data"].keys())
        assert "greet" in node_names
        assert "count" in node_names

        # Verify payload content — greet node should produce greeting
        greet_frames = [f for f in data_frames if "greet" in f["data"]]
        assert len(greet_frames) >= 1
        greet_payload = greet_frames[0]["data"]["greet"]
        assert greet_payload["last_reply"] == "Hello, Frank!"
        assert greet_payload["history"] == ["Hello, Frank!"]


class TestNativeState:
    """State endpoint with real compiled graph."""

    def test_state_after_invoke(self) -> None:
        """GET /state returns persisted thread state after invoke."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        fa = app.function_app

        invoke_fn = _get_fn(fa, "aflg_agent_invoke")
        state_fn = _get_fn(fa, "aflg_agent_state")

        # Invoke first
        req = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Grace", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "state-t1"}},
            },
        )
        resp = invoke_fn(req)
        assert resp.status_code == 200

        # GET state
        state_req = _get(
            "/api/graphs/agent/threads/state-t1/state",
            thread_id="state-t1",
        )
        state_resp = state_fn(state_req)
        assert state_resp.status_code == 200
        state_data = json.loads(state_resp.get_body())

        assert state_data["values"]["turn_count"] == 1
        assert state_data["values"]["history"] == ["Hello, Grace!"]
        assert state_data["values"]["last_reply"] == "Hello, Grace!"
        assert state_data["next"] == []

    def test_state_cross_check_with_direct_get_state(self) -> None:
        """HTTP state endpoint matches direct graph.get_state() on stable fields."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        fa = app.function_app

        invoke_fn = _get_fn(fa, "aflg_agent_invoke")
        state_fn = _get_fn(fa, "aflg_agent_state")

        # Invoke
        req = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Ivy", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "xcheck-1"}},
            },
        )
        invoke_fn(req)

        # HTTP state
        state_req = _get(
            "/api/graphs/agent/threads/xcheck-1/state",
            thread_id="xcheck-1",
        )
        http_resp = state_fn(state_req)
        http_state = json.loads(http_resp.get_body())

        # Direct get_state
        config = {"configurable": {"thread_id": "xcheck-1"}}
        snapshot = graph.get_state(config)

        # Cross-check stable fields
        assert http_state["values"] == snapshot.values
        assert http_state["next"] == list(snapshot.next)


class TestNativeErrors:
    """Error paths with real compiled graphs."""

    def test_state_unknown_thread_returns_empty(self) -> None:
        """GET /state for nonexistent thread returns 200 with empty values.

        Real MemorySaver returns an empty StateSnapshot (values={}, next=())
        for threads that have never been written — this is NOT a 404.
        """
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        state_fn = _get_fn(app.function_app, "aflg_agent_state")

        req = _get(
            "/api/graphs/agent/threads/nonexistent/state",
            thread_id="nonexistent",
        )
        resp = state_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["values"] == {}
        assert data["next"] == []

    def test_state_no_checkpointer_returns_404(self) -> None:
        """Graph without checkpointer → get_state raises ValueError → 404."""
        graph = _build_graph(checkpointer=None)
        # Real compiled graphs always have get_state(), so state route IS registered.
        # But get_state() raises ValueError('No checkpointer set').
        app = _make_app(graph)
        state_fn = _get_fn(app.function_app, "aflg_agent_state")

        req = _get(
            "/api/graphs/agent/threads/no-cp/state",
            thread_id="no-cp",
        )
        resp = state_fn(req)
        # ValueError is caught by handle_state → 404 (thread not found)
        assert resp.status_code == 404


class TestNativeStreamState:
    """Verify that stream also persists checkpointed state."""

    def test_stream_persists_state(self) -> None:
        """After streaming, GET /state returns the final persisted state."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        fa = app.function_app

        stream_fn = _get_fn(fa, "aflg_agent_stream")
        state_fn = _get_fn(fa, "aflg_agent_state")

        # Stream first
        req = _post(
            "/api/graphs/agent/stream",
            {
                "input": {"user_text": "Streamer", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "stream-persist"}},
                "stream_mode": "values",
            },
        )
        stream_resp = stream_fn(req)
        assert stream_resp.status_code == 200

        # GET state — should be persisted by checkpointer
        state_req = _get(
            "/api/graphs/agent/threads/stream-persist/state",
            thread_id="stream-persist",
        )
        state_resp = state_fn(state_req)
        assert state_resp.status_code == 200
        state_data = json.loads(state_resp.get_body())

        assert state_data["values"]["turn_count"] == 1
        assert state_data["values"]["history"] == ["Hello, Streamer!"]
        assert state_data["values"]["last_reply"] == "Hello, Streamer!"
        assert state_data["next"] == []


# ---------------------------------------------------------------------------
# Tests — version pass-through (#423)
# ---------------------------------------------------------------------------


class TestNativeVersionPassthrough:
    """Forwarding the optional ``version`` field to invoke/stream (#423)."""

    @_requires_v2
    def test_invoke_v2_returns_graph_output_envelope(self) -> None:
        """version='v2' invoke returns the GraphOutput {value, interrupts} shape."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Alice", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "v2-t1"}},
                "version": "v2",
            },
        )
        resp = handler(req)
        assert resp.status_code == 200
        output = json.loads(resp.get_body())["output"]

        # v2 wraps the state in a GraphOutput envelope, unlike the default shape.
        assert set(output.keys()) == {"value", "interrupts"}
        assert output["value"]["last_reply"] == "Hello, Alice!"
        assert output["value"]["turn_count"] == 1
        assert output["interrupts"] == []

    def test_invoke_default_shape_unchanged(self) -> None:
        """Omitting version keeps the flat state dict (no behavior change)."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Alice", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "v-default"}},
            },
        )
        resp = handler(req)
        assert resp.status_code == 200
        output = json.loads(resp.get_body())["output"]

        # Flat state — no GraphOutput envelope.
        assert "value" not in output
        assert output["last_reply"] == "Hello, Alice!"
        assert output["turn_count"] == 1

    @_requires_v2
    def test_stream_v2_emits_streampart_frames(self) -> None:
        """version='v2' stream yields StreamPart dicts (type/ns/data/interrupts)."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _post(
            "/api/graphs/agent/stream",
            {
                "input": {"user_text": "Bob", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "v2-stream"}},
                "stream_mode": "values",
                "version": "v2",
            },
        )
        resp = handler(req)
        assert resp.status_code == 200
        frames = _parse_sse_frames(resp.get_body().decode())
        data_frames = [
            f for f in frames if f["event"] == "data" and isinstance(f["data"], dict)
        ]
        assert data_frames
        for frame in data_frames:
            # StreamPart shape rather than a bare state snapshot.
            assert set(frame["data"].keys()) == {"type", "ns", "data", "interrupts"}
            assert frame["data"]["type"] == "values"
        final = data_frames[-1]["data"]["data"]
        assert final["turn_count"] == 1
        assert final["last_reply"] == "Hello, Bob!"

    def test_invoke_invalid_version_rejected(self) -> None:
        """An out-of-range version value fails Literal validation with 422."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Alice"},
                "version": "v9",
            },
        )
        resp = handler(req)
        assert resp.status_code == 422


class TestVersionKwargHelpers:
    """Unit coverage for the structural version-capability helpers (#423)."""

    def test_method_accepts_explicit_version_param(self) -> None:
        from azure_functions_langgraph._handlers import _method_accepts_version

        def fn(input: Any, *, version: str | None = None) -> None: ...

        assert _method_accepts_version(fn) is True

    def test_method_accepts_var_keyword(self) -> None:
        from azure_functions_langgraph._handlers import _method_accepts_version

        def fn(input: Any, **kwargs: Any) -> None: ...

        assert _method_accepts_version(fn) is True

    def test_method_without_version_rejected(self) -> None:
        from azure_functions_langgraph._handlers import _method_accepts_version

        def fn(input: Any, config: Any = None) -> None: ...

        assert _method_accepts_version(fn) is False

    def test_method_uninspectable_returns_false(self) -> None:
        from azure_functions_langgraph._handlers import _method_accepts_version

        # ``print`` is a builtin whose signature cannot be introspected.
        assert _method_accepts_version(print) is False

    def test_resolve_returns_empty_when_no_version(self) -> None:
        from azure_functions_langgraph._handlers import _resolve_version_kwarg

        def fn(input: Any, **kwargs: Any) -> None: ...

        assert _resolve_version_kwarg(fn, None, "agent") == {}

    def test_resolve_forwards_supported_version(self) -> None:
        from azure_functions_langgraph._handlers import _resolve_version_kwarg

        def fn(input: Any, **kwargs: Any) -> None: ...

        assert _resolve_version_kwarg(fn, "v2", "agent") == {"version": "v2"}

    def test_resolve_rejects_unsupported_graph(self) -> None:
        from azure_functions_langgraph._handlers import _resolve_version_kwarg

        def fn(input: Any, config: Any = None) -> None: ...

        result = _resolve_version_kwarg(fn, "v2", "agent")
        assert isinstance(result, func.HttpResponse)
        assert result.status_code == 422
        body = json.loads(result.get_body())
        assert "version" in body["detail"]



# ---------------------------------------------------------------------------
# Async graph fakes & helpers (#422)
# ---------------------------------------------------------------------------


class _PureAsyncGraph:
    """Async-only graph: exposes ``ainvoke``/``astream`` but no sync methods."""

    def __init__(self) -> None:
        self.checkpointer: Any = None
        self.calls: list[str] = []

    async def ainvoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        await asyncio.sleep(0)
        self.calls.append("ainvoke")
        text = input.get("user_text", "")
        return {"last_reply": f"Async, {text}!", "turn_count": 1}

    async def astream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
        **kwargs: Any,
    ) -> Any:
        await asyncio.sleep(0)
        self.calls.append("astream")
        text = input.get("user_text", "")
        yield {"last_reply": f"Async, {text}!"}
        yield {"turn_count": 1}


class _StrictAsyncGraph:
    """Async-only graph whose ``ainvoke`` does not accept a ``version`` kwarg."""

    def __init__(self) -> None:
        self.checkpointer: Any = None

    async def ainvoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"ok": True}

    async def astream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Any:
        await asyncio.sleep(0)
        yield {"ok": True}


class _FailingAsyncGraph:
    """Async-only graph whose ``ainvoke``/``astream`` raise mid-execution."""

    def __init__(self) -> None:
        self.checkpointer: Any = None

    async def ainvoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    async def astream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
        **kwargs: Any,
    ) -> Any:
        await asyncio.sleep(0)
        yield {"partial": True}
        raise RuntimeError("boom")


class _AsyncInvokeOnlyGraph:
    """Async invoke but no ``astream`` — the stream endpoint must return 501."""

    def __init__(self) -> None:
        self.checkpointer: Any = None

    async def ainvoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"ok": True}


class _SyncOnlyGraph:
    """Sync invoke only — ``async_mode=True`` must be rejected."""

    def invoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {}


class _NoInvokeGraph:
    """Neither ``invoke`` nor ``ainvoke`` — registration must fail."""

    def unrelated(self) -> None: ...


def _make_async_app(
    graph: Any, *, name: str = "agent", async_mode: bool = True, **app_kwargs: Any
) -> LangGraphApp:
    """Build a LangGraphApp registering *graph* through the async handler path."""
    app = LangGraphApp(**app_kwargs)
    app.register(graph=graph, name=name, async_mode=async_mode)
    return app


# ---------------------------------------------------------------------------
# Tests — native async invoke/stream (#422)
# ---------------------------------------------------------------------------


class TestAsyncNativeInvoke:
    """Async invoke handler via ``ainvoke``."""

    async def test_async_invoke_real_graph_envelope(self) -> None:
        """A real graph registered with async_mode=True awaits ainvoke."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Ada", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "a-t1"}},
            },
        )
        resp = await handler(req)
        assert resp.status_code == 200
        output = json.loads(resp.get_body())["output"]
        assert output["last_reply"] == "Hello, Ada!"
        assert output["turn_count"] == 1

    async def test_pure_async_graph_auto_routes(self) -> None:
        """A graph with only async methods is auto-routed to the async handler."""
        graph = _PureAsyncGraph()
        app = _make_async_app(graph, async_mode=False)  # not opted in
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post("/api/graphs/agent/invoke", {"input": {"user_text": "Bo"}})
        resp = await handler(req)
        assert resp.status_code == 200
        output = json.loads(resp.get_body())["output"]
        assert output["last_reply"] == "Async, Bo!"
        assert graph.calls == ["ainvoke"]

    async def test_async_invoke_forwards_version(self) -> None:
        """``version`` is forwarded when the async graph accepts it."""
        graph = _PureAsyncGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post(
            "/api/graphs/agent/invoke",
            {"input": {"user_text": "Cy"}, "version": "v2"},
        )
        resp = await handler(req)
        assert resp.status_code == 200

    async def test_async_invoke_version_unsupported_graph_422(self) -> None:
        """A graph whose ainvoke rejects ``version`` yields 422."""
        graph = _StrictAsyncGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post(
            "/api/graphs/agent/invoke",
            {"input": {"user_text": "Di"}, "version": "v2"},
        )
        resp = await handler(req)
        assert resp.status_code == 422

    async def test_async_invoke_graph_failure_returns_500(self) -> None:
        """An exception from ainvoke maps to a 500 error response."""
        graph = _FailingAsyncGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post("/api/graphs/agent/invoke", {"input": {"user_text": "Ed"}})
        resp = await handler(req)
        assert resp.status_code == 500

    async def test_async_invoke_thread_lock_contention_returns_409(self) -> None:
        """A held thread lock forces the async invoke to 409."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        assert app.thread_lock is not None
        token = app.thread_lock.acquire("agent", "busy-t")
        assert token is not None
        try:
            req = _post(
                "/api/graphs/agent/invoke",
                {
                    "input": {"user_text": "Fi", "history": [], "turn_count": 0},
                    "config": {"configurable": {"thread_id": "busy-t"}},
                },
            )
            resp = await handler(req)
            assert resp.status_code == 409
        finally:
            app.thread_lock.release("agent", "busy-t", token)

    async def test_async_invoke_releases_lock(self) -> None:
        """The async invoke releases the thread lock on success."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post(
            "/api/graphs/agent/invoke",
            {
                "input": {"user_text": "Gi", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "rel-t"}},
            },
        )
        resp = await handler(req)
        assert resp.status_code == 200
        # Lock must be free again after a successful run.
        assert app.thread_lock is not None
        token = app.thread_lock.acquire("agent", "rel-t")
        assert token is not None
        app.thread_lock.release("agent", "rel-t", token)


class TestAsyncNativeStream:
    """Async stream handler via ``astream``."""

    async def test_async_stream_buffered_sse(self) -> None:
        """A real graph async_mode=True streams buffered SSE frames."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _post(
            "/api/graphs/agent/stream",
            {
                "input": {"user_text": "Hu", "history": [], "turn_count": 0},
                "config": {"configurable": {"thread_id": "a-stream"}},
                "stream_mode": "values",
            },
        )
        resp = await handler(req)
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        frames = _parse_sse_frames(resp.get_body().decode())
        data_frames = [f for f in frames if f["data"] and isinstance(f["data"], dict)]
        assert data_frames
        final = data_frames[-1]["data"]
        assert final["turn_count"] == 1
        assert final["last_reply"] == "Hello, Hu!"
        assert any(f["event"] == "end" for f in frames)

    async def test_async_stream_invoke_only_returns_501(self) -> None:
        """A graph without ``astream`` returns 501 on the stream endpoint."""
        graph = _AsyncInvokeOnlyGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _post(
            "/api/graphs/agent/stream",
            {"input": {"user_text": "Io"}, "stream_mode": "values"},
        )
        resp = await handler(req)
        assert resp.status_code == 501

    async def test_async_stream_byte_cap_emits_error_frame(self) -> None:
        """Exceeding the buffered byte cap emits an SSE error frame."""
        graph = _PureAsyncGraph()
        app = _make_async_app(graph, max_stream_response_bytes=10)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _post(
            "/api/graphs/agent/stream",
            {"input": {"user_text": "Jo"}, "stream_mode": "values"},
        )
        resp = await handler(req)
        assert resp.status_code == 200
        frames = _parse_sse_frames(resp.get_body().decode())
        assert any(f["event"] == "error" for f in frames)

    async def test_async_stream_failure_emits_error_frame(self) -> None:
        """An exception raised mid-stream emits an SSE error frame."""
        graph = _FailingAsyncGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _post(
            "/api/graphs/agent/stream",
            {"input": {"user_text": "Ka"}, "stream_mode": "values"},
        )
        resp = await handler(req)
        assert resp.status_code == 200
        frames = _parse_sse_frames(resp.get_body().decode())
        error_frames = [f for f in frames if f["event"] == "error"]
        assert error_frames
        assert "stream processing failed" in error_frames[0]["data"]["error"]


class TestAsyncRegistration:
    """Registration-time validation for the async path (#422)."""

    def test_async_mode_requires_ainvoke(self) -> None:
        """``async_mode=True`` on a sync-only graph raises TypeError."""
        with pytest.raises(TypeError, match="ainvoke"):
            _make_async_app(_SyncOnlyGraph())

    def test_graph_without_any_invoke_rejected(self) -> None:
        """A graph with neither invoke nor ainvoke raises TypeError."""
        with pytest.raises(TypeError, match="invoke"):
            _make_async_app(_NoInvokeGraph(), async_mode=False)


class TestAsyncNativeBranchCoverage:
    """Error-path branch coverage for the async invoke/stream handlers (#422)."""

    async def test_async_invoke_malformed_json_returns_error(self) -> None:
        """A malformed JSON body short-circuits async invoke with an error."""
        graph = _PureAsyncGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _raw_post("/api/graphs/agent/invoke", b"{not-json")
        resp = await handler(req)
        assert resp.status_code == 400

    async def test_async_invoke_bad_config_returns_400(self) -> None:
        """A non-object ``config.configurable`` yields 400 on async invoke."""
        graph = _PureAsyncGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_invoke")

        req = _post(
            "/api/graphs/agent/invoke",
            {"input": {"user_text": "La"}, "config": {"configurable": "nope"}},
        )
        resp = await handler(req)
        assert resp.status_code == 400

    async def test_async_stream_malformed_json_returns_error(self) -> None:
        """A malformed JSON body short-circuits async stream with an error."""
        graph = _PureAsyncGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _raw_post("/api/graphs/agent/stream", b"{not-json")
        resp = await handler(req)
        assert resp.status_code == 400

    async def test_async_stream_bad_config_returns_400(self) -> None:
        """A non-object ``config.configurable`` yields 400 on async stream."""
        graph = _PureAsyncGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _post(
            "/api/graphs/agent/stream",
            {"input": {"user_text": "Ma"}, "config": {"configurable": "nope"}},
        )
        resp = await handler(req)
        assert resp.status_code == 400

    async def test_async_stream_version_unsupported_graph_422(self) -> None:
        """A graph whose astream rejects ``version`` yields 422."""
        graph = _StrictAsyncGraph()
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        req = _post(
            "/api/graphs/agent/stream",
            {"input": {"user_text": "Na"}, "version": "v2"},
        )
        resp = await handler(req)
        assert resp.status_code == 422

    async def test_async_stream_invoke_only_reg_returns_501(self) -> None:
        """A stream-disabled registration returns 501 (direct handler call)."""
        reg = types.SimpleNamespace(
            name="agent", stream_enabled=False, graph=_PureAsyncGraph()
        )
        req = _post("/api/graphs/agent/stream", {"input": {"user_text": "Ob"}})
        resp = await handle_stream_async(
            req,
            reg,
            thread_lock=InProcessThreadLock(),
            max_stream_response_bytes=1_000_000,
            max_request_body_bytes=1_000_000,
            max_input_depth=20,
            max_input_nodes=1_000,
        )
        assert resp.status_code == 501

    async def test_async_stream_thread_lock_contention_returns_409(self) -> None:
        """A held thread lock forces the async stream to 409."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        app = _make_async_app(graph)
        handler = _get_fn(app.function_app, "aflg_agent_stream")

        assert app.thread_lock is not None
        token = app.thread_lock.acquire("agent", "busy-stream")
        assert token is not None
        try:
            req = _post(
                "/api/graphs/agent/stream",
                {
                    "input": {"user_text": "Pa", "history": [], "turn_count": 0},
                    "config": {"configurable": {"thread_id": "busy-stream"}},
                },
            )
            resp = await handler(req)
            assert resp.status_code == 409
        finally:
            app.thread_lock.release("agent", "busy-stream", token)
