"""Tests for Platform API–compatible route layer (issue #38)."""

from __future__ import annotations

import json
from typing import Any, Iterator

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.platform.routes import (
    PlatformRouteDeps,
    _platform_error,
)
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from tests.conftest import (
    FakeCompiledGraph,
    FakeInvokeOnlyGraph,
    FakeStatefulGraph,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store() -> InMemoryThreadStore:
    """Thread store with deterministic IDs."""
    counter = iter(range(1000))
    return InMemoryThreadStore(id_factory=lambda: f"thread-{next(counter)}")


@pytest.fixture()
def graph() -> FakeCompiledGraph:
    return FakeCompiledGraph()


@pytest.fixture()
def stateful_graph() -> FakeStatefulGraph:
    return FakeStatefulGraph()


def _build_platform_app(
    *,
    graphs: dict[str, Any] | None = None,
    store: InMemoryThreadStore | None = None,
) -> LangGraphApp:
    """Build a LangGraphApp with platform_compat=True and register graphs."""
    app = LangGraphApp(platform_compat=True)
    if store is not None:
        app._thread_store = store
    if graphs:
        for name, g in graphs.items():
            app.register(graph=g, name=name)
    return app


def _get_fn(fa: func.FunctionApp, fn_name: str) -> Any:
    """Get a registered function handler by name from a FunctionApp."""
    fa.functions_bindings = {}
    for fn in fa.get_functions():
        if fn.get_function_name() == fn_name:
            return fn.get_user_function()
    raise AssertionError(f"Function {fn_name!r} not found")


def _post_request(
    url: str,
    body: dict[str, Any] | None = None,
    **route_params: str,
) -> func.HttpRequest:
    """Build a POST request with JSON body."""
    return func.HttpRequest(
        method="POST",
        url=url,
        body=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
        route_params=route_params,
    )


def _get_request(url: str, **route_params: str) -> func.HttpRequest:
    """Build a GET request."""
    return func.HttpRequest(
        method="GET",
        url=url,
        body=b"",
        route_params=route_params,
    )


def _patch_request(
    url: str,
    body: dict[str, Any] | None = None,
    **route_params: str,
) -> func.HttpRequest:
    """Build a PATCH request with JSON body."""
    return func.HttpRequest(
        method="PATCH",
        url=url,
        body=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
        route_params=route_params,
    )


def _delete_request(url: str, **route_params: str) -> func.HttpRequest:
    """Build a DELETE request."""
    return func.HttpRequest(
        method="DELETE",
        url=url,
        body=b"",
        route_params=route_params,
    )

# ---------------------------------------------------------------------------
# LangGraphApp integration — platform_compat flag
# ---------------------------------------------------------------------------


class TestPlatformCompatFlag:
    def test_platform_routes_registered_when_enabled(self, graph: FakeCompiledGraph) -> None:
        app = _build_platform_app(graphs={"agent": graph})
        fa = app.function_app

        fa.functions_bindings = {}
        fn_names = [f.get_function_name() for f in fa.get_functions()]

        # All 14 platform route names must be present
        expected = {
            "aflg_platform_assistants_search",
            "aflg_platform_assistants_count",
            "aflg_platform_assistants_get",
            "aflg_platform_threads_create",
            "aflg_platform_threads_get",
            "aflg_platform_threads_search",
            "aflg_platform_threads_count",
            "aflg_platform_threads_update",
            "aflg_platform_threads_delete",
            "aflg_platform_threads_state_get",
            "aflg_platform_runs_wait",
            "aflg_platform_runs_stream",
            "aflg_platform_runs_wait_threadless",
            "aflg_platform_runs_stream_threadless",
        }
        assert expected.issubset(set(fn_names))

    def test_platform_routes_not_registered_when_disabled(self, graph: FakeCompiledGraph) -> None:
        app = LangGraphApp(platform_compat=False)
        app.register(graph=graph, name="agent")
        fa = app.function_app

        fa.functions_bindings = {}
        fn_names = [f.get_function_name() for f in fa.get_functions()]

        assert "aflg_platform_assistants_search" not in fn_names

    def test_auto_creates_thread_store(self) -> None:
        app = LangGraphApp(platform_compat=True)
        assert app.thread_store is not None
        assert isinstance(app.thread_store, InMemoryThreadStore)

    def test_no_thread_store_when_disabled(self) -> None:
        app = LangGraphApp(platform_compat=False)
        assert app.thread_store is None

    def test_custom_thread_store(self, store: InMemoryThreadStore) -> None:
        app = LangGraphApp(platform_compat=True)
        app.thread_store = store
        assert app.thread_store is store


# ---------------------------------------------------------------------------
# Assistants endpoints
# ---------------------------------------------------------------------------


class TestAssistantsSearch:
    def test_search_returns_all(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_search")

        req = _post_request("/api/assistants/search")
        resp = fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert len(data) == 1
        assert data[0]["assistant_id"] == "agent"
        assert data[0]["graph_id"] == "agent"

    def test_search_multiple_graphs(self, store: InMemoryThreadStore) -> None:
        g1 = FakeCompiledGraph()
        g2 = FakeCompiledGraph()
        app = _build_platform_app(graphs={"alpha": g1, "beta": g2}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_search")

        req = _post_request("/api/assistants/search")
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 2
        names = {a["assistant_id"] for a in data}
        assert names == {"alpha", "beta"}

    def test_search_filter_by_graph_id(self, store: InMemoryThreadStore) -> None:
        g1 = FakeCompiledGraph()
        g2 = FakeCompiledGraph()
        app = _build_platform_app(graphs={"alpha": g1, "beta": g2}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_search")

        req = _post_request("/api/assistants/search", {"graph_id": "alpha"})
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 1
        assert data[0]["assistant_id"] == "alpha"

    def test_search_with_limit(self, store: InMemoryThreadStore) -> None:
        graphs = {f"g{i}": FakeCompiledGraph() for i in range(5)}
        app = _build_platform_app(graphs=graphs, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_search")

        req = _post_request("/api/assistants/search", {"limit": 2})
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 2

    def test_search_with_offset(self, store: InMemoryThreadStore) -> None:
        graphs = {f"g{i}": FakeCompiledGraph() for i in range(5)}
        app = _build_platform_app(graphs=graphs, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_search")

        req = _post_request("/api/assistants/search", {"limit": 2, "offset": 3})
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 2

    def test_search_empty_body(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        """POST with no body should still work (defaults)."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_search")

        req = func.HttpRequest(
            method="POST",
            url="/api/assistants/search",
            body=b"",
            headers={},
        )
        resp = fn(req)
        assert resp.status_code == 200

    def test_search_filter_by_name(self, store: InMemoryThreadStore) -> None:
        g1 = FakeCompiledGraph()
        g2 = FakeCompiledGraph()
        app = _build_platform_app(graphs={"chatbot": g1, "summarizer": g2}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_search")

        # Substring match
        req = _post_request("/api/assistants/search", {"name": "chat"})
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 1
        assert data[0]["assistant_id"] == "chatbot"

        # Case-insensitive
        req = _post_request("/api/assistants/search", {"name": "SUMM"})
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 1
        assert data[0]["assistant_id"] == "summarizer"

        # No match
        req = _post_request("/api/assistants/search", {"name": "nonexistent"})
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 0



class TestAssistantsCount:
    def test_count_all(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_count")

        req = _post_request("/api/assistants/count")
        resp = fn(req)
        assert resp.status_code == 200
        assert json.loads(resp.get_body()) == 1

    def test_count_multiple_graphs(self, store: InMemoryThreadStore) -> None:
        g1 = FakeCompiledGraph()
        g2 = FakeCompiledGraph()
        app = _build_platform_app(graphs={"alpha": g1, "beta": g2}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_count")

        req = _post_request("/api/assistants/count")
        resp = fn(req)
        assert resp.status_code == 200
        assert json.loads(resp.get_body()) == 2

    def test_count_filter_by_graph_id(self, store: InMemoryThreadStore) -> None:
        g1 = FakeCompiledGraph()
        g2 = FakeCompiledGraph()
        app = _build_platform_app(graphs={"alpha": g1, "beta": g2}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_count")

        req = _post_request("/api/assistants/count", {"graph_id": "alpha"})
        resp = fn(req)
        assert resp.status_code == 200
        assert json.loads(resp.get_body()) == 1

    def test_count_filter_by_graph_id_no_match(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": FakeCompiledGraph()}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_count")

        req = _post_request("/api/assistants/count", {"graph_id": "nonexistent"})
        resp = fn(req)
        assert resp.status_code == 200
        assert json.loads(resp.get_body()) == 0

    def test_count_filter_by_metadata_returns_zero(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """Assistants don't have user metadata, so any metadata filter yields 0."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_count")

        req = _post_request("/api/assistants/count", {"metadata": {"key": "val"}})
        resp = fn(req)
        assert resp.status_code == 200
        assert json.loads(resp.get_body()) == 0

    def test_count_filter_by_name_substring(self, store: InMemoryThreadStore) -> None:
        g1 = FakeCompiledGraph()
        g2 = FakeCompiledGraph()
        app = _build_platform_app(graphs={"chatbot": g1, "summarizer": g2}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_count")

        # Exact match
        req = _post_request("/api/assistants/count", {"name": "chatbot"})
        resp = fn(req)
        assert json.loads(resp.get_body()) == 1

        # Substring match
        req = _post_request("/api/assistants/count", {"name": "chat"})
        resp = fn(req)
        assert json.loads(resp.get_body()) == 1

        # Case-insensitive
        req = _post_request("/api/assistants/count", {"name": "CHAT"})
        resp = fn(req)
        assert json.loads(resp.get_body()) == 1

        # No match
        req = _post_request("/api/assistants/count", {"name": "nonexistent"})
        resp = fn(req)
        assert json.loads(resp.get_body()) == 0

    def test_count_empty_body(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        """POST with no body should still work."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_count")

        req = func.HttpRequest(
            method="POST",
            url="/api/assistants/count",
            body=b"",
            headers={},
        )
        resp = fn(req)
        assert resp.status_code == 200
        assert json.loads(resp.get_body()) == 1

    def test_count_invalid_json(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        """Malformed JSON returns 400."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_count")

        req = func.HttpRequest(
            method="POST",
            url="/api/assistants/count",
            body=b"not json",
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400
class TestAssistantsGet:
    def test_get_existing(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_get")

        req = _get_request("/api/assistants/agent", assistant_id="agent")
        resp = fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["assistant_id"] == "agent"
        assert data["name"] == "agent"

    def test_get_not_found(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_get")

        req = _get_request("/api/assistants/missing", assistant_id="missing")
        resp = fn(req)
        assert resp.status_code == 404
        data = json.loads(resp.get_body())
        assert "not found" in data["detail"]


# ---------------------------------------------------------------------------
# Threads endpoints
# ---------------------------------------------------------------------------


class TestThreadsCreate:
    def test_create_thread(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_create")

        req = _post_request("/api/threads", {})
        resp = fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["thread_id"] == "thread-0"
        assert data["status"] == "idle"

    def test_create_thread_with_metadata(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_create")

        req = _post_request("/api/threads", {"metadata": {"key": "value"}})
        resp = fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["metadata"]["key"] == "value"

    def test_create_thread_empty_body(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_create")

        req = func.HttpRequest(
            method="POST",
            url="/api/threads",
            body=b"",
            headers={},
        )
        resp = fn(req)
        assert resp.status_code == 200


class TestThreadsGet:
    def test_get_existing_thread(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        # Create thread first
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post_request("/api/threads", {}))
        thread_id = json.loads(resp.get_body())["thread_id"]

        # Get thread
        fa.functions_bindings = {}
        get_fn = _get_fn(fa, "aflg_platform_threads_get")
        req = _get_request(f"/api/threads/{thread_id}", thread_id=thread_id)
        resp = get_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["thread_id"] == thread_id

    def test_get_not_found(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_get")

        req = _get_request("/api/threads/missing", thread_id="missing")
        resp = fn(req)
        assert resp.status_code == 404
        data = json.loads(resp.get_body())
        assert "not found" in data["detail"]


# ---------------------------------------------------------------------------
# Thread update (PATCH) endpoint
# ---------------------------------------------------------------------------


class TestThreadsUpdate:
    def test_update_metadata(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        # Create thread with initial metadata
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post_request("/api/threads", {"metadata": {"key": "old"}}))
        thread_id = json.loads(resp.get_body())["thread_id"]

        # Update metadata
        fa.functions_bindings = {}
        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = _patch_request(
            f"/api/threads/{thread_id}",
            {"metadata": {"key": "new", "extra": "val"}},
            thread_id=thread_id,
        )
        resp = update_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["thread_id"] == thread_id
        # Shallow merge: old key overwritten, new key added
        assert data["metadata"] == {"key": "new", "extra": "val"}

    def test_update_merge_preserves_existing_keys(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        # Create with metadata
        thread = store.create(metadata={"a": 1, "b": 2})

        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = _patch_request(
            f"/api/threads/{thread.thread_id}",
            {"metadata": {"b": 99, "c": 3}},
            thread_id=thread.thread_id,
        )
        resp = update_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        # a kept, b overwritten, c added
        assert data["metadata"] == {"a": 1, "b": 99, "c": 3}

    def test_update_no_metadata_returns_unchanged(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """PATCH with no metadata field returns thread unchanged."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create(metadata={"x": 1})

        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = _patch_request(
            f"/api/threads/{thread.thread_id}", {},
            thread_id=thread.thread_id,
        )
        resp = update_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["metadata"] == {"x": 1}

    def test_update_not_found(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_update")

        req = _patch_request(
            "/api/threads/missing", {"metadata": {"x": 1}},
            thread_id="missing",
        )
        resp = fn(req)
        assert resp.status_code == 404
        data = json.loads(resp.get_body())
        assert "not found" in data["detail"]

    def test_update_empty_body(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """PATCH with empty body returns thread unchanged."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create()

        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = func.HttpRequest(
            method="PATCH",
            url=f"/api/threads/{thread.thread_id}",
            body=b"",
            headers={},
            route_params={"thread_id": thread.thread_id},
        )
        resp = update_fn(req)
        assert resp.status_code == 200

    def test_update_invalid_json(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create()

        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = func.HttpRequest(
            method="PATCH",
            url=f"/api/threads/{thread.thread_id}",
            body=b"not json",
            headers={"Content-Type": "application/json"},
            route_params={"thread_id": thread.thread_id},
        )
        resp = update_fn(req)
        assert resp.status_code == 400

    def test_update_ttl_field_ignored(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """SDK sends ttl field; it should be silently dropped."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create()

        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = _patch_request(
            f"/api/threads/{thread.thread_id}",
            {"metadata": {"k": "v"}, "ttl": {"days": 7}},
            thread_id=thread.thread_id,
        )
        resp = update_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["metadata"] == {"k": "v"}
        assert "ttl" not in data

    def test_update_empty_metadata_is_noop(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """PATCH with metadata={} means 'no new keys' — not 'clear'."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create(metadata={"keep": "me"})

        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = _patch_request(
            f"/api/threads/{thread.thread_id}",
            {"metadata": {}},
            thread_id=thread.thread_id,
        )
        resp = update_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["metadata"] == {"keep": "me"}

    def test_update_metadata_on_thread_with_none_metadata(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """PATCH metadata on a thread that has no existing metadata."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create()  # metadata=None by default

        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = _patch_request(
            f"/api/threads/{thread.thread_id}",
            {"metadata": {"new_key": "new_val"}},
            thread_id=thread.thread_id,
        )
        resp = update_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["metadata"] == {"new_key": "new_val"}

    def test_update_nested_metadata_shallow_merge(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """Shallow merge replaces nested dicts, does not deep-merge."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create(metadata={"a": {"x": 1}})

        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = _patch_request(
            f"/api/threads/{thread.thread_id}",
            {"metadata": {"a": {"y": 2}}},
            thread_id=thread.thread_id,
        )
        resp = update_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        # Shallow merge: entire 'a' value replaced, NOT deep-merged
        assert data["metadata"] == {"a": {"y": 2}}

    def test_update_metadata_wrong_type_422(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """metadata must be a dict; other types return 422."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create()

        update_fn = _get_fn(fa, "aflg_platform_threads_update")
        req = _patch_request(
            f"/api/threads/{thread.thread_id}",
            {"metadata": "not-a-dict"},
            thread_id=thread.thread_id,
        )
        resp = update_fn(req)
        assert resp.status_code == 422

# ---------------------------------------------------------------------------
# Thread delete (DELETE) endpoint
# ---------------------------------------------------------------------------


class TestThreadsDelete:
    def test_delete_thread(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create()

        delete_fn = _get_fn(fa, "aflg_platform_threads_delete")
        req = _delete_request(
            f"/api/threads/{thread.thread_id}",
            thread_id=thread.thread_id,
        )
        resp = delete_fn(req)
        assert resp.status_code == 204
        assert resp.get_body() == b""

        # Verify thread is gone
        assert store.get(thread.thread_id) is None

    def test_delete_not_found(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_delete")

        req = _delete_request("/api/threads/missing", thread_id="missing")
        resp = fn(req)
        assert resp.status_code == 404
        data = json.loads(resp.get_body())
        assert "not found" in data["detail"]

    def test_delete_then_get_returns_404(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """After delete, GET returns 404."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        thread = store.create()

        # Delete
        delete_fn = _get_fn(fa, "aflg_platform_threads_delete")
        req = _delete_request(
            f"/api/threads/{thread.thread_id}",
            thread_id=thread.thread_id,
        )
        delete_fn(req)

        # GET should 404
        fa.functions_bindings = {}
        get_fn = _get_fn(fa, "aflg_platform_threads_get")
        req = _get_request(
            f"/api/threads/{thread.thread_id}",
            thread_id=thread.thread_id,
        )
        resp = get_fn(req)
        assert resp.status_code == 404


class TestThreadsSearch:
    def test_search_all(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        store.create(metadata={"env": "prod"})
        store.create(metadata={"env": "dev"})
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = _post_request("/api/threads/search")
        resp = fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert len(data) == 2

    def test_search_by_status(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        t1 = store.create()
        store.create()
        store.update(t1.thread_id, status="busy")
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = _post_request("/api/threads/search", {"status": "busy"})
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 1
        assert data[0]["status"] == "busy"

    def test_search_by_metadata(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        store.create(metadata={"env": "prod"})
        store.create(metadata={"env": "dev"})
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = _post_request("/api/threads/search", {"metadata": {"env": "prod"}})
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 1
        assert data[0]["metadata"]["env"] == "prod"

    def test_search_with_limit_offset(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        for _ in range(5):
            store.create()
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = _post_request("/api/threads/search", {"limit": 2, "offset": 1})
        resp = fn(req)
        data = json.loads(resp.get_body())
        assert len(data) == 2

    def test_search_empty_body(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        store.create()
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = func.HttpRequest(method="POST", url="/api/threads/search", body=b"", headers={})
        resp = fn(req)
        assert resp.status_code == 200

    def test_search_invalid_json(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = func.HttpRequest(
            method="POST",
            url="/api/threads/search",
            body=b"not json",
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400

    def test_search_unsupported_field_501(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = _post_request("/api/threads/search", {"values": [1, 2]})
        resp = fn(req)
        assert resp.status_code == 501
        data = json.loads(resp.get_body())
        assert "Unsupported" in data["detail"]

    def test_search_unsupported_sort_by_501(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = _post_request("/api/threads/search", {"sort_by": "created_at"})
        resp = fn(req)
        assert resp.status_code == 501

    @pytest.mark.parametrize("body_bytes,desc", [
        (b"null", "null"),
        (b"[1,2]", "array"),
        (b'"abc"', "string"),
        (b"123", "integer"),
    ])
    def test_search_non_object_json_400(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
        body_bytes: bytes, desc: str,
    ) -> None:
        """Valid JSON that is not an object must return 400."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = func.HttpRequest(
            method="POST",
            url="/api/threads/search",
            body=body_bytes,
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400, f"Expected 400 for {desc}"
        assert "JSON object" in json.loads(resp.get_body())["detail"]

    def test_search_invalid_status_422(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = _post_request("/api/threads/search", {"status": "nonexistent"})
        resp = fn(req)
        assert resp.status_code == 422

    def test_search_offset_past_end(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """Offset past all results returns empty list, not error."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        store.create()
        fn = _get_fn(fa, "aflg_platform_threads_search")
        req = _post_request("/api/threads/search", {"offset": 100})
        resp = fn(req)
        assert resp.status_code == 200
        assert json.loads(resp.get_body()) == []

class TestThreadsCount:
    def test_count_all(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        store.create()
        store.create()
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = _post_request("/api/threads/count")
        resp = fn(req)
        assert resp.status_code == 200
        assert json.loads(resp.get_body()) == 2

    def test_count_by_status(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        t1 = store.create()
        store.create()
        store.update(t1.thread_id, status="busy")
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = _post_request("/api/threads/count", {"status": "busy"})
        resp = fn(req)
        assert json.loads(resp.get_body()) == 1

    def test_count_by_metadata(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        store.create(metadata={"env": "prod"})
        store.create(metadata={"env": "dev"})
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = _post_request("/api/threads/count", {"metadata": {"env": "prod"}})
        resp = fn(req)
        assert json.loads(resp.get_body()) == 1

    def test_count_empty_store(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = _post_request("/api/threads/count")
        resp = fn(req)
        assert json.loads(resp.get_body()) == 0

    def test_count_empty_body(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        store.create()
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = func.HttpRequest(method="POST", url="/api/threads/count", body=b"", headers={})
        resp = fn(req)
        assert resp.status_code == 200

    def test_count_invalid_json(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = func.HttpRequest(
            method="POST",
            url="/api/threads/count",
            body=b"not json",
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400

    def test_count_unsupported_field_501(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = _post_request("/api/threads/count", {"values": [1]})
        resp = fn(req)
        assert resp.status_code == 501

    def test_count_returns_raw_int(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        """Count endpoint returns raw integer, not {"count": N}."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        store.create()
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = _post_request("/api/threads/count")
        resp = fn(req)
        body = resp.get_body()
        assert json.loads(body) == 1
        assert body == b"1"  # Raw integer, not wrapped

    @pytest.mark.parametrize("body_bytes,desc", [
        (b"null", "null"),
        (b"[1,2]", "array"),
        (b'"abc"', "string"),
        (b"123", "integer"),
    ])
    def test_count_non_object_json_400(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
        body_bytes: bytes, desc: str,
    ) -> None:
        """Valid JSON that is not an object must return 400."""
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = func.HttpRequest(
            method="POST",
            url="/api/threads/count",
            body=body_bytes,
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400, f"Expected 400 for {desc}"
        assert "JSON object" in json.loads(resp.get_body())["detail"]

    def test_count_invalid_status_422(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_count")
        req = _post_request("/api/threads/count", {"status": "nonexistent"})
        resp = fn(req)
        assert resp.status_code == 422
# ---------------------------------------------------------------------------
# Thread state endpoint
# ---------------------------------------------------------------------------


class TestThreadsStateGet:
    def test_thread_not_found(self, graph: FakeCompiledGraph, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_state_get")

        req = _get_request("/api/threads/missing/state", thread_id="missing")
        resp = fn(req)
        assert resp.status_code == 404

    def test_thread_not_bound_to_assistant(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app

        # Create a thread (not bound)
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post_request("/api/threads", {}))
        thread_id = json.loads(resp.get_body())["thread_id"]

        # Get state — should 409 because not bound
        fa.functions_bindings = {}
        state_fn = _get_fn(fa, "aflg_platform_threads_state_get")
        req = _get_request(f"/api/threads/{thread_id}/state", thread_id=thread_id)
        resp = state_fn(req)
        assert resp.status_code == 409
        data = json.loads(resp.get_body())
        assert "not bound" in data["detail"]

    def test_state_with_stateful_graph(
        self, store: InMemoryThreadStore
    ) -> None:
        sg = FakeStatefulGraph()
        app = _build_platform_app(graphs={"agent": sg}, store=store)
        fa = app.function_app

        # Create thread and bind via a run
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post_request("/api/threads", {}))
        thread_id = json.loads(resp.get_body())["thread_id"]

        # Run to bind assistant
        fa.functions_bindings = {}
        wait_fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {"messages": []}},
            thread_id=thread_id,
        )
        wait_fn(req)

        # Now get state
        fa.functions_bindings = {}
        state_fn = _get_fn(fa, "aflg_platform_threads_state_get")
        req = _get_request(f"/api/threads/{thread_id}/state", thread_id=thread_id)
        resp = state_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert "values" in data
        assert "next" in data

    def test_state_assistant_not_found(self, store: InMemoryThreadStore) -> None:
        """If thread's assistant_id references a graph that no longer exists."""
        sg = FakeStatefulGraph()
        app = _build_platform_app(graphs={"agent": sg}, store=store)

        # Create and bind thread manually
        thread = store.create()
        store.update(thread.thread_id, assistant_id="vanished")

        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_state_get")
        req = _get_request(
            f"/api/threads/{thread.thread_id}/state",
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 404
        data = json.loads(resp.get_body())
        assert "vanished" in data["detail"]

    def test_state_graph_not_stateful(self, store: InMemoryThreadStore) -> None:
        """Graph bound to thread doesn't support get_state."""
        g = FakeCompiledGraph()  # has invoke/stream but NOT get_state
        app = _build_platform_app(graphs={"agent": g}, store=store)

        thread = store.create()
        store.update(thread.thread_id, assistant_id="agent")

        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_threads_state_get")
        req = _get_request(
            f"/api/threads/{thread.thread_id}/state",
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Runs/wait endpoint
# ---------------------------------------------------------------------------


class TestRunsWait:
    def test_invoke_success(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        # Create thread
        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post_request("/api/threads", {}))
        thread_id = json.loads(resp.get_body())["thread_id"]

        # Run
        fa.functions_bindings = {}
        wait_fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {"messages": [{"role": "human", "content": "hi"}]}},
            thread_id=thread_id,
        )
        resp = wait_fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        # SDK returns final state values (dict)
        assert isinstance(data, dict)
        assert "messages" in data

    def test_thread_becomes_idle_after_run(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        create_fn = _get_fn(fa, "aflg_platform_threads_create")
        resp = create_fn(_post_request("/api/threads", {}))
        thread_id = json.loads(resp.get_body())["thread_id"]

        fa.functions_bindings = {}
        wait_fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread_id,
        )
        wait_fn(req)

        thread = store.get(thread_id)
        assert thread is not None
        assert thread.status == "idle"
        assert thread.assistant_id == "agent"

    def test_thread_not_found(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait")

        req = _post_request(
            "/api/threads/missing/runs/wait",
            {"assistant_id": "agent", "input": {}},
            thread_id="missing",
        )
        resp = fn(req)
        assert resp.status_code == 404

    def test_assistant_not_found(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "nonexistent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 404
        data = json.loads(resp.get_body())
        assert "nonexistent" in data["detail"]

    def test_thread_assistant_binding_immutable(self, store: InMemoryThreadStore) -> None:
        """Once a thread is bound to an assistant, running with a different one is 409."""
        g1 = FakeCompiledGraph()
        g2 = FakeCompiledGraph()
        app = _build_platform_app(graphs={"alpha": g1, "beta": g2}, store=store)
        fa = app.function_app

        # Create thread and bind to 'alpha'
        thread = store.create()
        store.update(thread.thread_id, assistant_id="alpha")

        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "beta", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 409
        data = json.loads(resp.get_body())
        assert "bound to assistant" in data["detail"]

    def test_invalid_json_body(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = func.HttpRequest(
            method="POST",
            url=f"/api/threads/{thread.thread_id}/runs/wait",
            body=b"not json",
            headers={"Content-Type": "application/json"},
            route_params={"thread_id": thread.thread_id},
        )
        resp = fn(req)
        assert resp.status_code == 400

    def test_validation_error(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_wait")
        # Missing required assistant_id
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 422

    def test_graph_execution_failure(self, store: InMemoryThreadStore) -> None:
        class FailingGraph:
            def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
                raise RuntimeError("boom")

            def stream(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
                stream_mode: str = "values",
            ) -> Iterator[Any]:
                yield {}

        app = _build_platform_app(graphs={"agent": FailingGraph()}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 500

        # Thread should be in error state
        updated = store.get(thread.thread_id)
        assert updated is not None
        assert updated.status == "error"

    def test_with_user_config(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {
                "assistant_id": "agent",
                "input": {},
                "config": {"configurable": {"extra_key": "val"}},
            },
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Runs/stream endpoint
# ---------------------------------------------------------------------------


class TestRunsStream:
    def test_stream_success(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200
        body = resp.get_body().decode()

        # Should have metadata event, data events, and end event
        assert "event: metadata" in body
        assert "run_id" in body
        assert "event: values" in body
        assert "event: end" in body

    def test_stream_thread_not_found(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream")

        req = _post_request(
            "/api/threads/missing/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id="missing",
        )
        resp = fn(req)
        assert resp.status_code == 404

    def test_stream_assistant_not_found(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "nonexistent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 404

    def test_stream_not_streamable(self, store: InMemoryThreadStore) -> None:
        """Graph that doesn't support streaming should return 501."""
        g = FakeInvokeOnlyGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 501

    def test_stream_thread_binding_mismatch(self, store: InMemoryThreadStore) -> None:
        g1 = FakeCompiledGraph()
        g2 = FakeCompiledGraph()
        app = _build_platform_app(graphs={"alpha": g1, "beta": g2}, store=store)
        fa = app.function_app

        thread = store.create()
        store.update(thread.thread_id, assistant_id="alpha")

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "beta", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 409

    def test_stream_thread_becomes_idle(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        fn(req)

        updated = store.get(thread.thread_id)
        assert updated is not None
        assert updated.status == "idle"
        assert updated.assistant_id == "agent"

    def test_stream_failure_sets_error_state(self, store: InMemoryThreadStore) -> None:
        class FailingStreamGraph:
            def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
                return {}

            def stream(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
                stream_mode: str = "values",
            ) -> Iterator[Any]:
                raise RuntimeError("stream boom")

        app = _build_platform_app(graphs={"agent": FailingStreamGraph()}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200  # SSE always 200
        body = resp.get_body().decode()
        assert "event: error" in body
        assert "event: end" in body

        updated = store.get(thread.thread_id)
        assert updated is not None
        assert updated.status == "error"

    def test_stream_invalid_json(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = func.HttpRequest(
            method="POST",
            url=f"/api/threads/{thread.thread_id}/runs/stream",
            body=b"not json",
            headers={"Content-Type": "application/json"},
            route_params={"thread_id": thread.thread_id},
        )
        resp = fn(req)
        assert resp.status_code == 400

    def test_stream_max_bytes_exceeded(self, store: InMemoryThreadStore) -> None:
        """Stream should stop when max buffered bytes exceeded."""

        class BigStreamGraph:
            def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
                return {}

            def stream(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
                stream_mode: str = "values",
            ) -> Iterator[dict[str, Any]]:
                for i in range(1000):
                    yield {"data": "x" * 1000}

        app = LangGraphApp(platform_compat=True, max_stream_response_bytes=500)
        app._thread_store = store
        app.register(graph=BigStreamGraph(), name="agent")
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        body = resp.get_body().decode()
        assert "event: error" in body
        assert "max buffered size" in body
        # Must end with end event (SDK protocol requirement)
        assert "event: end\ndata: null" in body
        # Thread should be marked as error on overflow
        updated = store.get(thread.thread_id)
        assert updated is not None
        assert updated.status == "error"
        assert updated.status == "error"


# ---------------------------------------------------------------------------
# Preflight validation (501 for unsupported features)
# ---------------------------------------------------------------------------


class TestPreflightValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("interrupt_before", ["node_a"]),
            ("interrupt_after", ["node_b"]),
            ("webhook", "https://example.com"),
            ("on_completion", "callback"),
            ("after_seconds", 10.0),
            ("if_not_exists", "create"),
            ("checkpoint_id", "cp-123"),
        ],
    )
    def test_unsupported_field_returns_501(
        self, store: InMemoryThreadStore, field: str, value: Any
    ) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_wait")
        body = {"assistant_id": "agent", "input": {}, field: value}
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            body,
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 501

    def test_multitask_reject_allowed(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {}, "multitask_strategy": "reject"},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200

    def test_multitask_non_reject_returns_501(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {}, "multitask_strategy": "enqueue"},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 501

    @pytest.mark.parametrize(
        "field,value",
        [
            ("interrupt_before", ["*"]),
            ("webhook", "https://hooks.example.com/callback"),
        ],
    )
    def test_unsupported_field_in_stream_returns_501(
        self, store: InMemoryThreadStore, field: str, value: Any
    ) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        body = {"assistant_id": "agent", "input": {}, field: value}
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            body,
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Platform error helper
# ---------------------------------------------------------------------------


class TestPlatformError:
    def test_error_format(self) -> None:
        resp = _platform_error(418, "I'm a teapot")
        assert resp.status_code == 418
        data = json.loads(resp.get_body())
        assert data["detail"] == "I'm a teapot"
        assert resp.mimetype == "application/json"


# ---------------------------------------------------------------------------
# PlatformRouteDeps
# ---------------------------------------------------------------------------


class TestPlatformRouteDeps:
    def test_construction(self, store: InMemoryThreadStore) -> None:
        deps = PlatformRouteDeps(
            registrations={},
            thread_store=store,
            auth_level=func.AuthLevel.ANONYMOUS,
            max_stream_response_bytes=1024,
        )
        assert deps.registrations == {}
        assert deps.thread_store is store
        assert deps.auth_level == func.AuthLevel.ANONYMOUS
        assert deps.max_stream_response_bytes == 1024
        # New validation params — should have defaults
        assert deps.max_request_body_bytes == 1024 * 1024
        assert deps.max_input_depth == 32
        assert deps.max_input_nodes == 10_000


# ---------------------------------------------------------------------------
# Oracle review fixes — additional tests
# ---------------------------------------------------------------------------


class TestBusyThreadReject:
    """Threads marked 'busy' must return 409 on new run attempts."""

    def test_runs_wait_busy_thread_returns_409(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        store.update(thread.thread_id, status="busy", assistant_id="agent")

        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 409
        data = json.loads(resp.get_body())
        assert "busy" in data["detail"]

    def test_runs_stream_busy_thread_returns_409(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        store.update(thread.thread_id, status="busy", assistant_id="agent")

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 409
        data = json.loads(resp.get_body())
        assert "busy" in data["detail"]

    def test_idle_thread_can_run(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        # Thread is idle by default

        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200


class TestAssistantsSearchInvalidJson:
    """Invalid JSON in assistants_search should return 400."""

    def test_invalid_json_returns_400(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_search")

        req = func.HttpRequest(
            method="POST",
            url="/api/assistants/search",
            body=b"not valid json",
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "Invalid JSON" in data["detail"]


class TestContentLocationHeader:
    """Runs endpoints must include Content-Location header."""

    def test_runs_wait_has_content_location(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_wait")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200
        headers = dict(resp.headers)
        assert "Content-Location" in headers
        assert f"/api/threads/{thread.thread_id}/runs/" in headers["Content-Location"]

    def test_runs_stream_has_content_location(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200
        headers = dict(resp.headers)
        assert "Content-Location" in headers
        assert f"/api/threads/{thread.thread_id}/runs/" in headers["Content-Location"]


class TestAdditionalPreflightFields:
    """command and feedback_keys should return 501."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("command", {"resume": "value"}),
            ("feedback_keys", ["key1", "key2"]),
        ],
    )
    def test_unsupported_new_fields_return_501(
        self, store: InMemoryThreadStore, field: str, value: Any
    ) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_wait")
        body = {"assistant_id": "agent", "input": {}, field: value}
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            body,
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 501


class TestStableAssistantTimestamps:
    """Assistant timestamps should be stable across calls."""

    def test_timestamps_are_stable(
        self, graph: FakeCompiledGraph, store: InMemoryThreadStore,
    ) -> None:
        app = _build_platform_app(graphs={"agent": graph}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_assistants_get")

        req1 = _get_request("/api/assistants/agent", assistant_id="agent")
        resp1 = fn(req1)
        data1 = json.loads(resp1.get_body())

        req2 = _get_request("/api/assistants/agent", assistant_id="agent")
        resp2 = fn(req2)
        data2 = json.loads(resp2.get_body())

        assert data1["created_at"] == data2["created_at"]
        assert data1["updated_at"] == data2["updated_at"]


# ---------------------------------------------------------------------------
# Threadless runs (issue #53)
# ---------------------------------------------------------------------------


class _FakeCopyableCheckpointerGraph:
    """Graph with checkpointer + copy() — simulates disabling checkpointer."""

    def __init__(self) -> None:
        self.checkpointer = "memory"
        self._invoke_result: dict[str, Any] = {
            "messages": [{"role": "assistant", "content": "threadless!"}]
        }
        self._stream_results: list[dict[str, Any]] = [
            {"messages": [{"role": "assistant", "content": "chunk1"}]},
            {"messages": [{"role": "assistant", "content": "chunk2"}]},
        ]

    def copy(self, *, update: dict[str, Any] | None = None) -> "_FakeCopyableCheckpointerGraph":
        clone = _FakeCopyableCheckpointerGraph()
        if update and "checkpointer" in update:
            clone.checkpointer = update["checkpointer"]
        return clone

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._invoke_result

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield from self._stream_results


class _FakeNonCopyableCheckpointerGraph:
    """Graph with checkpointer but NO copy() — threadless should return 501."""

    checkpointer = "memory"

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"result": "ok"}

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield {"data": "chunk"}


class _FakeCopyRaisingCheckpointerGraph:
    """Graph with checkpointer + copy() that raises — threadless should return 501."""

    checkpointer = "memory"

    def copy(self, *, update: dict[str, Any] | None = None) -> "_FakeCopyRaisingCheckpointerGraph":
        raise RuntimeError("copy failed")

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"result": "ok"}

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield {"data": "chunk"}

class TestRunsWaitThreadless:
    """POST /runs/wait — threadless execution without a thread."""

    def test_basic_invocation(self, store: InMemoryThreadStore) -> None:
        """Threadless wait returns output dict with 200."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request("/api/runs/wait", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert isinstance(data, dict)
        assert "messages" in data

    def test_content_location_header(self, store: InMemoryThreadStore) -> None:
        """Response includes Content-Location: /api/runs/{run_id}."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request("/api/runs/wait", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 200
        headers = dict(resp.headers)
        assert "Content-Location" in headers
        assert headers["Content-Location"].startswith("/api/runs/")
        # Threadless: no thread_id in URL
        assert "/threads/" not in headers["Content-Location"]

    def test_unknown_graph_returns_404(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request("/api/runs/wait", {"assistant_id": "nonexistent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 404
        data = json.loads(resp.get_body())
        assert "nonexistent" in data["detail"]

    def test_invalid_json_returns_400(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = func.HttpRequest(
            method="POST",
            url="/api/runs/wait",
            body=b"not json",
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "Invalid JSON" in data["detail"]

    def test_non_dict_json_body_returns_400(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = func.HttpRequest(
            method="POST",
            url="/api/runs/wait",
            body=b'[1, 2, 3]',
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "JSON object" in data["detail"]

    def test_graph_error_returns_500(self, store: InMemoryThreadStore) -> None:
        class FailingGraph:
            checkpointer = None

            def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
                raise RuntimeError("boom")

            def stream(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
                stream_mode: str = "values",
            ) -> Iterator[Any]:
                yield {}

        app = _build_platform_app(graphs={"agent": FailingGraph()}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request("/api/runs/wait", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 500
        data = json.loads(resp.get_body())
        assert "execution failed" in data["detail"]

    def test_checkpointer_disabled_via_copy(self, store: InMemoryThreadStore) -> None:
        """Graph with checkpointer uses copy(update={checkpointer: None})."""
        g = _FakeCopyableCheckpointerGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request("/api/runs/wait", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert isinstance(data, dict)

    def test_non_copyable_checkpointer_returns_501(self, store: InMemoryThreadStore) -> None:
        """Graph with checkpointer but no copy() returns 501."""
        g = _FakeNonCopyableCheckpointerGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request("/api/runs/wait", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 501
        data = json.loads(resp.get_body())
        assert "cannot be disabled" in data["detail"]

    def test_validation_error_missing_assistant_id(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request("/api/runs/wait", {"input": {}})
        resp = fn(req)
        assert resp.status_code == 422

    def test_unsupported_interrupt_before_returns_501(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request(
            "/api/runs/wait",
            {"assistant_id": "agent", "input": {}, "interrupt_before": ["*"]},
        )
        resp = fn(req)
        assert resp.status_code == 501

    def test_thread_store_unchanged(self, store: InMemoryThreadStore) -> None:
        """Threadless runs must not create or modify any threads."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        # Snapshot thread store before
        before = store.search()
        assert len(before) == 0

        req = _post_request("/api/runs/wait", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 200

        # Store unchanged
        after = store.search()
        assert len(after) == 0

    def test_with_user_config(self, store: InMemoryThreadStore) -> None:
        """User-supplied config.configurable passes through to graph."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request(
            "/api/runs/wait",
            {
                "assistant_id": "agent",
                "input": {},
                "config": {"configurable": {"my_key": "my_value"}},
            },
        )
        resp = fn(req)
        assert resp.status_code == 200

    def test_copy_raising_returns_501(self, store: InMemoryThreadStore) -> None:
        """Graph whose copy() raises returns 501."""
        g = _FakeCopyRaisingCheckpointerGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request("/api/runs/wait", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 501
        data = json.loads(resp.get_body())
        assert "cannot be disabled" in data["detail"]

    def test_clone_has_checkpointer_none(self, store: InMemoryThreadStore) -> None:
        """Verify _get_threadless_graph actually sets checkpointer=None."""
        from azure_functions_langgraph.platform.routes import _get_threadless_graph

        g = _FakeCopyableCheckpointerGraph()
        assert g.checkpointer == "memory"
        clone = _get_threadless_graph(g)
        assert clone is not None
        assert clone.checkpointer is None

    def test_thread_id_in_config_rejected(self, store: InMemoryThreadStore) -> None:
        """Threadless wait rejects thread_id smuggled in config.configurable."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_wait_threadless")

        req = _post_request(
            "/api/runs/wait",
            {
                "assistant_id": "agent",
                "input": {},
                "config": {"configurable": {"thread_id": "smuggled-id"}},
            },
        )
        resp = fn(req)
        assert resp.status_code == 422
        data = json.loads(resp.get_body())
        assert "thread_id" in data["detail"]


class TestRunsStreamThreadless:
    """POST /runs/stream — threadless SSE streaming without a thread."""

    def test_basic_sse_stream(self, store: InMemoryThreadStore) -> None:
        """Threadless stream returns SSE with metadata → data → end."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request("/api/runs/stream", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        body = resp.get_body().decode()
        assert "event: metadata" in body
        assert "event: values" in body
        assert "event: end" in body

    def test_content_location_header(self, store: InMemoryThreadStore) -> None:
        """Response includes Content-Location: /api/runs/{run_id}."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request("/api/runs/stream", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 200
        headers = dict(resp.headers)
        assert "Content-Location" in headers
        assert headers["Content-Location"].startswith("/api/runs/")
        assert "/threads/" not in headers["Content-Location"]

    def test_unknown_graph_returns_404(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request("/api/runs/stream", {"assistant_id": "nonexistent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 404

    def test_invalid_json_returns_400(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = func.HttpRequest(
            method="POST",
            url="/api/runs/stream",
            body=b"not json",
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400

    def test_non_dict_json_body_returns_400(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = func.HttpRequest(
            method="POST",
            url="/api/runs/stream",
            body=b'[1, 2, 3]',
            headers={"Content-Type": "application/json"},
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "JSON object" in data["detail"]

    def test_not_streamable_returns_501(self, store: InMemoryThreadStore) -> None:
        """Graph without stream() returns 501."""
        g = FakeInvokeOnlyGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request("/api/runs/stream", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 501
        data = json.loads(resp.get_body())
        assert "does not support streaming" in data["detail"]

    def test_graph_error_produces_sse_error_event(self, store: InMemoryThreadStore) -> None:
        class FailingStreamGraph:
            checkpointer = None

            def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
                return {}

            def stream(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
                stream_mode: str = "values",
            ) -> Iterator[Any]:
                raise RuntimeError("stream boom")

        app = _build_platform_app(graphs={"agent": FailingStreamGraph()}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request("/api/runs/stream", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 200  # SSE always 200
        body = resp.get_body().decode()
        assert "event: error" in body
        assert "event: end" in body

    def test_checkpointer_disabled_via_copy(self, store: InMemoryThreadStore) -> None:
        g = _FakeCopyableCheckpointerGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request("/api/runs/stream", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 200
        body = resp.get_body().decode()
        assert "event: metadata" in body
        assert "event: end" in body

    def test_non_copyable_checkpointer_returns_501(self, store: InMemoryThreadStore) -> None:
        g = _FakeNonCopyableCheckpointerGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request("/api/runs/stream", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 501
        data = json.loads(resp.get_body())
        assert "cannot be disabled" in data["detail"]

    def test_thread_store_unchanged(self, store: InMemoryThreadStore) -> None:
        """Threadless streaming must not create or modify any threads."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        before = store.search()
        assert len(before) == 0

        req = _post_request("/api/runs/stream", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 200

        after = store.search()
        assert len(after) == 0

    def test_multi_stream_mode_returns_501(self, store: InMemoryThreadStore) -> None:
        """Multiple stream modes not supported."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request(
            "/api/runs/stream",
            {"assistant_id": "agent", "input": {}, "stream_mode": ["values", "updates"]},
        )
        resp = fn(req)
        assert resp.status_code == 501

    def test_unsupported_interrupt_before_returns_501(self, store: InMemoryThreadStore) -> None:
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request(
            "/api/runs/stream",
            {"assistant_id": "agent", "input": {}, "interrupt_before": ["*"]},
        )
        resp = fn(req)
        assert resp.status_code == 501

    def test_copy_raising_returns_501(self, store: InMemoryThreadStore) -> None:
        """Graph whose copy() raises returns 501."""
        g = _FakeCopyRaisingCheckpointerGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request("/api/runs/stream", {"assistant_id": "agent", "input": {}})
        resp = fn(req)
        assert resp.status_code == 501
        data = json.loads(resp.get_body())
        assert "cannot be disabled" in data["detail"]

    def test_clone_has_checkpointer_none(self, store: InMemoryThreadStore) -> None:
        """Verify _get_threadless_graph actually sets checkpointer=None."""
        from azure_functions_langgraph.platform.routes import _get_threadless_graph

        g = _FakeCopyableCheckpointerGraph()
        assert g.checkpointer == "memory"
        clone = _get_threadless_graph(g)
        assert clone is not None
        assert clone.checkpointer is None

    def test_thread_id_in_config_rejected(self, store: InMemoryThreadStore) -> None:
        """Threadless stream rejects thread_id smuggled in config.configurable."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        fn = _get_fn(fa, "aflg_platform_runs_stream_threadless")

        req = _post_request(
            "/api/runs/stream",
            {
                "assistant_id": "agent",
                "input": {},
                "config": {"configurable": {"thread_id": "smuggled-id"}},
            },
        )
        resp = fn(req)
        assert resp.status_code == 422
        data = json.loads(resp.get_body())
        assert "thread_id" in data["detail"]


# ---------------------------------------------------------------------------
# SSE wire-format tests (issue #39)
# ---------------------------------------------------------------------------


def _decode_sse_frame(frame: str) -> dict[str, Any]:
    """Parse a single SSE frame into {"event": str, "data": Any}."""
    event_name: str | None = None
    data_str: str | None = None
    for line in frame.strip().split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: "):]
        elif line.startswith("data: "):
            data_str = line[len("data: "):]
    assert event_name is not None, f"Missing event in frame: {frame!r}"
    parsed_data: Any = None
    if data_str is not None and data_str != "":
        parsed_data = json.loads(data_str)
    return {"event": event_name, "data": parsed_data}


def _decode_sse_body(body: str) -> list[dict[str, Any]]:
    """Split a full SSE response body into decoded frames."""
    frames = [f for f in body.split("\n\n") if f.strip()]
    return [_decode_sse_frame(f) for f in frames]


class TestStreamSSEWireFormat:
    """Exact byte-level verification of SSE output for SDK compatibility."""

    def test_frame_order_metadata_data_end(self, store: InMemoryThreadStore) -> None:
        """Successful stream must emit: metadata, data chunks, end."""
        g = FakeCompiledGraph(
            stream_results=[
                {"messages": [{"role": "assistant", "content": "a"}]},
                {"messages": [{"role": "assistant", "content": "b"}]},
            ]
        )
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        body = resp.get_body().decode()
        parts = _decode_sse_body(body)

        assert len(parts) == 4  # metadata + 2 data + end
        assert parts[0]["event"] == "metadata"
        assert "run_id" in parts[0]["data"]
        assert parts[1]["event"] == "values"
        assert parts[1]["data"]["messages"][0]["content"] == "a"
        assert parts[2]["event"] == "values"
        assert parts[2]["data"]["messages"][0]["content"] == "b"
        assert parts[3]["event"] == "end"
        assert parts[3]["data"] is None

    def test_end_event_data_is_null(self, store: InMemoryThreadStore) -> None:
        """End event must have ``data: null`` (not ``data: {}``)."""
        g = FakeCompiledGraph(stream_results=[{"ok": True}])
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        body = resp.get_body().decode()

        # Raw text must contain exact "data: null" (not "data: {}")
        assert "event: end\ndata: null\n" in body

    def test_error_then_end_sequence(self, store: InMemoryThreadStore) -> None:
        """On failure: metadata → error → end."""
        class BoomGraph:
            def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
                return {}

            def stream(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
                stream_mode: str = "values",
            ) -> Iterator[Any]:
                raise RuntimeError("kaboom")

        app = _build_platform_app(graphs={"agent": BoomGraph()}, store=store)
        fa = app.function_app
        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        body = resp.get_body().decode()
        parts = _decode_sse_body(body)

        events = [p["event"] for p in parts]
        assert events == ["metadata", "error", "end"]
        assert parts[1]["data"] == {"error": "stream processing failed"}
        assert parts[2]["data"] is None

    def test_content_type_is_event_stream(self, store: InMemoryThreadStore) -> None:
        """Response Content-Type must contain 'text/event-stream'."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert "text/event-stream" in resp.mimetype

    def test_stream_mode_from_string(self, store: InMemoryThreadStore) -> None:
        """stream_mode='updates' → event: updates."""
        g = FakeCompiledGraph(stream_results=[{"node": {"key": "val"}}])
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}, "stream_mode": "updates"},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        parts = _decode_sse_body(resp.get_body().decode())
        # Data event should use "updates" as event type
        assert parts[1]["event"] == "updates"

    def test_stream_mode_single_item_list(self, store: InMemoryThreadStore) -> None:
        """stream_mode=['values'] should unwrap to 'values'."""
        g = FakeCompiledGraph(stream_results=[{"ok": True}])
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}, "stream_mode": ["values"]},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200
        parts = _decode_sse_body(resp.get_body().decode())
        assert parts[1]["event"] == "values"

    def test_stream_mode_multi_item_list_returns_501(self, store: InMemoryThreadStore) -> None:
        """stream_mode=['values', 'updates'] → 501 (unsupported)."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}, "stream_mode": ["values", "updates"]},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 501
        data = json.loads(resp.get_body())
        assert "Multi-stream-mode" in data["detail"]

    def test_stream_mode_empty_list_defaults_to_values(self, store: InMemoryThreadStore) -> None:
        """stream_mode=[] → defaults to 'values'."""
        g = FakeCompiledGraph(stream_results=[{"x": 1}])
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}, "stream_mode": []},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200
        parts = _decode_sse_body(resp.get_body().decode())
        assert parts[1]["event"] == "values"

    def test_non_dict_event_wrapped(self, store: InMemoryThreadStore) -> None:
        """Non-dict stream events should be wrapped as {"data": payload}."""

        class StringStreamGraph:
            def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
                return {}

            def stream(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
                stream_mode: str = "values",
            ) -> Iterator[Any]:
                yield "hello"
                yield 42
                yield [1, 2, 3]

        app = _build_platform_app(graphs={"agent": StringStreamGraph()}, store=store)
        fa = app.function_app
        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        parts = _decode_sse_body(resp.get_body().decode())

        # Skip metadata (idx 0) and end (last)
        data_parts = parts[1:-1]
        assert len(data_parts) == 3
        assert data_parts[0]["data"] == {"data": "hello"}
        assert data_parts[1]["data"] == {"data": 42}
        assert data_parts[2]["data"] == {"data": [1, 2, 3]}

    def test_multi_mode_501_resets_thread_to_idle(self, store: InMemoryThreadStore) -> None:
        """After 501 from multi-mode, thread should be idle (not stuck busy)."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        thread = store.create()

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}, "stream_mode": ["values", "updates"]},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 501

        updated = store.get(thread.thread_id)
        assert updated is not None
        assert updated.status == "idle"


class TestMaxBytesMetadataOverflow:
    """Edge case: max_bytes smaller than the metadata frame."""

    def test_max_bytes_less_than_metadata_returns_error_end(
        self, store: InMemoryThreadStore
    ) -> None:
        """When metadata alone exceeds limit, return error+end immediately."""
        g = FakeCompiledGraph()
        app = LangGraphApp(platform_compat=True, max_stream_response_bytes=10)
        app._thread_store = store
        app.register(graph=g, name="agent")
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        body = resp.get_body().decode()

        assert "event: metadata" in body
        assert "event: error" in body
        assert "max buffered size" in body
        assert "event: end\ndata: null" in body

        updated = store.get(thread.thread_id)
        assert updated is not None
        assert updated.status == "error"


class TestMultiMode501AssistantId:
    """Multi-mode 501 must not bind assistant_id to thread."""

    def test_multi_mode_501_does_not_bind_assistant_id(
        self, store: InMemoryThreadStore
    ) -> None:
        """Thread assistant_id should remain None after 501 rejection."""
        g = FakeCompiledGraph()
        app = _build_platform_app(graphs={"agent": g}, store=store)
        fa = app.function_app
        thread = store.create()

        # Confirm assistant_id is initially None
        assert thread.assistant_id is None

        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}, "stream_mode": ["values", "updates"]},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 501

        # assistant_id must NOT have been mutated
        updated = store.get(thread.thread_id)
        assert updated is not None
        assert updated.assistant_id is None


class TestNaNPayloadInStream:
    """Graph that yields NaN should produce error event, not crash."""

    def test_nan_payload_produces_error_event(
        self, store: InMemoryThreadStore
    ) -> None:
        """NaN in graph output should be caught and returned as SSE error."""

        class NaNGraph:
            def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
                return {}

            def stream(
                self,
                input: dict[str, Any],
                config: dict[str, Any] | None = None,
                stream_mode: str = "values",
            ) -> Iterator[dict[str, Any]]:
                yield {"value": float("nan")}

        app = LangGraphApp(platform_compat=True)
        app._thread_store = store
        app.register(graph=NaNGraph(), name="agent")
        fa = app.function_app

        thread = store.create()
        fn = _get_fn(fa, "aflg_platform_runs_stream")
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        body = resp.get_body().decode()

        # Should contain error event (NaN rejected by allow_nan=False)
        assert "event: error" in body
        # Stream must still end properly
        assert "event: end\ndata: null" in body

        updated = store.get(thread.thread_id)
        assert updated is not None
        assert updated.status == "error"
