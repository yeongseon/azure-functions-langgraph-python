"""Tests for LangGraphApp core functionality."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp, _has_checkpointer
from tests.conftest import (
    FakeCompiledGraph,
    FakeFailingGraph,
    FakeFailingStatefulGraph,
    FakeInvokeOnlyGraph,
    FakeStatefulGraph,
)

# ------------------------------------------------------------------
# Registration tests
# ------------------------------------------------------------------


class TestRegistration:
    def test_warns_for_anonymous_auth_level(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with caplog.at_level("WARNING"):
            monkeypatch.setenv("AZURE_FUNCTIONS_ENVIRONMENT", "Production")
            LangGraphApp()

        assert "anonymous HTTP auth" in caplog.text

    def test_no_warning_for_function_auth_level(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with caplog.at_level("WARNING"):
            monkeypatch.setenv("AZURE_FUNCTIONS_ENVIRONMENT", "Production")
            LangGraphApp(auth_level=func.AuthLevel.FUNCTION)

        assert "anonymous HTTP auth" not in caplog.text

    def test_no_warning_without_azure_env(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with caplog.at_level("WARNING"):
            monkeypatch.delenv("AZURE_FUNCTIONS_ENVIRONMENT", raising=False)
            LangGraphApp()

        assert "anonymous HTTP auth" not in caplog.text

    def test_register_single_graph(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        assert "agent" in app._registrations

    def test_register_multiple_graphs(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent1")
        app.register(graph=FakeCompiledGraph(), name="agent2")
        assert len(app._registrations) == 2

    def test_register_duplicate_raises(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        with pytest.raises(ValueError, match="already registered"):
            app.register(graph=fake_graph, name="agent")

    def test_register_with_description(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent", description="My agent")
        assert app._registrations["agent"].description == "My agent"

    def test_register_invoke_only_mode(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent", stream=False)
        assert app._registrations["agent"].stream_enabled is False

    def test_register_invalid_graph_raises(self) -> None:
        app = LangGraphApp()
        with pytest.raises(TypeError, match="invoke"):
            app.register(graph="not a graph", name="bad")

    def test_register_invoke_only_graph(self, fake_invoke_only_graph: FakeInvokeOnlyGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_invoke_only_graph, name="invoke_only")
        assert "invoke_only" in app._registrations

    def test_register_with_auth_level_override(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(graph=fake_graph, name="agent", auth_level=func.AuthLevel.ADMIN)
        assert app._registrations["agent"].auth_level == func.AuthLevel.ADMIN

    def test_register_auth_level_defaults_to_none(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        assert app._registrations["agent"].auth_level is None

    def test_register_anonymous_auth_override(self, fake_graph: FakeCompiledGraph) -> None:
        """AuthLevel.ANONYMOUS is falsy-ish; ensure 'is not None' check preserves it."""
        app = LangGraphApp(auth_level=func.AuthLevel.ADMIN)
        app.register(graph=fake_graph, name="agent", auth_level=func.AuthLevel.ANONYMOUS)
        assert app._registrations["agent"].auth_level == func.AuthLevel.ANONYMOUS


# ------------------------------------------------------------------
# Per-graph auth level tests
# ------------------------------------------------------------------


class TestPerGraphAuth:
    """Verify per-graph auth_level overrides propagate to Azure Functions routes."""

    @staticmethod
    def _get_trigger_auth(fa: func.FunctionApp, fn_name: str) -> func.AuthLevel:
        """Extract the auth_level from a registered function's HTTP trigger."""
        # Reset bindings cache to avoid duplicate-name validation on repeat calls
        fa.functions_bindings = {}
        for f in fa.get_functions():
            if f.get_function_name() == fn_name:
                return f.get_trigger().auth_level  # type: ignore[union-attr, no-any-return]
        raise ValueError(f"Function {fn_name!r} not found")

    def test_per_graph_auth_overrides_invoke_route(self) -> None:
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(graph=FakeCompiledGraph(), name="secure", auth_level=func.AuthLevel.ADMIN)
        fa = app.function_app
        assert self._get_trigger_auth(fa, "aflg_secure_invoke") == func.AuthLevel.ADMIN

    def test_per_graph_auth_overrides_stream_route(self) -> None:
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(graph=FakeCompiledGraph(), name="secure", auth_level=func.AuthLevel.ADMIN)
        fa = app.function_app
        assert self._get_trigger_auth(fa, "aflg_secure_stream") == func.AuthLevel.ADMIN

    def test_fallback_to_app_auth_when_none(self) -> None:
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(graph=FakeCompiledGraph(), name="default")
        fa = app.function_app
        assert self._get_trigger_auth(fa, "aflg_default_invoke") == func.AuthLevel.FUNCTION
        assert self._get_trigger_auth(fa, "aflg_default_stream") == func.AuthLevel.FUNCTION

    def test_anonymous_override_is_preserved(self) -> None:
        """AuthLevel.ANONYMOUS must not be swallowed by falsy check."""
        app = LangGraphApp(auth_level=func.AuthLevel.ADMIN)
        app.register(
            graph=FakeCompiledGraph(), name="public", auth_level=func.AuthLevel.ANONYMOUS
        )
        fa = app.function_app
        assert self._get_trigger_auth(fa, "aflg_public_invoke") == func.AuthLevel.ANONYMOUS
        assert self._get_trigger_auth(fa, "aflg_public_stream") == func.AuthLevel.ANONYMOUS

    def test_mixed_graphs_use_correct_auth(self) -> None:
        """Two graphs: one with override, one with default — each gets correct auth."""
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(graph=FakeCompiledGraph(), name="private", auth_level=func.AuthLevel.ADMIN)
        app.register(graph=FakeCompiledGraph(), name="default")
        fa = app.function_app
        # Private graph → ADMIN
        assert self._get_trigger_auth(fa, "aflg_private_invoke") == func.AuthLevel.ADMIN
        assert self._get_trigger_auth(fa, "aflg_private_stream") == func.AuthLevel.ADMIN
        # Default graph → FUNCTION (app-level)
        assert self._get_trigger_auth(fa, "aflg_default_invoke") == func.AuthLevel.FUNCTION
        assert self._get_trigger_auth(fa, "aflg_default_stream") == func.AuthLevel.FUNCTION

    def test_health_always_uses_app_auth(self) -> None:
        """Health/OpenAPI endpoints must use app-level auth, not per-graph."""
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(graph=FakeCompiledGraph(), name="agent", auth_level=func.AuthLevel.ADMIN)
        fa = app.function_app
        assert self._get_trigger_auth(fa, "aflg_health") == func.AuthLevel.FUNCTION
        assert self._get_trigger_auth(fa, "aflg_openapi") == func.AuthLevel.FUNCTION
