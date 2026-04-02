"""Tests for LangGraphApp core functionality."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp, _has_checkpointer
from tests.conftest import FakeCompiledGraph, FakeFailingGraph, FakeInvokeOnlyGraph

# ------------------------------------------------------------------
# Registration tests
# ------------------------------------------------------------------


class TestRegistration:
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

    def test_register_invalid_graph_raises(self) -> None:
        app = LangGraphApp()
        with pytest.raises(TypeError, match="invoke"):
            app.register(graph="not a graph", name="bad")

    def test_register_invoke_only_graph(self, fake_invoke_only_graph: FakeInvokeOnlyGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_invoke_only_graph, name="invoke_only")
        assert "invoke_only" in app._registrations


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

    def test_invoke_graph_failure(self, fake_failing_graph: FakeFailingGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_failing_graph, name="agent")
        req = self._make_request({"input": {"messages": []}})
        resp = app._handle_invoke(req, app._registrations["agent"])
        assert resp.status_code == 500
        data = json.loads(resp.get_body())
        assert "Graph execution failed" in data["detail"]

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

    def test_stream_contains_chunks(self, fake_graph: FakeCompiledGraph) -> None:
        app = LangGraphApp()
        app.register(graph=fake_graph, name="agent")
        req = self._make_request({"input": {"messages": []}})
        resp = app._handle_stream(req, app._registrations["agent"])
        body = resp.get_body().decode()
        # Should have 2 data events + 1 end event
        assert body.count("event: data") == 2
        assert body.count("event: end") == 1

    def test_stream_invoke_only_graph_returns_501(
        self, fake_invoke_only_graph: FakeInvokeOnlyGraph
    ) -> None:
        app = LangGraphApp()
        app.register(graph=fake_invoke_only_graph, name="agent")
        req = self._make_request({"input": {"messages": []}})
        resp = app._handle_stream(req, app._registrations["agent"])
        assert resp.status_code == 501


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
