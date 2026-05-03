"""Integration tests — Platform API routes with real LangGraph graphs.

These tests drive real compiled graphs through the Platform API–compatible
route layer (``/api/threads/{thread_id}/runs/wait``, ``runs/stream``,
``threads/{thread_id}/state``).

Issue: #41
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, Any, TypedDict

import azure.functions as func
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.platform.stores import InMemoryThreadStore

# ---------------------------------------------------------------------------
# Graph state & deterministic nodes (same schema as test_integration.py)
# ---------------------------------------------------------------------------


class ChatState(TypedDict, total=False):
    user_text: str
    history: Annotated[list[str], operator.add]
    turn_count: int
    last_reply: str


def greet(state: ChatState) -> dict[str, Any]:
    text = state.get("user_text", "")
    reply = f"Hello, {text}!" if text else "Hello!"
    return {"history": [reply], "last_reply": reply}


def count(state: ChatState) -> dict[str, Any]:
    return {"turn_count": (state.get("turn_count") or 0) + 1}


def _build_graph(*, checkpointer: Any = None) -> Any:
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


def _make_platform_app(
    graph: Any,
    *,
    store: InMemoryThreadStore | None = None,
    name: str = "agent",
) -> LangGraphApp:
    """Build a LangGraphApp with platform_compat=True."""
    app = LangGraphApp(platform_compat=True)
    if store is not None:
        app._thread_store = store
    app.register(graph=graph, name=name)
    return app


def _get_fn(fa: func.FunctionApp, fn_name: str) -> Any:
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
# Tests — Platform routes with real LangGraph graphs
# ---------------------------------------------------------------------------


class TestPlatformInvoke:
    """runs/wait endpoint with real compiled graph."""

    def test_invoke_via_platform(self) -> None:
        """POST /threads/{tid}/runs/wait invokes real graph and returns state."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        store = InMemoryThreadStore(id_factory=lambda: "pt-1")
        app = _make_platform_app(graph, store=store)
        fa = app.function_app

        # Create thread
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post("/api/threads", {}))
        assert resp.status_code == 200
        thread = json.loads(resp.get_body())
        tid = thread["thread_id"]

        # runs/wait
        wait_fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post(
            f"/api/threads/{tid}/runs/wait",
            {
                "assistant_id": "agent",
                "input": {"user_text": "Platform", "history": [], "turn_count": 0},
            },
            thread_id=tid,
        )
        resp = wait_fn(req)
        assert resp.status_code == 200

        output = json.loads(resp.get_body())
        assert output["last_reply"] == "Hello, Platform!"
        assert output["turn_count"] == 1
        assert "Hello, Platform!" in output["history"]

    def test_multi_turn_via_platform(self) -> None:
        """Two runs/wait on same thread accumulate state."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        store = InMemoryThreadStore(id_factory=lambda: "pt-mt")
        app = _make_platform_app(graph, store=store)
        fa = app.function_app

        # Create thread
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post("/api/threads", {}))
        tid = json.loads(resp.get_body())["thread_id"]

        wait_fn = _get_fn(fa, "aflg_platform_runs_wait")

        # Turn 1
        req1 = _post(
            f"/api/threads/{tid}/runs/wait",
            {
                "assistant_id": "agent",
                "input": {"user_text": "Turn1", "history": [], "turn_count": 0},
            },
            thread_id=tid,
        )
        out1 = json.loads(wait_fn(req1).get_body())
        assert out1["turn_count"] == 1

        # Turn 2
        req2 = _post(
            f"/api/threads/{tid}/runs/wait",
            {
                "assistant_id": "agent",
                "input": {"user_text": "Turn2"},
            },
            thread_id=tid,
        )
        out2 = json.loads(wait_fn(req2).get_body())
        assert out2["turn_count"] == 2
        assert "Hello, Turn1!" in out2["history"]
        assert out2["history"] == ["Hello, Turn1!", "Hello, Turn2!"]
        assert out2["last_reply"] == "Hello, Turn2!"


class TestPlatformStream:
    """runs/stream endpoint with real compiled graph."""

    def test_stream_via_platform(self) -> None:
        """POST /threads/{tid}/runs/stream returns valid SSE events."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        store = InMemoryThreadStore(id_factory=lambda: "ps-1")
        app = _make_platform_app(graph, store=store)
        fa = app.function_app

        # Create thread
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post("/api/threads", {}))
        tid = json.loads(resp.get_body())["thread_id"]

        # runs/stream
        stream_fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post(
            f"/api/threads/{tid}/runs/stream",
            {
                "assistant_id": "agent",
                "input": {"user_text": "Stream", "history": [], "turn_count": 0},
                "stream_mode": "values",
            },
            thread_id=tid,
        )
        resp = stream_fn(req)
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"

        body = resp.get_body().decode()
        frames = _parse_sse_frames(body)

        # Verify event types using proper frame parser
        event_types = [f["event"] for f in frames if f["event"]]
        assert "metadata" in event_types
        assert "values" in event_types
        assert "end" in event_types

        # MUST-FIX: Assert payload content, not just event-type presence
        values_frames = [
            f
            for f in frames
            if f["event"] == "values" and f["data"] and isinstance(f["data"], dict)
        ]
        assert len(values_frames) >= 1
        # At least one values event should contain the final state
        final_values = values_frames[-1]["data"]
        assert final_values["turn_count"] == 1
        assert final_values["history"] == ["Hello, Stream!"]
        assert final_values["last_reply"] == "Hello, Stream!"


class TestPlatformState:
    """State endpoint with real compiled graph via platform routes."""

    def test_state_after_platform_invoke(self) -> None:
        """GET /threads/{tid}/state returns real persisted state."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        store = InMemoryThreadStore(id_factory=lambda: "pst-1")
        app = _make_platform_app(graph, store=store)
        fa = app.function_app

        # Create thread
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post("/api/threads", {}))
        tid = json.loads(resp.get_body())["thread_id"]

        # Invoke to populate state
        wait_fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post(
            f"/api/threads/{tid}/runs/wait",
            {
                "assistant_id": "agent",
                "input": {"user_text": "StateCheck", "history": [], "turn_count": 0},
            },
            thread_id=tid,
        )
        wait_fn(req)

        # GET state
        state_fn = _get_fn(fa, "aflg_platform_threads_state_get")
        state_req = _get(
            f"/api/threads/{tid}/state",
            thread_id=tid,
        )
        state_resp = state_fn(state_req)
        assert state_resp.status_code == 200

        state_data = json.loads(state_resp.get_body())
        assert state_data["values"]["turn_count"] == 1
        assert "Hello, StateCheck!" in state_data["values"]["history"]
        assert state_data["values"]["last_reply"] == "Hello, StateCheck!"

    def test_state_unbound_thread_returns_409(self) -> None:
        """GET /state on a thread that has never run returns 409."""
        saver = MemorySaver()
        graph = _build_graph(checkpointer=saver)
        store = InMemoryThreadStore(id_factory=lambda: "pst-unbound")
        app = _make_platform_app(graph, store=store)
        fa = app.function_app

        # Create thread but don't run anything
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post("/api/threads", {}))
        tid = json.loads(resp.get_body())["thread_id"]

        # GET state on unbound thread
        state_fn = _get_fn(fa, "aflg_platform_threads_state_get")
        state_req = _get(f"/api/threads/{tid}/state", thread_id=tid)
        state_resp = state_fn(state_req)
        assert state_resp.status_code == 409
