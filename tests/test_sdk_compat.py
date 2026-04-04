"""SDK compatibility tests — verify that ``langgraph_sdk`` clients work
against our Platform API routes via ``httpx.MockTransport``.

The transport bridge converts SDK HTTP requests into
``azure.functions.HttpRequest`` objects, dispatches them to the
registered Azure Functions handler, and converts the response back
to an ``httpx.Response``.

Issue: #42
"""

from __future__ import annotations

import operator
import re
from typing import Annotated, Any, TypedDict

import azure.functions as func
import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph_sdk.client import SyncLangGraphClient
from langgraph_sdk.errors import ConflictError, InternalServerError, NotFoundError
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.platform.stores import InMemoryThreadStore

# ---------------------------------------------------------------------------
# Graph state & deterministic nodes (same as integration tests)
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
# Route table — explicit mapping from SDK paths to Azure Functions handlers
# ---------------------------------------------------------------------------

# Each entry: (HTTP method, regex pattern, function_name, [route_param_names])
_ROUTE_TABLE: list[tuple[str, re.Pattern[str], str, list[str]]] = [
    (
        "POST",
        re.compile(r"^/assistants/search$"),
        "aflg_platform_assistants_search",
        [],
    ),
    (
        "POST",
        re.compile(r"^/assistants/count$"),
        "aflg_platform_assistants_count",
        [],
    ),
    (
        "GET",
        re.compile(r"^/assistants/(?P<assistant_id>[^/]+)$"),
        "aflg_platform_assistants_get",
        ["assistant_id"],
    ),
    (
        "POST",
        re.compile(r"^/threads$"),
        "aflg_platform_threads_create",
        [],
    ),
    (
        "GET",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)$"),
        "aflg_platform_threads_get",
        ["thread_id"],
    ),
    (
        "PATCH",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)$"),
        "aflg_platform_threads_update",
        ["thread_id"],
    ),
    (
        "DELETE",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)$"),
        "aflg_platform_threads_delete",
        ["thread_id"],
    ),
    (
        "GET",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)/state$"),
        "aflg_platform_threads_state_get",
        ["thread_id"],
    ),
    (
        "POST",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)/runs/wait$"),
        "aflg_platform_runs_wait",
        ["thread_id"],
    ),
    (
        "POST",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)/runs/stream$"),
        "aflg_platform_runs_stream",
        ["thread_id"],
    ),
]


# ---------------------------------------------------------------------------
# MockTransport bridge
# ---------------------------------------------------------------------------


def _get_fn(fa: func.FunctionApp, fn_name: str) -> Any:
    """Retrieve a registered function handler by name."""
    fa.functions_bindings = {}
    for fn in fa.get_functions():
        if fn.get_function_name() == fn_name:
            return fn.get_user_function()
    raise AssertionError(f"Function {fn_name!r} not found")


def _make_transport(fa: func.FunctionApp) -> httpx.MockTransport:
    """Build an ``httpx.MockTransport`` that dispatches to Azure Functions handlers.

    The transport strips the optional ``/api`` prefix from incoming paths
    before matching against the route table, since the SDK sends requests
    without the Azure Functions ``/api`` prefix.
    """
    # Pre-resolve all handlers once
    handlers: dict[str, Any] = {}
    for _, _, fn_name, _ in _ROUTE_TABLE:
        if fn_name not in handlers:
            handlers[fn_name] = _get_fn(fa, fn_name)

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.raw_path.decode().split("?")[0]

        # Strip optional /api prefix (boundary-safe)
        if path.startswith("/api/") or path == "/api":
            path = path[4:]

        # Match against route table
        for rt_method, pattern, fn_name, param_names in _ROUTE_TABLE:
            if method != rt_method:
                continue
            m = pattern.match(path)
            if m is None:
                continue

            # Extract route params
            route_params = {name: m.group(name) for name in param_names}

            # Build Azure Functions HttpRequest
            body = request.content
            az_req = func.HttpRequest(
                method=method,
                url=str(request.url),
                body=body,
                headers=dict(request.headers),
                route_params=route_params,
            )

            # Call handler
            az_resp: func.HttpResponse = handlers[fn_name](az_req)

            # Convert to httpx.Response
            resp_headers = dict(az_resp.headers) if az_resp.headers else {}
            content_type = az_resp.mimetype or "application/json"
            resp_headers["content-type"] = content_type
            return httpx.Response(
                status_code=az_resp.status_code,
                content=az_resp.get_body(),
                headers=resp_headers,
            )

        # No match
        return httpx.Response(status_code=404, content=b'{"detail": "Not found"}')

    return httpx.MockTransport(handler)


