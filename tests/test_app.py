"""Tests for LangGraphApp core functionality."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp, _has_checkpointer
from tests import conftest
from tests.conftest import (
    FakeCompiledGraph,
    FakeFailingGraph,
    FakeFailingStatefulGraph,
    FakeInvokeOnlyGraph,
    FakeNotFoundStatefulGraph,
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

        assert "ANONYMOUS auth" in caplog.text
        assert "LangGraphApp(auth_level=func.AuthLevel.FUNCTION)" in caplog.text
        assert "v1.0" in caplog.text

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

    def test_openapi_route_not_registered(self) -> None:
        """Verify deprecated openapi.json route is no longer registered."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")

        fa = app.function_app
        function_names = [fn.get_function_name() for fn in fa.get_functions()]

        assert "aflg_openapi" not in function_names


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

    def test_state_get_state_unexpected_error_returns_500(
        self, fake_failing_stateful_graph: FakeFailingStatefulGraph
    ) -> None:
        """Unexpected errors (not KeyError/ValueError) must return 500, not 404."""
        app = LangGraphApp()
        app.register(graph=fake_failing_stateful_graph, name="agent")
        req = self._make_state_request("bad_thread")
        resp = app._handle_state(req, app._registrations["agent"])
        assert resp.status_code == 500
        data = json.loads(resp.get_body())
        assert "Internal error" in data["detail"]
        # Must NOT leak internal error message
        assert "Checkpointer unavailable" not in data["detail"]

    def test_state_thread_not_found_returns_404(
        self, fake_not_found_stateful_graph: FakeNotFoundStatefulGraph
    ) -> None:
        """KeyError from get_state should return 404 (thread not found)."""
        app = LangGraphApp()
        app.register(graph=fake_not_found_stateful_graph, name="agent")
        req = self._make_state_request("missing-thread")
        resp = app._handle_state(req, app._registrations["agent"])
        assert resp.status_code == 404
        data = json.loads(resp.get_body())
        assert "not found" in data["detail"]

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
# HTTP Handler tests for uncovered lines
# ------------------------------------------------------------------


class TestHealthEndpointHTTPHandler:
    """Test health endpoint via actual HTTP request dispatch."""

    def test_health_endpoint_with_no_graphs(self) -> None:
        """Health endpoint should work even with no registered graphs."""
        app = LangGraphApp()
        fa = app.function_app
        
        # Get the health function and call it
        for fn in fa.get_functions():
            if fn.get_function_name() == "aflg_health":
                health_fn = fn.get_user_function()
                req = func.HttpRequest(
                    method="GET",
                    url="http://localhost:7071/api/health",
                    body=b"",
                )
                resp = health_fn(req)
                assert resp.status_code == 200
                body = json.loads(resp.get_body())
                assert body["status"] == "ok"
                assert body["graphs"] == []
                break
        else:
            raise AssertionError("health function not found")

    def test_health_endpoint_with_graphs_no_checkpointer(self) -> None:
        """Health endpoint lists graphs without checkpointer marker."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(checkpointer=None), name="agent1")
        app.register(graph=FakeCompiledGraph(checkpointer=None), name="agent2")
        fa = app.function_app
        
        for fn in fa.get_functions():
            if fn.get_function_name() == "aflg_health":
                health_fn = fn.get_user_function()
                req = func.HttpRequest(
                    method="GET",
                    url="http://localhost:7071/api/health",
                    body=b"",
                )
                resp = health_fn(req)
                body = json.loads(resp.get_body())
                assert len(body["graphs"]) == 2
                assert body["graphs"][0]["name"] == "agent1"
                assert body["graphs"][0]["has_checkpointer"] is False
                assert body["graphs"][1]["name"] == "agent2"
                assert body["graphs"][1]["has_checkpointer"] is False
                break

    def test_health_endpoint_with_graphs_with_checkpointer(self) -> None:
        """Health endpoint marks graphs with checkpointer."""
        app = LangGraphApp()
        app.register(
            graph=FakeCompiledGraph(checkpointer=MagicMock()), name="stateful_agent"
        )
        fa = app.function_app
        
        for fn in fa.get_functions():
            if fn.get_function_name() == "aflg_health":
                health_fn = fn.get_user_function()
                req = func.HttpRequest(
                    method="GET",
                    url="http://localhost:7071/api/health",
                    body=b"",
                )
                resp = health_fn(req)
                body = json.loads(resp.get_body())
                assert len(body["graphs"]) == 1
                assert body["graphs"][0]["name"] == "stateful_agent"
                assert body["graphs"][0]["has_checkpointer"] is True
                break


class TestStreamValidationError:
    """Test stream endpoint validation error path (lines 266-267)."""

    def test_stream_with_invalid_body_returns_422(self) -> None:
        """Stream endpoint should return 422 on invalid request model."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        
        # Create request with missing required 'input' field
        req = func.HttpRequest(
            method="POST",
            url="http://localhost:7071/api/graphs/agent/stream",
            body=json.dumps({"not_input": "bad"}).encode(),
        )
        resp = app._handle_stream(req, app._registrations["agent"])
        assert resp.status_code == 422
        body = json.loads(resp.get_body())
        assert body["error"] == "error"
        assert "Validation error" in body["detail"]


