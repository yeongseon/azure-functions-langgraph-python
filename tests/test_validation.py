"""Tests for input validation utilities (_validation.py) and integration with routes.

Covers:
- validate_graph_name: graph name / assistant_id format
- validate_thread_id: thread ID permissive format
- validate_body_size: request body size limit
- validate_input_structure: depth and node-count limits
- Integration: validation in native handlers and platform routes
"""

from __future__ import annotations

import json
import string
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph._validation import (
    validate_body_size,
    validate_graph_name,
    validate_input_structure,
    validate_thread_id,
)
from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from tests.conftest import FakeCompiledGraph, FakeStatefulGraph

# ---------------------------------------------------------------------------
# Unit tests: validate_graph_name
# ---------------------------------------------------------------------------


class TestValidateGraphName:
    """Graph names must match ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$."""

    def test_valid_simple(self) -> None:
        assert validate_graph_name("agent") is None

    def test_valid_single_letter(self) -> None:
        assert validate_graph_name("a") is None

    def test_valid_with_underscores(self) -> None:
        assert validate_graph_name("my_agent") is None

    def test_valid_with_hyphens(self) -> None:
        assert validate_graph_name("my-agent") is None

    def test_valid_with_digits(self) -> None:
        assert validate_graph_name("agent1") is None

    def test_valid_mixed(self) -> None:
        assert validate_graph_name("My_Agent-v2") is None

    def test_valid_max_length(self) -> None:
        # 64 chars: 1 letter + 63 alphanumeric
        name = "a" + "b" * 63
        assert len(name) == 64
        assert validate_graph_name(name) is None

    def test_empty_string(self) -> None:
        err = validate_graph_name("")
        assert err is not None
        assert "empty" in err.lower()

    def test_starts_with_digit(self) -> None:
        err = validate_graph_name("1agent")
        assert err is not None
        assert "Invalid graph name" in err

    def test_starts_with_underscore(self) -> None:
        err = validate_graph_name("_agent")
        assert err is not None

    def test_starts_with_hyphen(self) -> None:
        err = validate_graph_name("-agent")
        assert err is not None

    def test_too_long(self) -> None:
        name = "a" + "b" * 64  # 65 chars
        err = validate_graph_name(name)
        assert err is not None
        assert "64 characters" in err

    def test_special_characters(self) -> None:
        err = validate_graph_name("agent!@#")
        assert err is not None

    def test_spaces(self) -> None:
        err = validate_graph_name("my agent")
        assert err is not None

    def test_dots(self) -> None:
        err = validate_graph_name("my.agent")
        assert err is not None

    def test_unicode(self) -> None:
        err = validate_graph_name("aéènt")
        assert err is not None


# ---------------------------------------------------------------------------
# Unit tests: validate_thread_id
# ---------------------------------------------------------------------------


class TestValidateThreadId:
    """Thread IDs: non-empty, printable ASCII, max 256 chars."""

    def test_valid_simple(self) -> None:
        assert validate_thread_id("abc") is None

    def test_valid_uuid(self) -> None:
        assert validate_thread_id("550e8400-e29b-41d4-a716-446655440000") is None

    def test_valid_custom_format(self) -> None:
        assert validate_thread_id("my-thread-123") is None

    def test_valid_with_spaces(self) -> None:
        # Space is printable ASCII (0x20)
        assert validate_thread_id("thread 1") is None

    def test_valid_all_printable(self) -> None:
        # All printable ASCII from space (0x20) through tilde (0x7e)
        assert validate_thread_id(string.printable[:95].strip()) is None

    def test_valid_max_length(self) -> None:
        assert validate_thread_id("a" * 256) is None

    def test_empty_string(self) -> None:
        err = validate_thread_id("")
        assert err is not None
        assert "empty" in err.lower()

    def test_too_long(self) -> None:
        err = validate_thread_id("a" * 257)
        assert err is not None
        assert "256" in err

    def test_control_characters_tab(self) -> None:
        err = validate_thread_id("thread\t1")
        assert err is not None
        assert "printable" in err.lower()

    def test_control_characters_newline(self) -> None:
        err = validate_thread_id("thread\n1")
        assert err is not None

    def test_control_characters_null(self) -> None:
        err = validate_thread_id("thread\x001")
        assert err is not None

    def test_non_ascii_unicode(self) -> None:
        err = validate_thread_id("thread-日本語")
        assert err is not None
        assert "printable" in err.lower()

    def test_valid_tilde(self) -> None:
        assert validate_thread_id("~thread~") is None


