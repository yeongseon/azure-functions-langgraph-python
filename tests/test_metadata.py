"""Tests for the metadata API (get_app_metadata, dataclasses, deprecation)."""

from __future__ import annotations

from types import MappingProxyType

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.contracts import (
    AppMetadata,
    RegisteredGraphMetadata,
    RouteMetadata,
)
from tests.conftest import FakeCompiledGraph, FakeStatefulGraph

# ------------------------------------------------------------------
# RouteMetadata dataclass tests
# ------------------------------------------------------------------


class TestRouteMetadata:
    def test_frozen(self) -> None:
        route = RouteMetadata(path="/api/foo", method="POST")
        with pytest.raises(AttributeError):
            route.path = "/api/bar"  # type: ignore[misc]

    def test_defaults(self) -> None:
        route = RouteMetadata(path="/api/foo", method="GET")
        assert route.summary == ""
        assert route.description == ""
        assert route.parameters == ()
        assert route.request_model is None
        assert route.response_model is None

    def test_with_parameters(self) -> None:
        params = ({"name": "thread_id", "in": "path", "required": True},)
        route = RouteMetadata(
            path="/api/graphs/a/threads/{thread_id}/state",
            method="GET",
            parameters=params,
        )
        assert len(route.parameters) == 1
        assert route.parameters[0]["name"] == "thread_id"


# ------------------------------------------------------------------
# RegisteredGraphMetadata dataclass tests
# ------------------------------------------------------------------


class TestRegisteredGraphMetadata:
    def test_frozen(self) -> None:
        meta = RegisteredGraphMetadata(name="agent")
        with pytest.raises(AttributeError):
            meta.name = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        meta = RegisteredGraphMetadata(name="agent")
        assert meta.description is None
        assert meta.routes == ()


# ------------------------------------------------------------------
# AppMetadata dataclass tests
# ------------------------------------------------------------------


class TestAppMetadata:
    def test_frozen(self) -> None:
        meta = AppMetadata()
        with pytest.raises(AttributeError):
            meta.app_routes = ()  # type: ignore[misc]

    def test_defaults(self) -> None:
        meta = AppMetadata()
        assert meta.graphs == {}
        assert meta.app_routes == ()


# ------------------------------------------------------------------
# get_app_metadata tests
# ------------------------------------------------------------------


