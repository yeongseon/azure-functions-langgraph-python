"""Tests for the shared ``endpoint`` metadata namespace (issue #294).

``LangGraphApp`` writes ``_azure_functions_metadata["endpoint"]`` onto each
per-graph HTTP handler so ``azure-functions-openapi`` can generate an OpenAPI
spec directly from the handler, without importing this package. The payload is a
*self-contained* JSON-Schema contract replicated from ``azure-functions-validation``.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from azure_functions_langgraph._endpoint import (
    ENDPOINT_METADATA_VERSION,
    build_endpoint_metadata,
    set_endpoint_metadata,
)
from azure_functions_langgraph.app import LangGraphApp, _EndpointSpec, _resolve_endpoint_spec
from tests.conftest import FakeCompiledGraph, FakeStatefulGraph


class RequestBody(BaseModel):
    """Request model with an alias and a required + optional field."""

    user_id: int = Field(alias="userId")
    note: str = "hello"


class OptionalBody(BaseModel):
    """Request model whose fields are all optional."""

    note: str = "hi"


class ResponseBody(BaseModel):
    """Response model."""

    message: str


def _endpoint_meta(handler: object) -> dict[str, Any]:
    metadata = getattr(handler, "_azure_functions_metadata", None)
    assert isinstance(metadata, dict)
    assert "endpoint" in metadata
    payload = metadata["endpoint"]
    assert isinstance(payload, dict)
    return payload


def _get_registered_functions(func_app: object) -> dict[str, object]:
    functions: dict[str, object] = {}
    builders = getattr(func_app, "_function_builders", [])
    for builder in builders:
        fn = getattr(builder, "_function", None)
        if fn is not None:
            fn_name = getattr(fn, "get_function_name", lambda: None)()
            user_fn = getattr(fn, "get_user_function", lambda: None)()
            if fn_name and user_fn:
                functions[fn_name] = user_fn
    return functions


class TestEndpointNamespaceOnHandlers:
    """The endpoint namespace is attached to per-graph handlers with correct shape."""

    def test_invoke_handler_has_endpoint_metadata(self) -> None:
        app = LangGraphApp()
        app.register(
            graph=FakeCompiledGraph(),
            name="agent",
            request_model=RequestBody,
            response_model=ResponseBody,
        )
        invoke_fn = _get_registered_functions(app.function_app)["aflg_agent_invoke"]

        payload = _endpoint_meta(invoke_fn)
        assert payload["version"] == ENDPOINT_METADATA_VERSION
        assert payload["request_body"] == RequestBody.model_json_schema(
            by_alias=True, ref_template="#/$defs/{model}", mode="validation"
        )
        # user_id is required (no default) -> request body required.
        assert payload["request_body_required"] is True
        assert payload["parameters"] == []
        assert payload["responses"] == {
            "200": {
                "schema": ResponseBody.model_json_schema(
                    by_alias=True, ref_template="#/$defs/{model}", mode="serialization"
                )
            }
        }

    def test_stream_handler_has_request_body_but_no_responses(self) -> None:
        app = LangGraphApp()
        app.register(
            graph=FakeCompiledGraph(),
            name="agent",
            request_model=RequestBody,
            response_model=ResponseBody,
        )
        stream_fn = _get_registered_functions(app.function_app)["aflg_agent_stream"]

        payload = _endpoint_meta(stream_fn)
        # Stream reuses the request model but streams SSE (no single JSON response).
        assert payload["request_body"] is not None
        assert payload["responses"] is None
        assert payload["parameters"] == []

    def test_state_handler_has_thread_id_path_param_only(self) -> None:
        app = LangGraphApp()
        app.register(
            graph=FakeStatefulGraph(),
            name="stateful",
            request_model=RequestBody,
            response_model=ResponseBody,
        )
        state_fn = _get_registered_functions(app.function_app)["aflg_stateful_state"]

        payload = _endpoint_meta(state_fn)
        assert payload["request_body"] is None
        assert payload["request_body_required"] is False
        assert payload["responses"] is None
        assert payload["parameters"] == [
            {"name": "thread_id", "in": "path", "required": True, "schema": {"type": "string"}}
        ]

    def test_endpoint_metadata_with_no_models(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        invoke_fn = _get_registered_functions(app.function_app)["aflg_agent_invoke"]

        payload = _endpoint_meta(invoke_fn)
        assert payload["request_body"] is None
        assert payload["request_body_required"] is False
        assert payload["responses"] is None
        assert payload["parameters"] == []

    def test_endpoint_namespace_preserves_langgraph_namespace(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        invoke_fn = _get_registered_functions(app.function_app)["aflg_agent_invoke"]

        metadata = getattr(invoke_fn, "_azure_functions_metadata")
        assert "langgraph" in metadata
        assert "endpoint" in metadata
        assert metadata["langgraph"]["graph_name"] == "agent"


class TestBuildEndpointMetadata:
    """Unit coverage for the replicated builder."""

    def test_request_body_required_true_when_field_required(self) -> None:
        payload = build_endpoint_metadata(request_model=RequestBody, response_model=None)
        assert payload["request_body_required"] is True

    def test_request_body_required_false_when_all_optional(self) -> None:
        payload = build_endpoint_metadata(request_model=OptionalBody, response_model=None)
        assert payload["request_body"] is not None
        assert payload["request_body_required"] is False

    def test_success_status_code_key(self) -> None:
        payload = build_endpoint_metadata(
            request_model=None, response_model=ResponseBody, success_status_code=201
        )
        assert payload["responses"] is not None
        assert set(payload["responses"]) == {"201"}

    def test_success_status_code_falls_back_to_200(self) -> None:
        payload = build_endpoint_metadata(
            request_model=None, response_model=ResponseBody, success_status_code=0
        )
        assert payload["responses"] is not None
        assert set(payload["responses"]) == {"200"}

    def test_parameters_passed_through(self) -> None:
        params = [{"name": "q", "in": "query", "required": False, "schema": {"type": "string"}}]
        payload = build_endpoint_metadata(
            request_model=None, response_model=None, parameters=params
        )
        assert payload["parameters"] == params

    def test_non_model_inputs_are_ignored(self) -> None:
        payload = build_endpoint_metadata(
            request_model="not-a-model",  # type: ignore[arg-type]
            response_model=object,  # type: ignore[arg-type]
        )
        assert payload["request_body"] is None
        assert payload["responses"] is None

    def test_by_alias_and_ref_template_fidelity(self) -> None:
        class Nested(BaseModel):
            value: int

        class Wrapper(BaseModel):
            item: Nested
            tag: str = Field(alias="tagName")

        payload = build_endpoint_metadata(request_model=Wrapper, response_model=None)
        schema = payload["request_body"]
        assert schema is not None
        # by_alias -> property uses the serialization alias.
        assert "tagName" in schema["properties"]
        # ref_template -> nested model referenced via #/$defs/{model}.
        assert schema["properties"]["item"]["$ref"] == "#/$defs/Nested"


class TestSetEndpointMetadata:
    """The merge helper does not clobber sibling namespaces."""

    def test_merge_preserves_other_namespaces(self) -> None:
        def handler() -> None:
            pass

        setattr(handler, "_azure_functions_metadata", {"langgraph": {"version": 1}})
        set_endpoint_metadata(
            handler, build_endpoint_metadata(request_model=None, response_model=None)
        )

        metadata = getattr(handler, "_azure_functions_metadata")
        assert "langgraph" in metadata
        assert "endpoint" in metadata

    def test_merge_on_bare_handler(self) -> None:
        def handler() -> None:
            pass

        set_endpoint_metadata(
            handler, build_endpoint_metadata(request_model=None, response_model=None)
        )
        metadata = getattr(handler, "_azure_functions_metadata")
        assert set(metadata) == {"endpoint"}

    def test_merge_ignores_non_dict_existing_attr(self) -> None:
        def handler() -> None:
            pass

        setattr(handler, "_azure_functions_metadata", "not-a-dict")
        set_endpoint_metadata(
            handler, build_endpoint_metadata(request_model=None, response_model=None)
        )
        metadata = getattr(handler, "_azure_functions_metadata")
        assert set(metadata) == {"endpoint"}


class TestResolveEndpointSpec:
    """The shared resolver is the single source of truth for both writers."""

    def test_invoke_spec(self) -> None:
        reg = _make_reg(request_model=RequestBody, response_model=ResponseBody)
        spec = _resolve_endpoint_spec(reg, "invoke")
        assert spec.request_model is RequestBody
        assert spec.response_model is ResponseBody
        assert spec.parameters == ()

    def test_stream_spec(self) -> None:
        reg = _make_reg(request_model=RequestBody, response_model=ResponseBody)
        spec = _resolve_endpoint_spec(reg, "stream")
        assert spec.request_model is RequestBody
        assert spec.response_model is None
        assert spec.parameters == ()

    def test_state_spec(self) -> None:
        reg = _make_reg(request_model=RequestBody, response_model=ResponseBody)
        spec = _resolve_endpoint_spec(reg, "state")
        assert spec.request_model is None
        assert spec.response_model is None
        assert spec.parameters == (
            {"name": "thread_id", "in": "path", "required": True, "schema": {"type": "string"}},
        )

    def test_unknown_spec_is_empty(self) -> None:
        reg = _make_reg(request_model=RequestBody, response_model=ResponseBody)
        spec = _resolve_endpoint_spec(reg, "unknown")
        assert spec == _EndpointSpec()


def _make_reg(*, request_model: Optional[type[Any]], response_model: Optional[type[Any]]) -> Any:
    """Build a LangGraphApp registration record via the public register() API."""
    app = LangGraphApp()
    app.register(
        graph=FakeStatefulGraph(),
        name="agent",
        request_model=request_model,
        response_model=response_model,
    )
    return app._registrations["agent"]
