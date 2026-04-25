"""Tests for the openapi bridge module (azure_functions_langgraph.openapi)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from pydantic import BaseModel
import pytest

from azure_functions_langgraph.app import LangGraphApp
from tests.conftest import FakeCompiledGraph, FakeStatefulGraph

# ------------------------------------------------------------------
# Import guard tests
# ------------------------------------------------------------------


class TestImportGuard:
    def test_import_error_when_openapi_not_installed(self) -> None:
        """register_with_openapi raises ImportError if azure-functions-openapi is missing."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")

        with patch.dict("sys.modules", {"azure_functions_openapi": None}):
            from azure_functions_langgraph.openapi import register_with_openapi

            with pytest.raises(ImportError, match="azure-functions-openapi-python"):
                register_with_openapi(app)


# ------------------------------------------------------------------
# Bridge registration tests
# ------------------------------------------------------------------


class TestRegisterWithOpenapi:
    @patch("azure_functions_langgraph.openapi.register_with_openapi.__module__")
    def _get_bridge_fn(self, mock_mod: Any) -> Any:
        """Helper to import bridge function."""
        from azure_functions_langgraph.openapi import register_with_openapi

        return register_with_openapi

    def test_registers_invoke_and_stream_routes(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")

        mock_register = MagicMock()
        with patch(
            "azure_functions_langgraph.openapi.register_with_openapi",
            wraps=None,
        ):
            # Directly import and mock the dependency
            with patch.dict("sys.modules", {}):
                from azure_functions_langgraph.openapi import register_with_openapi

                with patch(
                    "azure_functions_langgraph.openapi.register_openapi_metadata",
                    mock_register,
                    create=True,
                ):
                    # We need to patch the import inside the function
                    pass

        # Better approach: mock at import site
        mock_register = MagicMock()
        mock_openapi_module = MagicMock()
        mock_openapi_module.register_openapi_metadata = mock_register

        with patch.dict("sys.modules", {"azure_functions_openapi": mock_openapi_module}):
            from azure_functions_langgraph.openapi import register_with_openapi

            count = register_with_openapi(app)

        # 2 graph routes (invoke + stream) + 1 app route (health) = 3
        assert count == 3
        assert mock_register.call_count == 3

        # Verify invoke route
        invoke_call = [
            c for c in mock_register.call_args_list
            if c.kwargs.get("path") == "/api/graphs/agent/invoke"
            or (c.args and c.args[0] == "/api/graphs/agent/invoke")
        ]
        assert len(invoke_call) == 1

        # Verify stream route
        stream_call = [
            c for c in mock_register.call_args_list
            if c.kwargs.get("path") == "/api/graphs/agent/stream"
            or (c.args and c.args[0] == "/api/graphs/agent/stream")
        ]
        assert len(stream_call) == 1

        # Verify health route
        health_call = [
            c for c in mock_register.call_args_list
            if c.kwargs.get("path") == "/api/health"
            or (c.args and c.args[0] == "/api/health")
        ]
        assert len(health_call) == 1

    def test_registers_state_route_for_stateful_graph(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeStatefulGraph(), name="stateful")

        mock_register = MagicMock()
        mock_openapi_module = MagicMock()
        mock_openapi_module.register_openapi_metadata = mock_register

        with patch.dict("sys.modules", {"azure_functions_openapi": mock_openapi_module}):
            from azure_functions_langgraph.openapi import register_with_openapi

            count = register_with_openapi(app)

        # 3 graph routes (invoke + stream + state) + 1 app route (health) = 4
        assert count == 4

        # Verify state route includes parameters
        state_calls = [
            c for c in mock_register.call_args_list
            if "/threads/" in str(c.kwargs.get("path", ""))
        ]
        assert len(state_calls) == 1

    def test_registers_with_request_model(self) -> None:
        class ChatRequest(BaseModel):
            query: str
            temperature: float = 0.7

        app = LangGraphApp()
        app.register(
            graph=FakeCompiledGraph(),
            name="agent",
            request_model=ChatRequest,
        )

        mock_register = MagicMock()
        mock_openapi_module = MagicMock()
        mock_openapi_module.register_openapi_metadata = mock_register

        with patch.dict("sys.modules", {"azure_functions_openapi": mock_openapi_module}):
            from azure_functions_langgraph.openapi import register_with_openapi

            register_with_openapi(app)

        # Find invoke call and check request_body is set
        invoke_calls = [
            c for c in mock_register.call_args_list
            if c.kwargs.get("path") == "/api/graphs/agent/invoke"
        ]
        assert len(invoke_calls) == 1
        invoke_kwargs = invoke_calls[0].kwargs
        assert invoke_kwargs["request_body"] is not None
        assert invoke_kwargs["request_body"]["required"] is True
        schema = invoke_kwargs["request_body"]["content"]["application/json"]["schema"]
        assert "properties" in schema
        assert "query" in schema["properties"]

    def test_registers_with_response_model(self) -> None:
        class ChatResponse(BaseModel):
            answer: str

        app = LangGraphApp()
        app.register(
            graph=FakeCompiledGraph(),
            name="agent",
            response_model=ChatResponse,
        )

        mock_register = MagicMock()
        mock_openapi_module = MagicMock()
        mock_openapi_module.register_openapi_metadata = mock_register

        with patch.dict("sys.modules", {"azure_functions_openapi": mock_openapi_module}):
            from azure_functions_langgraph.openapi import register_with_openapi

            register_with_openapi(app)

        # Find invoke call and check response_model is set
        invoke_calls = [
            c for c in mock_register.call_args_list
            if c.kwargs.get("path") == "/api/graphs/agent/invoke"
        ]
        assert len(invoke_calls) == 1
        assert invoke_calls[0].kwargs["response_model"] is ChatResponse

    def test_invoke_only_omits_stream(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent", stream=False)

        mock_register = MagicMock()
        mock_openapi_module = MagicMock()
        mock_openapi_module.register_openapi_metadata = mock_register

        with patch.dict("sys.modules", {"azure_functions_openapi": mock_openapi_module}):
            from azure_functions_langgraph.openapi import register_with_openapi

            count = register_with_openapi(app)

        # 1 graph route (invoke only) + 1 app route (health) = 2
        assert count == 2

    def test_multiple_graphs(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="alpha")
        app.register(graph=FakeCompiledGraph(), name="beta")

        mock_register = MagicMock()
        mock_openapi_module = MagicMock()
        mock_openapi_module.register_openapi_metadata = mock_register

        with patch.dict("sys.modules", {"azure_functions_openapi": mock_openapi_module}):
            from azure_functions_langgraph.openapi import register_with_openapi

            count = register_with_openapi(app)

        # 2 graphs * 2 routes + 1 health = 5
        assert count == 5

    def test_tags_set_to_graph_name(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="my_agent")

        mock_register = MagicMock()
        mock_openapi_module = MagicMock()
        mock_openapi_module.register_openapi_metadata = mock_register

        with patch.dict("sys.modules", {"azure_functions_openapi": mock_openapi_module}):
            from azure_functions_langgraph.openapi import register_with_openapi

            register_with_openapi(app)

        # Graph routes should have tags=[graph_name]
        graph_calls = [
            c for c in mock_register.call_args_list
            if c.kwargs.get("tags") == ["my_agent"]
        ]
        assert len(graph_calls) == 2  # invoke + stream

        # Health route should have tags=["system"]
        system_calls = [
            c for c in mock_register.call_args_list
            if c.kwargs.get("tags") == ["system"]
        ]
        assert len(system_calls) == 1

    def test_empty_app(self) -> None:
        app = LangGraphApp()

        mock_register = MagicMock()
        mock_openapi_module = MagicMock()
        mock_openapi_module.register_openapi_metadata = mock_register

        with patch.dict("sys.modules", {"azure_functions_openapi": mock_openapi_module}):
            from azure_functions_langgraph.openapi import register_with_openapi

            count = register_with_openapi(app)

        # Only health route
        assert count == 1


# ------------------------------------------------------------------
# _build_request_body tests
# ------------------------------------------------------------------


class TestBuildRequestBody:
    def test_valid_pydantic_model(self) -> None:
        from azure_functions_langgraph.openapi import _build_request_body

        class MyModel(BaseModel):
            name: str
            count: int = 0

        body = _build_request_body(MyModel)
        assert body["required"] is True
        assert "application/json" in body["content"]
        schema = body["content"]["application/json"]["schema"]
        assert "properties" in schema
        assert "name" in schema["properties"]
        assert "count" in schema["properties"]

    def test_non_pydantic_model_raises_type_error(self) -> None:
        from azure_functions_langgraph.openapi import _build_request_body

        with pytest.raises(TypeError, match="Pydantic BaseModel subclass"):
            _build_request_body(dict)

    def test_non_class_raises_type_error(self) -> None:
        from azure_functions_langgraph.openapi import _build_request_body

        with pytest.raises(TypeError, match="Pydantic BaseModel subclass"):
            _build_request_body("not a class")  # type: ignore[arg-type]

    def test_instance_raises_type_error(self) -> None:
        from azure_functions_langgraph.openapi import _build_request_body

        class MyModel(BaseModel):
            x: int

        with pytest.raises(TypeError, match="Pydantic BaseModel subclass"):
            _build_request_body(MyModel(x=1))  # type: ignore[arg-type]


# ------------------------------------------------------------------
# _validate_model tests
# ------------------------------------------------------------------


class TestValidateModel:
    def test_valid_response_model(self) -> None:
        from azure_functions_langgraph.openapi import _validate_model

        class MyResponse(BaseModel):
            answer: str

        # Should not raise
        _validate_model(MyResponse, "response_model")

    def test_non_pydantic_response_model_raises(self) -> None:
        from azure_functions_langgraph.openapi import _validate_model

        with pytest.raises(TypeError, match="response_model must be a Pydantic BaseModel"):
            _validate_model(dict, "response_model")

    def test_non_class_response_model_raises(self) -> None:
        from azure_functions_langgraph.openapi import _validate_model

        with pytest.raises(TypeError, match="response_model must be a Pydantic BaseModel"):
            _validate_model("not a class", "response_model")

    def test_response_model_validated_during_registration(self) -> None:
        """response_model should be validated when register_with_openapi is called."""
        app = LangGraphApp()
        app.register(
            graph=FakeCompiledGraph(),
            name="agent",
            response_model=dict,
        )

        mock_register = MagicMock()
        mock_openapi_module = MagicMock()
        mock_openapi_module.register_openapi_metadata = mock_register

        with patch.dict("sys.modules", {"azure_functions_openapi": mock_openapi_module}):
            from azure_functions_langgraph.openapi import register_with_openapi

            with pytest.raises(TypeError, match="response_model must be a Pydantic BaseModel"):
                register_with_openapi(app)