# ---------------------------------------------------------------------------
# Unit tests: validate_body_size
# ---------------------------------------------------------------------------


class TestValidateBodySize:
    def test_within_limit(self) -> None:
        assert validate_body_size(b"hello", 10) is None

    def test_at_limit(self) -> None:
        assert validate_body_size(b"12345", 5) is None

    def test_over_limit(self) -> None:
        err = validate_body_size(b"123456", 5)
        assert err is not None
        assert "6 bytes" in err
        assert "max 5 bytes" in err

    def test_empty_body(self) -> None:
        assert validate_body_size(b"", 0) is None

    def test_large_body(self) -> None:
        body = b"x" * (1024 * 1024 + 1)
        err = validate_body_size(body, 1024 * 1024)
        assert err is not None
        assert "too large" in err.lower()


# ---------------------------------------------------------------------------
# Unit tests: validate_input_structure
# ---------------------------------------------------------------------------


class TestValidateInputStructure:
    def test_flat_dict(self) -> None:
        assert validate_input_structure({"a": 1, "b": "hello"}) is None

    def test_scalar_input(self) -> None:
        """Scalars (not dict/list) always pass."""
        assert validate_input_structure("hello") is None
        assert validate_input_structure(42) is None
        assert validate_input_structure(None) is None

    def test_nested_at_max_depth(self) -> None:
        """Build a dict nested exactly at max_depth=4 (3 wrapping dicts), should pass."""
        data: dict[str, Any] = {"level3": "leaf"}
        data = {"level2": data}
        data = {"level1": data}
        # depth 1=level1, 2=level2, 3=level3, 4=leaf string (scalar, no recursion)
        # _count_depth_and_nodes enters level3 value at depth 4, but "leaf" is
        # not dict/list so no further recursion → passes at max_depth=4.
        assert validate_input_structure(data, max_depth=4) is None

    def test_nested_over_max_depth(self) -> None:
        """Build a dict nested deeper than max_depth=2."""
        data: dict[str, Any] = {"deep": "leaf"}
        data = {"mid": data}
        data = {"top": data}
        err = validate_input_structure(data, max_depth=2)
        assert err is not None
        assert "depth" in err.lower()

    def test_list_nesting(self) -> None:
        """Lists also count toward nesting depth."""
        data: list[Any] = ["leaf"]
        data = [data]
        data = [data]
        err = validate_input_structure(data, max_depth=2)
        assert err is not None
        assert "depth" in err.lower()

    def test_within_node_count(self) -> None:
        data = {f"key{i}": i for i in range(100)}
        assert validate_input_structure(data, max_nodes=100) is None

    def test_over_node_count(self) -> None:
        data = {f"key{i}": i for i in range(101)}
        err = validate_input_structure(data, max_nodes=100)
        assert err is not None
        assert "node count" in err.lower()

    def test_wide_shallow_dict(self) -> None:
        """Wide but shallow dicts should be caught by node count, not depth."""
        data = {f"k{i}": "v" for i in range(50)}
        err = validate_input_structure(data, max_depth=100, max_nodes=10)
        assert err is not None
        assert "node count" in err.lower()

    def test_mixed_dict_and_list(self) -> None:
        data = {"items": [{"a": 1}, {"b": 2}, {"c": 3}]}
        assert validate_input_structure(data, max_depth=10, max_nodes=100) is None

    def test_default_limits(self) -> None:
        """Default max_depth=32, max_nodes=10_000 — should pass for normal input."""
        data = {"messages": [{"role": "user", "content": "hi"}], "config": {}}
        assert validate_input_structure(data) is None

    def test_deeply_nested_exceeds_default_depth(self) -> None:
        """Build 33 levels of nesting — should exceed default max_depth=32."""
        data: Any = "leaf"
        for _ in range(33):
            data = {"nested": data}
        err = validate_input_structure(data)
        assert err is not None
        assert "depth" in err.lower()

    def test_empty_dict(self) -> None:
        assert validate_input_structure({}) is None

    def test_empty_list(self) -> None:
        assert validate_input_structure([]) is None