class TestInvokeWithEmptyInput:
    """Test invoke with empty input dict."""

    def test_invoke_with_empty_input_dict(self) -> None:
        """Invoke should handle empty input dict."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        
        req = func.HttpRequest(
            method="POST",
            url="http://localhost:7071/api/graphs/agent/invoke",
            body=json.dumps({"input": {}}).encode(),
        )
        resp = app._handle_invoke(req, app._registrations["agent"])
        assert resp.status_code == 200
        body = json.loads(resp.get_body())
        assert "output" in body


class TestStreamWithEmptyInput:
    """Test stream with empty input dict."""

    def test_stream_with_empty_input_dict(self) -> None:
        """Stream should handle empty input dict."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        
        req = func.HttpRequest(
            method="POST",
            url="http://localhost:7071/api/graphs/agent/stream",
            body=json.dumps({"input": {}}).encode(),
        )
        resp = app._handle_stream(req, app._registrations["agent"])
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"


class TestMultipleGraphsStatefulNonStateful:
    """Test registering multiple stateful and non-stateful graphs together."""

    def test_mixed_stateful_and_nonstateful_graphs(self) -> None:
        """Multiple graphs with mixed stateful/non-stateful should all be registered."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="simple")
        app.register(graph=FakeStatefulGraph(), name="stateful1")
        app.register(graph=FakeCompiledGraph(), name="simple2")
        app.register(graph=FakeStatefulGraph(), name="stateful2")
        
        fa = app.function_app
        function_names = {fn.get_function_name() for fn in fa.get_functions()}
        
        # Should have invoke/stream for all, plus state for stateful graphs
        assert "aflg_simple_invoke" in function_names
        assert "aflg_simple_stream" in function_names
        assert "aflg_stateful1_state" in function_names
        assert "aflg_stateful2_state" in function_names
        # non-stateful should NOT have state endpoint
        assert "aflg_simple_state" not in function_names


class TestStateResponseEdgeCases:
    """Test StateResponse model edge cases."""

    def test_state_response_with_empty_values(self) -> None:
        """StateResponse should handle empty values dict."""
        from azure_functions_langgraph.contracts import StateResponse
        
        resp = StateResponse(values={})
        assert resp.values == {}
        assert resp.next == []
        assert resp.metadata is None
        
    def test_state_response_model_dump_all_fields(self) -> None:
        """StateResponse model_dump should include all fields."""
        from azure_functions_langgraph.contracts import StateResponse
        
        resp = StateResponse(
            values={"a": 1}, next=["node1"], metadata={"key": "value"}
        )
        dumped = resp.model_dump()
        assert "values" in dumped
        assert "next" in dumped
        assert "metadata" in dumped
        assert dumped["values"] == {"a": 1}
        assert dumped["next"] == ["node1"]
        assert dumped["metadata"] == {"key": "value"}


class TestRegisterWithFunctionAuth:
    """Test register graph with auth_level=FUNCTION."""

    def test_register_with_function_auth_level(self) -> None:
        """Register should accept and preserve auth_level=FUNCTION."""
        app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
        app.register(
            graph=FakeCompiledGraph(), name="secure", auth_level=func.AuthLevel.FUNCTION
        )
        
        reg = app._registrations["secure"]
        assert reg.auth_level == func.AuthLevel.FUNCTION


class TestHasCheckpointerEdgeCases:
    """Test _has_checkpointer helper with edge cases."""

    def test_has_checkpointer_with_string_checkpointer(self) -> None:
        """_has_checkpointer should return True for non-None checkpointer (e.g. string)."""
        from azure_functions_langgraph.app import _has_checkpointer
        
        class GraphWithStringCheckpointer:
            checkpointer = "memory"
        
        graph = GraphWithStringCheckpointer()
        assert _has_checkpointer(graph) is True

    def test_has_checkpointer_with_none(self) -> None:
        """_has_checkpointer should return False for None."""
        from azure_functions_langgraph.app import _has_checkpointer
        
        class GraphWithoutCheckpointer:
            checkpointer = None
        
        graph = GraphWithoutCheckpointer()
        assert _has_checkpointer(graph) is False

    def test_has_checkpointer_with_missing_attr(self) -> None:
        """_has_checkpointer should return False when checkpointer attr is missing."""
        from azure_functions_langgraph.app import _has_checkpointer
        
        class GraphWithoutAttr:
            pass
        
        graph = GraphWithoutAttr()
        assert _has_checkpointer(graph) is False


class TestStateEndpointEmptyMetadata:
    """Test state endpoint with empty/missing metadata in snapshot."""

    def test_state_endpoint_with_empty_metadata(self) -> None:
        """State endpoint should handle snapshot with empty metadata."""
        app = LangGraphApp()
        snapshot_empty_metadata = conftest._FakeStateSnapshot(
            values={"messages": []}, metadata={}
        )
        graph = FakeStatefulGraph(state_snapshot=snapshot_empty_metadata)
        app.register(graph=graph, name="agent")
        
        req = func.HttpRequest(
            method="GET",
            url="http://localhost:7071/api/graphs/agent/threads/t1/state",
            body=b"",
            route_params={"thread_id": "t1"},
        )
        resp = app._handle_state(req, app._registrations["agent"])
        body = json.loads(resp.get_body())
        assert body["metadata"] is None  # Empty dict is falsy, returns None

    def test_state_endpoint_with_no_metadata_attr(self) -> None:
        """State endpoint handles snapshot with no metadata attr gracefully."""
        app = LangGraphApp()
        
        class FakeSnapshot:
            def __init__(self) -> None:
                self.values = {"data": "value"}
                self.next = ()
        
        graph = FakeStatefulGraph(state_snapshot=FakeSnapshot())  # type: ignore[arg-type]
        app.register(graph=graph, name="agent")
        
        req = func.HttpRequest(
            method="GET",
            url="http://localhost:7071/api/graphs/agent/threads/t1/state",
            body=b"",
            route_params={"thread_id": "t1"},
        )
        resp = app._handle_state(req, app._registrations["agent"])
        body = json.loads(resp.get_body())
        assert body["metadata"] is None


class TestVersionIsString:
    """Test that __version__ is a string."""

    def test_version_is_string(self) -> None:
        """__version__ should be a string."""
        from azure_functions_langgraph import __version__
        
        assert isinstance(__version__, str)
        assert len(__version__) > 0