def _make_sdk_client(fa: func.FunctionApp) -> SyncLangGraphClient:
    """Build a ``SyncLangGraphClient`` backed by our MockTransport."""
    transport = _make_transport(fa)
    httpx_client = httpx.Client(transport=transport, base_url="http://test")
    return SyncLangGraphClient(httpx_client)


def _make_app(
    *,
    store: InMemoryThreadStore | None = None,
    name: str = "agent",
) -> tuple[LangGraphApp, SyncLangGraphClient]:
    """Build a LangGraphApp + SDK client pair for testing."""
    saver = MemorySaver()
    graph = _build_graph(checkpointer=saver)
    app = LangGraphApp(platform_compat=True)
    if store is not None:
        app._thread_store = store
    app.register(graph=graph, name=name)
    client = _make_sdk_client(app.function_app)
    return app, client


# ---------------------------------------------------------------------------
# Tests — Assistants
# ---------------------------------------------------------------------------


class TestSdkAssistants:
    """Verify SDK AssistantsClient against our platform routes."""

    def test_search(self) -> None:
        """assistants.search() returns the registered graph as an assistant."""
        _, client = _make_app()
        results = client.assistants.search(limit=10, offset=0)

        assert isinstance(results, list)
        assert len(results) == 1
        assistant = results[0]
        assert assistant["assistant_id"] == "agent"
        assert assistant["graph_id"] == "agent"
        assert assistant["name"] == "agent"

    def test_get(self) -> None:
        """assistants.get() returns the assistant by ID."""
        _, client = _make_app()
        assistant = client.assistants.get("agent")

        assert assistant["assistant_id"] == "agent"
        assert assistant["graph_id"] == "agent"
        assert assistant["name"] == "agent"
        assert "created_at" in assistant
        assert "updated_at" in assistant

    def test_get_unknown_404(self) -> None:
        """assistants.get() for an unknown ID raises an error."""
        _, client = _make_app()
        with pytest.raises(NotFoundError):
            client.assistants.get("nonexistent")

    def test_count_all(self) -> None:
        """assistants.count() returns total number of registered graphs."""
        _, client = _make_app()
        result = client.assistants.count()
        assert result == 1

    def test_count_with_graph_id_filter(self) -> None:
        """assistants.count(graph_id=...) filters by graph_id."""
        _, client = _make_app()
        assert client.assistants.count(graph_id="agent") == 1
        assert client.assistants.count(graph_id="nonexistent") == 0

    def test_count_with_metadata_filter(self) -> None:
        """assistants.count(metadata=...) returns 0 (no user metadata on assistants)."""
        _, client = _make_app()
        assert client.assistants.count(metadata={"key": "value"}) == 0

    def test_count_with_name_filter(self) -> None:
        """assistants.count(name=...) filters by case-insensitive substring."""
        _, client = _make_app()
        assert client.assistants.count(name="agent") == 1
        assert client.assistants.count(name="AGENT") == 1
        assert client.assistants.count(name="age") == 1
        assert client.assistants.count(name="nonexistent") == 0

# ---------------------------------------------------------------------------
# Tests — Threads
# ---------------------------------------------------------------------------


