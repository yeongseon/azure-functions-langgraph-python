"""Integration tests using real LangGraph StateGraph + InMemorySaver.

These tests drive real compiled graphs through the native HTTP handler layer
(``/api/graphs/{name}/invoke``, ``stream``, ``state``).  All node logic is
deterministic — no LLM calls.

Issue: #41
"""

from __future__ import annotations

from importlib.metadata import version as _pkg_version
import json
import operator
from typing import Annotated, Any, TypedDict

import azure.functions as func
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
import pytest

from azure_functions_langgraph.app import LangGraphApp


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