# ------------------------------------------------------------------
# Function app creation tests
# ------------------------------------------------------------------


class TestFunctionApp:
    def test_function_app_is_created(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        fa = app.function_app
        assert isinstance(fa, func.FunctionApp)

    def test_function_app_is_cached(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        fa1 = app.function_app
        fa2 = app.function_app
        assert fa1 is fa2

    def test_cache_invalidated_on_register(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent1")
        fa1 = app.function_app
        app.register(graph=FakeCompiledGraph(), name="agent2")
        fa2 = app.function_app
        assert fa1 is not fa2

    def test_multi_graph_routes_are_independent(self) -> None:
        """Regression: each graph's handlers must close over own registration."""
        graph_a = FakeCompiledGraph()
        graph_b = FakeCompiledGraph()
        la = LangGraphApp()
        la.register(graph=graph_a, name="alpha")
        la.register(graph=graph_b, name="beta")
        req_alpha = func.HttpRequest(
            method="POST",
            url="/api/graphs/alpha/invoke",
            body=json.dumps({"input": {"messages": []}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        req_beta = func.HttpRequest(
            method="POST",
            url="/api/graphs/beta/invoke",
            body=json.dumps({"input": {"messages": []}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp_a = la._handle_invoke(req_alpha, la._registrations["alpha"])
        resp_b = la._handle_invoke(req_beta, la._registrations["beta"])
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        # Verify they used different registrations (closure correctness)
        assert la._registrations["alpha"].graph is graph_a
        assert la._registrations["beta"].graph is graph_b

    def test_openapi_paths_match_registered_routes_without_api_prefix(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")

        openapi = app._build_openapi()
        paths = openapi["paths"]

        assert "/graphs/agent/invoke" in paths
        assert "/graphs/agent/stream" in paths
        assert "/health" in paths
        assert "/api/graphs/agent/invoke" not in paths
        assert "/api/graphs/agent/stream" not in paths


# ------------------------------------------------------------------
# Invoke handler tests
# ------------------------------------------------------------------


class TestInvokeHandler:
    def _make_request(self, body: dict[str, Any]) -> func.HttpRequest:
        return func.HttpRequest(
            method="POST",
            url="/api/graphs/agent/invoke",
            body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )

    def test_invoke_success(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        req = self._make_request({"input": {"messages": [{"role": "human", "content": "hi"}]}})
        resp = app._handle_invoke(req, app._registrations["agent"])
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert "output" in data

    def test_invoke_with_config(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        req = self._make_request(
            {
                "input": {"messages": []},
                "config": {"configurable": {"thread_id": "t1"}},
            }
        )
        resp = app._handle_invoke(req, app._registrations["agent"])
        assert resp.status_code == 200

    def test_invoke_invalid_json(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        req = func.HttpRequest(
            method="POST",
            url="/api/graphs/agent/invoke",
            body=b"not json",
            headers={"Content-Type": "application/json"},
        )
        resp = app._handle_invoke(req, app._registrations["agent"])
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert data["error"] == "error"

    def test_invoke_validation_error(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        # Missing required 'input' field
        req = self._make_request({"not_input": "bad"})
        resp = app._handle_invoke(req, app._registrations["agent"])
        assert resp.status_code == 422

    def test_invoke_graph_failure(self) -> None:
        class ExplodingGraph:
            checkpointer = None

            def invoke(self, input: dict[str, Any], config: Any = None) -> dict[str, Any]:
                raise RuntimeError("database password leaked")

        app = LangGraphApp()
        app.register(graph=ExplodingGraph(), name="agent")
        req = self._make_request({"input": {"messages": []}})
        resp = app._handle_invoke(req, app._registrations["agent"])
        assert resp.status_code == 500
        data = json.loads(resp.get_body())
        assert data["detail"] == "Graph execution failed"
        assert "database password leaked" not in data["detail"]

    def test_invoke_non_dict_result(self) -> None:
        """Graph returning non-dict result is wrapped in {'result': ...}."""

        class StringGraph:
            checkpointer = None

            def invoke(self, input: dict[str, Any], config: Any = None) -> str:
                return "hello"

            def stream(
                self,
                input: dict[str, Any],
                config: Any = None,
                stream_mode: str = "values",
            ) -> Any:
                yield {"data": "chunk"}

        app = LangGraphApp()
        app.register(graph=StringGraph(), name="agent")
        req = self._make_request({"input": {"messages": []}})
        resp = app._handle_invoke(req, app._registrations["agent"])
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["output"]["result"] == "hello"


# ------------------------------------------------------------------
# Stream handler tests
# ------------------------------------------------------------------


class TestStreamHandler:
    def _make_request(self, body: dict[str, Any]) -> func.HttpRequest:
        return func.HttpRequest(
            method="POST",
            url="/api/graphs/agent/stream",
            body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )

    def test_stream_success(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        req = self._make_request({"input": {"messages": []}})
        resp = app._handle_stream(req, app._registrations["agent"])
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        body = resp.get_body().decode()
        assert "event: data" in body
        assert "event: end" in body

    def test_stream_with_custom_mode(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        req = self._make_request({"input": {"messages": []}, "stream_mode": "updates"})
        resp = app._handle_stream(req, app._registrations["agent"])
        assert resp.status_code == 200

    def test_stream_invalid_json(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        req = func.HttpRequest(
            method="POST",
            url="/api/graphs/agent/stream",
            body=b"bad",
            headers={"Content-Type": "application/json"},
        )
        resp = app._handle_stream(req, app._registrations["agent"])
        assert resp.status_code == 400

    def test_stream_graph_failure(self, fake_failing_graph: FakeFailingGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_failing_graph, name="agent")
        req = self._make_request({"input": {"messages": []}})
        resp = app._handle_stream(req, app._registrations["agent"])
        assert resp.status_code == 200  # SSE always 200, error is in the stream
        body = resp.get_body().decode()
        assert "event: error" in body
        assert '"error": "stream processing failed"' in body
        assert "Stream execution failed" not in body

    def test_stream_contains_chunks(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        req = self._make_request({"input": {"messages": []}})
        resp = app._handle_stream(req, app._registrations["agent"])
        body = resp.get_body().decode()
        # Should have 2 data events + 1 end event
        assert body.count("event: data") == 2
        assert body.count("event: end") == 1

    def test_stream_enforces_bounded_buffer(self) -> None:
        large_events = [
            {"messages": [{"role": "assistant", "content": "x" * 400}]},
            {"messages": [{"role": "assistant", "content": "y" * 400}]},
            {"messages": [{"role": "assistant", "content": "z" * 400}]},
        ]
        app = LangGraphApp(max_stream_response_bytes=700)
        app.register(graph=FakeCompiledGraph(stream_results=large_events), name="agent")
        req = self._make_request({"input": {"messages": []}})

        resp = app._handle_stream(req, app._registrations["agent"])

        assert resp.status_code == 200
        body = resp.get_body().decode()
        assert "event: data" in body
        assert "event: error" in body
        assert "exceeded max buffered size" in body
        assert body.count("event: data") == 1

    def test_stream_invoke_only_graph_returns_501(
        self, fake_invoke_only_graph: FakeInvokeOnlyGraph
    ) -> None:
        app = LangGraphApp()
        app.register(graph=fake_invoke_only_graph, name="agent")
        req = self._make_request({"input": {"messages": []}})
        resp = app._handle_stream(req, app._registrations["agent"])
        assert resp.status_code == 501

    def test_stream_returns_501_when_registered_in_invoke_only_mode(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent", stream=False)
        req = self._make_request({"input": {"messages": []}})

        resp = app._handle_stream(req, app._registrations["agent"])

        assert resp.status_code == 501
        payload = json.loads(resp.get_body())
        assert "invoke-only" in payload["detail"]


# ------------------------------------------------------------------
# State handler tests
# ------------------------------------------------------------------


class TestStateHandler:
    @staticmethod
    def _make_state_request(thread_id: str = "t1") -> func.HttpRequest:
        return func.HttpRequest(
            method="GET",
            url=f"/api/graphs/agent/threads/{thread_id}/state",
            body=b"",
            route_params={"thread_id": thread_id},
        )

    def test_state_success(
        self, fake_stateful_graph: FakeStatefulGraph
    ) -> None:
        app = LangGraphApp()
        app.register(graph=fake_stateful_graph, name="agent")
        req = self._make_state_request("t1")
        resp = app._handle_state(req, app._registrations["agent"])
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert "values" in data
        assert isinstance(data["next"], list)

    def test_state_returns_values_and_next(
        self, fake_stateful_graph: FakeStatefulGraph
    ) -> None:
        from tests.conftest import _FakeStateSnapshot

        snapshot = _FakeStateSnapshot(
            values={"count": 42},
            next_nodes=("agent", "tool"),
            metadata={"step": 3},
        )
        graph = FakeStatefulGraph(state_snapshot=snapshot)
        app = LangGraphApp()
        app.register(graph=graph, name="agent")
        req = self._make_state_request("t1")
        resp = app._handle_state(req, app._registrations["agent"])
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["values"] == {"count": 42}
        assert data["next"] == ["agent", "tool"]
        assert data["metadata"] == {"step": 3}

    def test_state_non_stateful_graph_returns_409(
        self, fake_graph: FakeCompiledGraph
    ) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        req = self._make_state_request("t1")
        resp = app._handle_state(req, app._registrations["agent"])
        assert resp.status_code == 409
        data = json.loads(resp.get_body())
        assert "does not support state" in data["detail"]

    def test_state_missing_thread_id(
        self, fake_stateful_graph: FakeStatefulGraph
    ) -> None:
        app = LangGraphApp()
        app.register(graph=fake_stateful_graph, name="agent")
        req = func.HttpRequest(
            method="GET",
            url="/api/graphs/agent/threads//state",
            body=b"",
            route_params={},
        )
        resp = app._handle_state(req, app._registrations["agent"])
        assert resp.status_code == 400
        data = json.loads(resp.get_body())
        assert "Missing thread_id" in data["detail"]

    def test_state_get_state_failure_returns_404(
        self, fake_failing_stateful_graph: FakeFailingStatefulGraph
    ) -> None:
        app = LangGraphApp()
        app.register(graph=fake_failing_stateful_graph, name="agent")
        req = self._make_state_request("bad_thread")
        resp = app._handle_state(req, app._registrations["agent"])
        assert resp.status_code == 404
        data = json.loads(resp.get_body())
        assert "not found" in data["detail"]
        # Must NOT leak internal error message
        assert "Checkpointer unavailable" not in data["detail"]

    def test_state_route_only_registered_for_stateful_graph(self) -> None:
        """State route should only exist for graphs satisfying StatefulGraph."""
        app = LangGraphApp()
        app.register(graph=FakeStatefulGraph(), name="stateful")
        app.register(graph=FakeCompiledGraph(), name="basic")
        fa = app.function_app
        fa.functions_bindings = {}
        fn_names = [f.get_function_name() for f in fa.get_functions()]
        assert "aflg_stateful_state" in fn_names
        assert "aflg_basic_state" not in fn_names

    def test_state_uses_per_graph_auth(self) -> None:
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(
            graph=FakeStatefulGraph(),
            name="agent",
            auth_level=func.AuthLevel.ADMIN,
        )
        fa = app.function_app
        fa.functions_bindings = {}
        for f in fa.get_functions():
            if f.get_function_name() == "aflg_agent_state":
                assert f.get_trigger().auth_level == func.AuthLevel.ADMIN  # type: ignore[union-attr]
                break
        else:
            pytest.fail("State function not found")

    def test_state_openapi_includes_state_path_for_stateful_graph(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeStatefulGraph(), name="agent")
        spec = app._build_openapi()
        state_path = "/graphs/agent/threads/{thread_id}/state"
        assert state_path in spec["paths"]
        state_op = spec["paths"][state_path]["get"]
        assert "parameters" in state_op
        assert state_op["parameters"][0]["name"] == "thread_id"

    def test_state_openapi_excludes_state_path_for_non_stateful_graph(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        spec = app._build_openapi()
        state_paths = [p for p in spec["paths"] if "state" in p]
        assert len(state_paths) == 0


# ------------------------------------------------------------------
# Helper tests
# ------------------------------------------------------------------


class TestHelpers:
    def test_has_checkpointer_true(self) -> None:
        graph = MagicMock()
        graph.checkpointer = MagicMock()
        assert _has_checkpointer(graph) is True

    def test_has_checkpointer_false(self) -> None:
        graph = MagicMock(spec=[])
        assert _has_checkpointer(graph) is False

    def test_has_checkpointer_none(self) -> None:
        graph = MagicMock()
        graph.checkpointer = None
        assert _has_checkpointer(graph) is False


# ------------------------------------------------------------------
# OpenAPI tests
# ------------------------------------------------------------------


class TestOpenAPI:
    def test_openapi_endpoint_returns_valid_spec(self, fake_graph: FakeCompiledGraph) -> None:
        """OpenAPI endpoint returns a valid OpenAPI 3.0.3 spec."""
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        spec = app._build_openapi()

        assert spec["openapi"] == "3.0.3"
        assert spec["info"]["title"] == "azure-functions-langgraph"
        assert "paths" in spec

    def test_health_endpoint_has_responses(self, fake_graph: FakeCompiledGraph) -> None:
        """Health endpoint in OpenAPI spec must have responses field."""
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        spec = app._build_openapi()

        health_get = spec["paths"]["/health"]["get"]
        assert "responses" in health_get
        assert "200" in health_get["responses"]
        assert health_get["responses"]["200"]["description"] == "Service is healthy"

    def test_invoke_endpoint_has_responses_and_no_parameters(
        self, fake_graph: FakeCompiledGraph
    ) -> None:
        """Invoke endpoint must have responses and no path parameters."""
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        spec = app._build_openapi()

        invoke_post = spec["paths"]["/graphs/agent/invoke"]["post"]
        assert "responses" in invoke_post
        assert "200" in invoke_post["responses"]
        assert "parameters" not in invoke_post

    def test_stream_endpoint_has_responses_and_no_parameters(
        self, fake_graph: FakeCompiledGraph
    ) -> None:
        """Stream endpoint must have responses and no path parameters."""
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        spec = app._build_openapi()

        stream_post = spec["paths"]["/graphs/agent/stream"]["post"]
        assert "responses" in stream_post
        assert "200" in stream_post["responses"]
        assert "parameters" not in stream_post

    def test_invoke_only_registration_omits_stream_path(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="invoke_only", stream=False)

        spec = app._build_openapi()

        assert "/graphs/invoke_only/invoke" in spec["paths"]
        assert "/graphs/invoke_only/stream" not in spec["paths"]

    def test_multiple_graphs_have_separate_paths(self) -> None:
        """Each registered graph should have its own paths in OpenAPI spec."""
        graph_a = FakeCompiledGraph()
        graph_b = FakeCompiledGraph()
        app = LangGraphApp()
        app.register(graph=graph_a, name="alpha")
        app.register(graph=graph_b, name="beta")
        spec = app._build_openapi()

        # Verify both graphs have invoke and stream paths
        assert "/graphs/alpha/invoke" in spec["paths"]
        assert "/graphs/alpha/stream" in spec["paths"]
        assert "/graphs/beta/invoke" in spec["paths"]
        assert "/graphs/beta/stream" in spec["paths"]

    def test_all_operations_have_responses(self, fake_graph: FakeCompiledGraph) -> None:
        """All operations in OpenAPI spec must have responses field (OpenAPI 3.0 requirement)."""
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        spec = app._build_openapi()

        for path, path_item in spec["paths"].items():
            for method, operation in path_item.items():
                if method in ["get", "post", "put", "delete", "patch"]:
                    assert "responses" in operation, (
                        f"Path {path} method {method} missing responses"
                    )
                    assert isinstance(operation["responses"], dict)
                    assert len(operation["responses"]) > 0