class TestSdkThreads:
    """Verify SDK ThreadsClient against our platform routes."""

    def test_create(self) -> None:
        """threads.create() returns a thread with idle status."""
        _, client = _make_app()
        thread = client.threads.create()

        assert "thread_id" in thread
        assert thread["status"] == "idle"
        assert "created_at" in thread
        assert "updated_at" in thread

    def test_get(self) -> None:
        """threads.get() returns the created thread."""
        _, client = _make_app()
        created = client.threads.create()
        fetched = client.threads.get(created["thread_id"])

        assert fetched["thread_id"] == created["thread_id"]
        assert fetched["status"] == "idle"

    def test_get_state_after_run(self) -> None:
        """threads.get_state() returns persisted state after a run."""
        _, client = _make_app()
        thread = client.threads.create()
        tid = thread["thread_id"]

        # Run the graph first
        client.runs.wait(
            tid,
            "agent",
            input={"user_text": "SDK", "history": [], "turn_count": 0},
        )

        state = client.threads.get_state(tid)
        values = state["values"]
        assert isinstance(values, dict)
        assert values["turn_count"] == 1
        assert "Hello, SDK!" in values["history"]
        assert values["last_reply"] == "Hello, SDK!"

    def test_get_state_unbound_409(self) -> None:
        """threads.get_state() on unbound thread returns 409."""
        _, client = _make_app()
        thread = client.threads.create()

        with pytest.raises(ConflictError):
            client.threads.get_state(thread["thread_id"])

    def test_update(self) -> None:
        """threads.update() merges metadata."""
        _, client = _make_app()
        thread = client.threads.create(metadata={"a": 1})
        updated = client.threads.update(thread["thread_id"], metadata={"b": 2})
        assert updated["metadata"] == {"a": 1, "b": 2}

    def test_update_overwrite_key(self) -> None:
        """threads.update() overwrites existing metadata keys (shallow merge)."""
        _, client = _make_app()
        thread = client.threads.create(metadata={"x": "old"})
        updated = client.threads.update(thread["thread_id"], metadata={"x": "new"})
        assert updated["metadata"] == {"x": "new"}

    def test_update_nonexistent_404(self) -> None:
        """threads.update() on a nonexistent thread raises NotFoundError."""
        _, client = _make_app()
        with pytest.raises(NotFoundError):
            client.threads.update("nonexistent", metadata={"a": 1})

    def test_delete(self) -> None:
        """threads.delete() removes the thread."""
        _, client = _make_app()
        thread = client.threads.create()
        client.threads.delete(thread["thread_id"])
        with pytest.raises(NotFoundError):
            client.threads.get(thread["thread_id"])

    def test_delete_nonexistent_404(self) -> None:
        """threads.delete() on a nonexistent thread raises NotFoundError."""
        _, client = _make_app()
        with pytest.raises(NotFoundError):
            client.threads.delete("nonexistent")

    def test_update_nested_shallow_merge(self) -> None:
        """Shallow merge replaces nested dicts, does not deep-merge."""
        _, client = _make_app()
        thread = client.threads.create(metadata={"a": {"x": 1}})
        updated = client.threads.update(thread["thread_id"], metadata={"a": {"y": 2}})
        # Entire 'a' replaced, not deep-merged
        assert updated["metadata"] == {"a": {"y": 2}}
# ---------------------------------------------------------------------------
# Tests — Runs
# ---------------------------------------------------------------------------


class TestSdkRuns:
    """Verify SDK RunsClient against our platform routes."""

    def test_wait(self) -> None:
        """runs.wait() executes graph and returns final state values."""
        _, client = _make_app()
        thread = client.threads.create()
        tid = thread["thread_id"]

        result = client.runs.wait(
            tid,
            "agent",
            input={"user_text": "World", "history": [], "turn_count": 0},
        )

        # runs.wait returns the final state dict directly
        assert isinstance(result, dict)
        assert result["last_reply"] == "Hello, World!"
        assert result["turn_count"] == 1
        assert "Hello, World!" in result["history"]

    def test_multi_turn(self) -> None:
        """Two runs.wait() calls on the same thread accumulate state."""
        _, client = _make_app()
        thread = client.threads.create()
        tid = thread["thread_id"]

        # Turn 1
        out1 = client.runs.wait(
            tid,
            "agent",
            input={"user_text": "Alice", "history": [], "turn_count": 0},
        )
        assert isinstance(out1, dict)
        assert out1["turn_count"] == 1

        # Turn 2
        out2 = client.runs.wait(
            tid,
            "agent",
            input={"user_text": "Bob"},
        )
        assert isinstance(out2, dict)
        assert out2["turn_count"] == 2
        assert out2["history"] == ["Hello, Alice!", "Hello, Bob!"]
        assert out2["last_reply"] == "Hello, Bob!"

    def test_stream(self) -> None:
        """runs.stream() returns SSE events that the SDK parses correctly."""
        _, client = _make_app()
        thread = client.threads.create()
        tid = thread["thread_id"]

        events = list(
            client.runs.stream(
                tid,
                "agent",
                input={"user_text": "Stream", "history": [], "turn_count": 0},
                stream_mode="values",
            )
        )

        # Verify event ordering: metadata is first, end is last
        assert events[0].event == "metadata"
        assert events[-1].event == "end"

        # Verify metadata has run_id
        assert events[0].data is not None
        assert "run_id" in events[0].data

        # Check event types
        event_types = [e.event for e in events]
        assert "values" in event_types

        # Verify values event content — last values event has final state
        values_events = [e for e in events if e.event == "values"]
        assert len(values_events) >= 1
        final = values_events[-1].data
        assert final["turn_count"] == 1
        assert final["history"] == ["Hello, Stream!"]
        assert final["last_reply"] == "Hello, Stream!"

    def test_stream_multi_turn(self) -> None:
        """Streaming also supports multi-turn conversations."""
        _, client = _make_app()
        thread = client.threads.create()
        tid = thread["thread_id"]

        # Turn 1 via stream
        events1 = list(
            client.runs.stream(
                tid,
                "agent",
                input={"user_text": "Turn1", "history": [], "turn_count": 0},
                stream_mode="values",
            )
        )
        values1 = [e for e in events1 if e.event == "values"]
        assert values1[-1].data["turn_count"] == 1

        # Turn 2 via stream
        events2 = list(
            client.runs.stream(
                tid,
                "agent",
                input={"user_text": "Turn2"},
                stream_mode="values",
            )
        )
        values2 = [e for e in events2 if e.event == "values"]
        final = values2[-1].data
        assert final["turn_count"] == 2
        assert final["history"] == ["Hello, Turn1!", "Hello, Turn2!"]