class TestGetAppMetadata:
    def test_empty_app(self) -> None:
        app = LangGraphApp()
        meta = app.get_app_metadata()
        assert isinstance(meta, AppMetadata)
        assert meta.graphs == {}
        assert len(meta.app_routes) == 1
        assert meta.app_routes[0].path == "/api/health"

    def test_single_graph(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        meta = app.get_app_metadata()

        assert "agent" in meta.graphs
        graph_meta = meta.graphs["agent"]
        assert graph_meta.name == "agent"
        assert len(graph_meta.routes) == 2  # invoke + stream
        paths = {r.path for r in graph_meta.routes}
        assert "/api/graphs/agent/invoke" in paths
        assert "/api/graphs/agent/stream" in paths

    def test_invoke_only_graph(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent", stream=False)
        meta = app.get_app_metadata()

        graph_meta = meta.graphs["agent"]
        assert len(graph_meta.routes) == 1  # invoke only
        assert graph_meta.routes[0].path == "/api/graphs/agent/invoke"

    def test_stateful_graph_includes_state_route(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeStatefulGraph(), name="stateful")
        meta = app.get_app_metadata()

        graph_meta = meta.graphs["stateful"]
        assert len(graph_meta.routes) == 3  # invoke + stream + state
        state_routes = [r for r in graph_meta.routes if "/threads/" in r.path]
        assert len(state_routes) == 1
        state_route = state_routes[0]
        assert state_route.method == "GET"
        assert len(state_route.parameters) == 1
        assert state_route.parameters[0]["name"] == "thread_id"

    def test_multiple_graphs(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="alpha")
        app.register(graph=FakeCompiledGraph(), name="beta")
        meta = app.get_app_metadata()

        assert len(meta.graphs) == 2
        assert "alpha" in meta.graphs
        assert "beta" in meta.graphs

    def test_description_propagated(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent", description="My agent")
        meta = app.get_app_metadata()

        assert meta.graphs["agent"].description == "My agent"

    def test_request_response_models_propagated(self) -> None:
        from pydantic import BaseModel

        class MyRequest(BaseModel):
            query: str

        class MyResponse(BaseModel):
            answer: str

        app = LangGraphApp()
        app.register(
            graph=FakeCompiledGraph(),
            name="agent",
            request_model=MyRequest,
            response_model=MyResponse,
        )
        meta = app.get_app_metadata()

        invoke_route = meta.graphs["agent"].routes[0]
        assert invoke_route.request_model is MyRequest
        assert invoke_route.response_model is MyResponse

    def test_stream_route_has_no_response_model(self) -> None:
        """Stream routes should not have response_model (SSE, not JSON)."""
        from pydantic import BaseModel

        class MyResponse(BaseModel):
            answer: str

        app = LangGraphApp()
        app.register(
            graph=FakeCompiledGraph(),
            name="agent",
            response_model=MyResponse,
        )
        meta = app.get_app_metadata()

        stream_routes = [r for r in meta.graphs["agent"].routes if "stream" in r.path]
        assert len(stream_routes) == 1
        assert stream_routes[0].response_model is None

    def test_app_routes_includes_health(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        meta = app.get_app_metadata()

        assert len(meta.app_routes) == 1
        assert meta.app_routes[0].path == "/api/health"
        assert meta.app_routes[0].method == "GET"
        assert meta.app_routes[0].summary == "Health check"

    def test_metadata_is_snapshot(self) -> None:
        """Metadata should be an immutable snapshot, not live."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent1")
        meta1 = app.get_app_metadata()

        app.register(graph=FakeCompiledGraph(), name="agent2")
        meta2 = app.get_app_metadata()

        assert len(meta1.graphs) == 1
        assert len(meta2.graphs) == 2

    def test_graphs_is_immutable_mapping(self) -> None:
        """AppMetadata.graphs should be a MappingProxyType, not a plain dict."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        meta = app.get_app_metadata()
        assert isinstance(meta.graphs, MappingProxyType)
        with pytest.raises(TypeError):
            meta.graphs["rogue"] = None  # type: ignore[index]

    def test_nested_parameters_are_immutable(self) -> None:
        """RouteMetadata.parameters dicts must be MappingProxyType (deep immutability)."""
        app = LangGraphApp()
        app.register(graph=FakeStatefulGraph(), name="stateful_graph")
        meta = app.get_app_metadata()
        state_routes = [r for r in meta.graphs["stateful_graph"].routes if "/threads/" in r.path]
        assert len(state_routes) == 1
        param = state_routes[0].parameters[0]
        assert isinstance(param, MappingProxyType)
        with pytest.raises(TypeError):
            param["rogue"] = "value"  # type: ignore[index]


# ------------------------------------------------------------------
# register() keyword-only params tests
# ------------------------------------------------------------------


class TestRegisterKeywordOnlyParams:
    def test_request_model_must_be_keyword_only(self) -> None:
        """request_model/response_model should be keyword-only."""
        app = LangGraphApp()
        # This should work (keyword)
        app.register(
            graph=FakeCompiledGraph(),
            name="agent",
            request_model=None,
            response_model=None,
        )
        assert "agent" in app._registrations

    def test_request_model_stored_in_registration(self) -> None:
        from pydantic import BaseModel

        class MyModel(BaseModel):
            x: int

        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent", request_model=MyModel)
        assert app._registrations["agent"].request_model is MyModel

    def test_response_model_stored_in_registration(self) -> None:
        from pydantic import BaseModel

        class MyModel(BaseModel):
            y: str

        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent", response_model=MyModel)
        assert app._registrations["agent"].response_model is MyModel

    def test_backward_compat_without_models(self) -> None:
        """Existing code without request_model/response_model must still work."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent", description="test")
        reg = app._registrations["agent"]
        assert reg.request_model is None
        assert reg.response_model is None

    def test_legacy_positional_register_call(self) -> None:
        """All pre-v0.5 positional args must still work without keyword-only params.

        Regression test: register(graph, name, description, stream, auth_level)
        was the full positional signature before ``*`` was added.
        """
        app = LangGraphApp()
        app.register(
            FakeCompiledGraph(),  # graph
            "agent",  # name
            "A description",  # description
            False,  # stream
            func.AuthLevel.FUNCTION,  # auth_level
        )
        reg = app._registrations["agent"]
        assert reg.name == "agent"
        assert reg.description == "A description"
        assert reg.stream_enabled is False
        assert reg.auth_level == func.AuthLevel.FUNCTION
        assert reg.request_model is None
        assert reg.response_model is None