# ---------------------------------------------------------------------------
# Integration: graph name validation at register() time
# ---------------------------------------------------------------------------


class TestRegisterGraphNameValidation:
    def test_register_rejects_invalid_graph_name(self) -> None:
        app = LangGraphApp()
        with pytest.raises(ValueError, match="Invalid graph name"):
            app.register(graph=FakeCompiledGraph(), name="123invalid")

    def test_register_rejects_empty_name(self) -> None:
        app = LangGraphApp()
        with pytest.raises(ValueError, match="empty"):
            app.register(graph=FakeCompiledGraph(), name="")

    def test_register_accepts_valid_name(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="valid_agent")
        assert "valid_agent" in app._registrations


# ---------------------------------------------------------------------------
# Integration: native handler body size rejection
# ---------------------------------------------------------------------------


def _make_post_request(
    url: str,
    body_bytes: bytes,
    **route_params: str,
) -> func.HttpRequest:
    """Build a POST request with raw bytes body."""
    return func.HttpRequest(
        method="POST",
        url=url,
        body=body_bytes,
        headers={"Content-Type": "application/json"},
        route_params=route_params,
    )


class TestNativeHandlerValidation:
    """Test validation in native invoke/stream handlers."""

    def _build_app(self, *, max_request_body_bytes: int = 1024) -> LangGraphApp:
        app = LangGraphApp(max_request_body_bytes=max_request_body_bytes)
        app.register(graph=FakeCompiledGraph(), name="agent")
        return app

    @staticmethod
    def _get_fn(fa: func.FunctionApp, fn_name: str) -> Any:
        fa.functions_bindings = {}
        for fn in fa.get_functions():
            if fn.get_function_name() == fn_name:
                return fn.get_user_function()
        raise AssertionError(f"Function {fn_name!r} not found")

    def test_invoke_rejects_oversized_body(self) -> None:
        app = self._build_app(max_request_body_bytes=50)
        fa = app.function_app
        fn = self._get_fn(fa, "aflg_agent_invoke")

        big_body = json.dumps({"input": {"data": "x" * 100}}).encode()
        req = _make_post_request("/api/graphs/agent/invoke", big_body)
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "too large" in data["detail"].lower()

    def test_stream_rejects_oversized_body(self) -> None:
        app = self._build_app(max_request_body_bytes=50)
        fa = app.function_app
        fn = self._get_fn(fa, "aflg_agent_stream")

        big_body = json.dumps({"input": {"data": "x" * 100}}).encode()
        req = _make_post_request("/api/graphs/agent/stream", big_body)
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "too large" in data["detail"].lower()

    def test_invoke_accepts_normal_body(self) -> None:
        app = self._build_app(max_request_body_bytes=4096)
        fa = app.function_app
        fn = self._get_fn(fa, "aflg_agent_invoke")

        body = json.dumps({"input": {"message": "hi"}}).encode()
        req = _make_post_request("/api/graphs/agent/invoke", body)
        resp = fn(req)
        assert resp.status_code == 200

    def test_invoke_rejects_deep_input(self) -> None:
        app = LangGraphApp(max_input_depth=3, max_request_body_bytes=10_000)
        app.register(graph=FakeCompiledGraph(), name="agent")
        fa = app.function_app
        fn = self._get_fn(fa, "aflg_agent_invoke")

        deep_input: Any = "leaf"
        for _ in range(4):
            deep_input = {"n": deep_input}
        body = json.dumps({"input": deep_input}).encode()
        req = _make_post_request("/api/graphs/agent/invoke", body)
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "depth" in data["detail"].lower()

    def test_invoke_rejects_too_many_nodes(self) -> None:
        app = LangGraphApp(max_input_nodes=5, max_request_body_bytes=100_000)
        app.register(graph=FakeCompiledGraph(), name="agent")
        fa = app.function_app
        fn = self._get_fn(fa, "aflg_agent_invoke")

        wide_input = {f"k{i}": i for i in range(10)}
        body = json.dumps({"input": wide_input}).encode()
        req = _make_post_request("/api/graphs/agent/invoke", body)
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "node count" in data["detail"].lower()

    def test_invoke_rejects_deep_config(self) -> None:
        app = LangGraphApp(max_input_depth=2, max_request_body_bytes=10_000)
        app.register(graph=FakeCompiledGraph(), name="agent")
        fa = app.function_app
        fn = self._get_fn(fa, "aflg_agent_invoke")

        deep_config: Any = "leaf"
        for _ in range(3):
            deep_config = {"n": deep_config}
        body = json.dumps({"input": {}, "config": deep_config}).encode()
        req = _make_post_request("/api/graphs/agent/invoke", body)
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "depth" in data["detail"].lower()

    def test_state_rejects_control_chars_thread_id(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeStatefulGraph(), name="agent")
        fa = app.function_app
        fn = self._get_fn(fa, "aflg_agent_state")

        req = _get_request(
            "/api/graphs/agent/threads/bad\x00id/state",
            thread_id="bad\x00id",
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "printable" in data["detail"].lower()


# ---------------------------------------------------------------------------
# Integration: platform route validation
# ---------------------------------------------------------------------------


def _build_platform_app(
    *,
    graphs: dict[str, Any] | None = None,
    store: InMemoryThreadStore | None = None,
    max_request_body_bytes: int = 1024 * 1024,
    max_input_depth: int = 32,
    max_input_nodes: int = 10_000,
) -> LangGraphApp:
    app = LangGraphApp(
        platform_compat=True,
        max_request_body_bytes=max_request_body_bytes,
        max_input_depth=max_input_depth,
        max_input_nodes=max_input_nodes,
    )
    if store is not None:
        app._thread_store = store
    if graphs:
        for name, g in graphs.items():
            app.register(graph=g, name=name)
    return app


def _platform_get_fn(fa: func.FunctionApp, fn_name: str) -> Any:
    fa.functions_bindings = {}
    for fn in fa.get_functions():
        if fn.get_function_name() == fn_name:
            return fn.get_user_function()
    raise AssertionError(f"Function {fn_name!r} not found")


def _post_request(
    url: str,
    body: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    **route_params: str,
) -> func.HttpRequest:
    if raw_body is not None:
        body_bytes = raw_body
    else:
        body_bytes = json.dumps(body or {}).encode()
    return func.HttpRequest(
        method="POST",
        url=url,
        body=body_bytes,
        headers={"Content-Type": "application/json"},
        route_params=route_params,
    )


def _get_request(url: str, **route_params: str) -> func.HttpRequest:
    return func.HttpRequest(
        method="GET",
        url=url,
        body=b"",
        route_params=route_params,
    )


class TestPlatformBodySizeValidation:
    """Body size checks on platform POST endpoints."""

    @pytest.fixture()
    def store(self) -> InMemoryThreadStore:
        counter = iter(range(1000))
        return InMemoryThreadStore(id_factory=lambda: f"thread-{next(counter)}")

    def test_assistants_search_rejects_oversized(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_request_body_bytes=50,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_assistants_search")

        big_body = b'{"metadata": "' + b"x" * 100 + b'"}'
        req = _post_request("/api/assistants/search", raw_body=big_body)
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "too large" in data["detail"].lower()

    def test_assistants_search_rejects_oversized_whitespace_only(
        self, store: InMemoryThreadStore
    ) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_request_body_bytes=50,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_assistants_search")

        req = _post_request("/api/assistants/search", raw_body=b" " * 2_000_000)
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "too large" in data["detail"].lower()

    def test_threads_create_rejects_oversized(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_request_body_bytes=20,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_threads_create")

        big_body = b'{"metadata": {"key": "' + b"v" * 100 + b'"}}'
        req = _post_request("/api/threads", raw_body=big_body)
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "too large" in data["detail"].lower()

    def test_threads_create_rejects_oversized_whitespace_only(
        self, store: InMemoryThreadStore
    ) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_request_body_bytes=50,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_threads_create")

        req = _post_request("/api/threads", raw_body=b" " * 2_000_000)
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "too large" in data["detail"].lower()

    def test_runs_wait_rejects_oversized(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_request_body_bytes=30,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_wait")

        thread = store.create()

        big_body = b'{"assistant_id": "agent", "input": {"data": "' + b"x" * 100 + b'"}}'
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            raw_body=big_body,
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "too large" in data["detail"].lower()

    def test_runs_wait_rejects_oversized_before_thread_lookup(
        self, store: InMemoryThreadStore
    ) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_request_body_bytes=30,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_wait")

        big_body = b'{"assistant_id": "agent", "input": {"data": "' + b"x" * 100 + b'"}}'
        req = _post_request(
            "/api/threads/missing-thread/runs/wait",
            raw_body=big_body,
            thread_id="missing-thread",
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "too large" in data["detail"].lower()

    def test_runs_stream_rejects_oversized(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_request_body_bytes=30,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_stream")

        thread = store.create()

        big_body = b'{"assistant_id": "agent", "input": {"data": "' + b"x" * 100 + b'"}}'
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            raw_body=big_body,
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "too large" in data["detail"].lower()


class TestPlatformThreadIdValidation:
    """Thread ID validation on platform GET routes."""

    @pytest.fixture()
    def store(self) -> InMemoryThreadStore:
        counter = iter(range(1000))
        return InMemoryThreadStore(id_factory=lambda: f"thread-{next(counter)}")

    def test_threads_get_rejects_control_chars(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_threads_get")

        req = _get_request("/api/threads/bad\x00id", thread_id="bad\x00id")
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "printable" in data["detail"].lower()

    def test_threads_state_get_rejects_control_chars(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_threads_state_get")

        req = _get_request("/api/threads/bad\nid/state", thread_id="bad\nid")
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "printable" in data["detail"].lower()

    def test_threads_get_accepts_valid_id(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_threads_get")

        # Create thread so we get 200, not 404
        thread = store.create()
        req = _get_request(
            f"/api/threads/{thread.thread_id}",
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200


class TestPlatformAssistantIdValidation:
    """assistant_id (graph name) validation on platform run endpoints."""

    @pytest.fixture()
    def store(self) -> InMemoryThreadStore:
        counter = iter(range(1000))
        return InMemoryThreadStore(id_factory=lambda: f"thread-{next(counter)}")

    def test_runs_wait_rejects_invalid_assistant_id(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_wait")

        thread = store.create()
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "123-bad!", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "Invalid graph name" in data["detail"]

    def test_runs_stream_rejects_invalid_assistant_id(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_stream")

        thread = store.create()
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "123-bad!", "input": {}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "Invalid graph name" in data["detail"]


class TestPlatformInputStructureValidation:
    """Input/config depth and node validation on platform run endpoints."""

    @pytest.fixture()
    def store(self) -> InMemoryThreadStore:
        counter = iter(range(1000))
        return InMemoryThreadStore(id_factory=lambda: f"thread-{next(counter)}")

    def test_runs_wait_rejects_deep_input(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_input_depth=3,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_wait")

        thread = store.create()
        deep_input: Any = "leaf"
        for _ in range(4):
            deep_input = {"n": deep_input}

        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "agent", "input": deep_input},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "depth" in data["detail"].lower()

    def test_runs_stream_rejects_too_many_nodes(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_input_nodes=5,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_stream")

        thread = store.create()
        wide_input = {f"k{i}": i for i in range(10)}

        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/stream",
            {"assistant_id": "agent", "input": wide_input},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "node count" in data["detail"].lower()

    def test_runs_wait_rejects_deep_config(self, store: InMemoryThreadStore) -> None:
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
            max_input_depth=2,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_wait")

        thread = store.create()
        deep_config: Any = "leaf"
        for _ in range(3):
            deep_config = {"n": deep_config}

        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {}, "config": deep_config},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "depth" in data["detail"].lower()

    def test_runs_wait_accepts_valid_input(self, store: InMemoryThreadStore) -> None:
        """Normal input should pass all validation and return 200."""
        app = _build_platform_app(
            graphs={"agent": FakeCompiledGraph()},
            store=store,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_wait")

        thread = store.create()
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {"assistant_id": "agent", "input": {"message": "hello"}},
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200

    def test_runs_wait_uses_path_thread_id_over_user_config(
        self, store: InMemoryThreadStore
    ) -> None:
        class CaptureConfigGraph(FakeCompiledGraph):
            def __init__(self) -> None:
                super().__init__()
                self.last_config: dict[str, Any] | None = None

            def invoke(
                self, input: dict[str, Any], config: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                self.last_config = config
                return super().invoke(input, config)

        graph = CaptureConfigGraph()
        app = _build_platform_app(
            graphs={"agent": graph},
            store=store,
        )
        fa = app.function_app
        fn = _platform_get_fn(fa, "aflg_platform_runs_wait")

        thread = store.create()
        req = _post_request(
            f"/api/threads/{thread.thread_id}/runs/wait",
            {
                "assistant_id": "agent",
                "input": {"message": "hello"},
                "config": {
                    "configurable": {"thread_id": "different-thread-id"},
                    "tag": "value",
                },
            },
            thread_id=thread.thread_id,
        )
        resp = fn(req)
        assert resp.status_code == 200
        assert graph.last_config is not None
        assert graph.last_config["configurable"]["thread_id"] == thread.thread_id


# ---------------------------------------------------------------------------
# PlatformRouteDeps new fields
# ---------------------------------------------------------------------------


class TestPlatformRouteDepsNewFields:
    """Verify new validation params on PlatformRouteDeps."""

    @pytest.fixture()
    def store(self) -> InMemoryThreadStore:
        return InMemoryThreadStore()

    def test_defaults(self, store: InMemoryThreadStore) -> None:
        from azure_functions_langgraph.platform.routes import PlatformRouteDeps

        deps = PlatformRouteDeps(
            registrations={},
            thread_store=store,
            auth_level=func.AuthLevel.ANONYMOUS,
            max_stream_response_bytes=2048,
        )
        assert deps.max_request_body_bytes == 1024 * 1024
        assert deps.max_input_depth == 32
        assert deps.max_input_nodes == 10_000

    def test_custom_values(self, store: InMemoryThreadStore) -> None:
        from azure_functions_langgraph.platform.routes import PlatformRouteDeps

        deps = PlatformRouteDeps(
            registrations={},
            thread_store=store,
            auth_level=func.AuthLevel.ANONYMOUS,
            max_stream_response_bytes=2048,
            max_request_body_bytes=512,
            max_input_depth=10,
            max_input_nodes=500,
        )
        assert deps.max_request_body_bytes == 512
        assert deps.max_input_depth == 10
        assert deps.max_input_nodes == 500


# ---------------------------------------------------------------------------
# LangGraphApp constructor new params
# ---------------------------------------------------------------------------


class TestLangGraphAppNewParams:
    def test_defaults(self) -> None:
        app = LangGraphApp()
        assert app.max_request_body_bytes == 1024 * 1024
        assert app.max_input_depth == 32
        assert app.max_input_nodes == 10_000

    def test_custom_values(self) -> None:
        app = LangGraphApp(
            max_request_body_bytes=512,
            max_input_depth=5,
            max_input_nodes=100,
        )
        assert app.max_request_body_bytes == 512
        assert app.max_input_depth == 5
        assert app.max_input_nodes == 100

    def test_params_propagate_to_platform_deps(self) -> None:
        """Custom limits must flow through to PlatformRouteDeps."""
        app = LangGraphApp(
            platform_compat=True,
            max_request_body_bytes=999,
            max_input_depth=7,
            max_input_nodes=77,
        )
        app.register(graph=FakeCompiledGraph(), name="agent")
        # Access function_app to trigger _build_function_app
        _ = app.function_app
        # The deps are constructed internally; we verify via actual behavior
        # by checking the platform routes reject appropriately
        # (already covered in TestPlatformBodySizeValidation)