# ---------------------------------------------------------------------------
# Tests — Unsupported features (501)
# ---------------------------------------------------------------------------


class TestSdkUnsupported:
    """Verify that unsupported features return 501 via the SDK."""

    def test_multi_stream_mode_501(self) -> None:
        """Passing multiple stream_modes to runs.stream() returns 501."""
        _, client = _make_app()
        thread = client.threads.create()
        tid = thread["thread_id"]

        with pytest.raises(InternalServerError):
            # Multi-stream-mode is not supported → 501
            list(
                client.runs.stream(
                    tid,
                    "agent",
                    input={"user_text": "test"},
                    stream_mode=["values", "updates"],
                )
            )

    def test_interrupt_before_501(self) -> None:
        """Passing interrupt_before to runs.wait() returns 501."""
        _, client = _make_app()
        thread = client.threads.create()
        tid = thread["thread_id"]

        with pytest.raises(httpx.HTTPStatusError, match="501"):
            client.runs.wait(
                tid,
                "agent",
                input={"user_text": "test"},
                interrupt_before=["greet"],
            )

    def test_interrupt_after_501(self) -> None:
        """Passing interrupt_after to runs.wait() returns 501."""
        _, client = _make_app()
        thread = client.threads.create()
        tid = thread["thread_id"]

        with pytest.raises(httpx.HTTPStatusError, match="501"):
            client.runs.wait(
                tid,
                "agent",
                input={"user_text": "test"},
                interrupt_after=["greet"],
            )

    def test_webhook_501(self) -> None:
        """Passing webhook to runs.wait() returns 501."""
        _, client = _make_app()
        thread = client.threads.create()
        tid = thread["thread_id"]

        with pytest.raises(httpx.HTTPStatusError, match="501"):
            client.runs.wait(
                tid,
                "agent",
                input={"user_text": "test"},
                webhook="https://example.com/hook",
            )


# ---------------------------------------------------------------------------
# Tests — Transport bridge edge cases
# ---------------------------------------------------------------------------


class TestTransportBridge:
    """Verify transport bridge robustness."""

    def test_api_prefix_stripping(self) -> None:
        """Requests with /api prefix are correctly routed."""
        app_inst, _ = _make_app()
        transport = _make_transport(app_inst.function_app)
        http = httpx.Client(transport=transport, base_url="http://test")

        # Request with /api prefix
        resp = http.post(
            "http://test/api/assistants/search",
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["assistant_id"] == "agent"
        http.close()

    def test_route_parity(self) -> None:
        """_ROUTE_TABLE covers all platform routes registered on FunctionApp."""
        app_inst, _ = _make_app()
        fa = app_inst.function_app

        # Collect all platform function names from the FunctionApp
        fa.functions_bindings = {}
        platform_fn_names = {
            name
            for fn in fa.get_functions()
            if (name := fn.get_function_name()) is not None
            and name.startswith("aflg_platform_")
        }

        # Collect all function names referenced in _ROUTE_TABLE
        route_table_fn_names = {fn_name for _, _, fn_name, _ in _ROUTE_TABLE}

        # Every platform function must appear in the route table
        missing = platform_fn_names - route_table_fn_names
        assert not missing, (
            f"Platform functions not covered by _ROUTE_TABLE: {missing}"
        )

        # Every route table entry must correspond to a real function
        extra = route_table_fn_names - platform_fn_names
        assert not extra, (
            f"_ROUTE_TABLE references non-existent functions: {extra}"
        )
