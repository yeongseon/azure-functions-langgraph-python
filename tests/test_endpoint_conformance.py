"""Cross-package semantic-conformance tests for the ``endpoint`` metadata namespace (issue #368).

The Azure Functions Python DX Toolkit shares the ``_azure_functions_metadata["endpoint"]``
convention by **copy, not by a runtime dependency**: producers (this package,
``azure-functions-validation``) emit it, a consumer (``azure-functions-openapi``)
turns it into an OpenAPI spec, and a validator (``azure-functions-doctor``) checks
it — none of them import a shared runtime package (see issue #368; the shared
``azure-functions-contracts`` runtime dependency was explicitly rejected).

These tests pin that contract from *this* package's side using a **vendored** JSON
Schema (``tests/fixtures/endpoint_metadata_schema.v1.json``) and a dependency-free
validator, then simulate the OpenAPI consumer and the Doctor validator against a
canonical full-stack fixture app. If this package's runtime output drifts from the
vendored contract, or the vendored contract drifts from the code's own
``EndpointMetadata`` TypedDict, these tests fail loudly — without pulling in
``jsonschema`` or any sibling package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from azure_functions_langgraph._endpoint import (
    ENDPOINT_METADATA_VERSION,
    EndpointMetadata,
    build_endpoint_metadata,
)
from azure_functions_langgraph.app import LangGraphApp
from tests.conftest import FakeStatefulGraph

_SCHEMA_PATH = Path(__file__).parent / "fixtures" / "endpoint_metadata_schema.v1.json"


# ---------------------------------------------------------------------------
# Fixture models + canonical full-stack app
# ---------------------------------------------------------------------------


class _Nested(BaseModel):
    value: int


class _RequestBody(BaseModel):
    item: _Nested
    note: str = "hello"


class _ResponseBody(BaseModel):
    message: str


def _load_vendored_schema() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return result


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


def _endpoint_payload(handler: object) -> dict[str, Any]:
    metadata = getattr(handler, "_azure_functions_metadata", None)
    assert isinstance(metadata, dict), "handler is missing the metadata convention attr"
    assert "endpoint" in metadata, "handler is missing the 'endpoint' namespace"
    payload = metadata["endpoint"]
    assert isinstance(payload, dict)
    return payload


def _full_stack_payloads() -> dict[str, dict[str, Any]]:
    """Register invoke/stream/state handlers and return their endpoint payloads."""
    app = LangGraphApp()
    app.register(
        graph=FakeStatefulGraph(),
        name="agent",
        request_model=_RequestBody,
        response_model=_ResponseBody,
    )
    functions = _get_registered_functions(app.function_app)
    return {
        "invoke": _endpoint_payload(functions["aflg_agent_invoke"]),
        "stream": _endpoint_payload(functions["aflg_agent_stream"]),
        "state": _endpoint_payload(functions["aflg_agent_state"]),
    }


# ---------------------------------------------------------------------------
# Dependency-free JSON-Schema validator (sufficient for the vendored contract)
# ---------------------------------------------------------------------------

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _matches_type(value: Any, type_spec: Any) -> bool:
    types = type_spec if isinstance(type_spec, list) else [type_spec]
    return any(_TYPE_CHECKS[t](value) for t in types)


def _validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Minimal structural validator. Returns a list of human-readable errors."""
    errors: list[str] = []

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']!r}")

    if "type" in schema:
        if not _matches_type(value, schema["type"]):
            errors.append(f"{path}: expected type {schema['type']!r}, got {type(value).__name__}")
            return errors  # further checks assume the type matched

    if isinstance(value, dict) and (schema.get("type") == "object" or "properties" in schema):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required property {req!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                errors.append(f"{path}: unexpected propert(ies) {sorted(extra)!r}")
        for key, subschema in props.items():
            if key in value:
                errors.extend(_validate(value[key], subschema, f"{path}.{key}"))

    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(_validate(item, schema["items"], f"{path}[{i}]"))

    return errors


# ---------------------------------------------------------------------------
# Consumer / validator simulations
# ---------------------------------------------------------------------------


def _collect_refs(node: Any) -> list[str]:
    """Recursively collect every ``$ref`` string in a JSON-Schema-ish structure."""
    refs: list[str] = []
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "$ref" and isinstance(val, str):
                refs.append(val)
            else:
                refs.extend(_collect_refs(val))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_collect_refs(item))
    return refs


def _assert_self_contained(schema: dict[str, Any], where: str) -> None:
    """Every ``$ref`` must resolve inside the schema's own ``$defs`` (issue #368).

    This is exactly what the ``azure-functions-openapi`` consumer relies on: it
    embeds the payload's schemas into an OpenAPI document and re-homes ``$defs``,
    so a ``$ref`` pointing outside ``#/$defs/`` (e.g. at the user's model classes
    or a sibling package) would produce a dangling reference.
    """
    defs = set(schema.get("$defs", {}))
    for ref in _collect_refs(schema):
        assert ref.startswith("#/$defs/"), f"{where}: non-self-contained $ref {ref!r}"
        name = ref[len("#/$defs/") :]
        assert name in defs, f"{where}: $ref {ref!r} has no matching $defs entry"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVendoredSchemaConformance:
    """Runtime output conforms to the vendored contract, and the contract tracks the code."""

    def test_all_full_stack_payloads_validate_against_vendored_schema(self) -> None:
        schema = _load_vendored_schema()
        for route, payload in _full_stack_payloads().items():
            errors = _validate(payload, schema)
            assert not errors, f"{route} payload violates vendored schema: {errors}"

    def test_builder_output_validates_against_vendored_schema(self) -> None:
        schema = _load_vendored_schema()
        payload = build_endpoint_metadata(
            request_model=_RequestBody,
            response_model=_ResponseBody,
            parameters=[
                {"name": "q", "in": "query", "required": False, "schema": {"type": "string"}}
            ],
            success_status_code=201,
        )
        assert not _validate(dict(payload), schema)

    def test_vendored_schema_matches_endpoint_metadata_typeddict(self) -> None:
        """Drift guard: the vendored contract's properties must equal the runtime keys.

        Prevents the vendored fixture from silently rotting when
        ``EndpointMetadata`` gains or loses a field.
        """
        schema = _load_vendored_schema()
        schema_props = set(schema["properties"])
        typeddict_keys = set(EndpointMetadata.__annotations__)
        assert schema_props == typeddict_keys, (
            f"vendored schema drifted from EndpointMetadata: "
            f"only-in-schema={schema_props - typeddict_keys}, "
            f"only-in-code={typeddict_keys - schema_props}"
        )
        assert set(schema["required"]) == typeddict_keys, (
            "all endpoint keys are required by contract"
        )

    def test_vendored_schema_pins_current_version(self) -> None:
        schema = _load_vendored_schema()
        assert schema["properties"]["version"]["const"] == ENDPOINT_METADATA_VERSION


class TestOpenApiConsumerConformance:
    """Simulate the azure-functions-openapi consumer reading the payload."""

    def test_request_and_response_schemas_are_self_contained(self) -> None:
        payloads = _full_stack_payloads()
        invoke = payloads["invoke"]
        assert invoke["request_body"] is not None
        _assert_self_contained(invoke["request_body"], "invoke.request_body")
        assert invoke["responses"] is not None
        for status, response in invoke["responses"].items():
            _assert_self_contained(response["schema"], f"invoke.responses.{status}")

    def test_consumer_can_build_operation_object(self) -> None:
        """The consumer maps the payload onto an OpenAPI operation without extra data."""
        invoke = _full_stack_payloads()["invoke"]
        operation: dict[str, Any] = {"responses": {}}
        if invoke["request_body"] is not None:
            operation["requestBody"] = {
                "required": invoke["request_body_required"],
                "content": {"application/json": {"schema": invoke["request_body"]}},
            }
        if invoke["parameters"]:
            operation["parameters"] = invoke["parameters"]
        for status, response in (invoke["responses"] or {}).items():
            operation["responses"][status] = {
                "description": "OK",
                "content": {"application/json": {"schema": response["schema"]}},
            }
        # Round-trips as JSON (fully serialisable, no Python objects leaked).
        assert json.loads(json.dumps(operation))["responses"].keys() == {"200"}
        assert operation["requestBody"]["required"] is True

    def test_stream_route_has_no_json_responses(self) -> None:
        """SSE stream advertises a request body but no single JSON response."""
        stream = _full_stack_payloads()["stream"]
        assert stream["request_body"] is not None
        assert stream["responses"] is None


class TestDoctorValidatorConformance:
    """Simulate the azure-functions-doctor validator inspecting the payload."""

    def test_doctor_required_keys_and_version(self) -> None:
        required = {
            "version",
            "request_body",
            "request_body_required",
            "parameters",
            "responses",
        }
        for route, payload in _full_stack_payloads().items():
            assert required <= set(payload), f"{route}: doctor-required keys missing"
            assert payload["version"] == ENDPOINT_METADATA_VERSION
            assert isinstance(payload["request_body_required"], bool)
            assert isinstance(payload["parameters"], list)

    def test_state_route_path_parameter_is_well_formed(self) -> None:
        state = _full_stack_payloads()["state"]
        assert state["request_body"] is None
        assert state["responses"] is None
        assert state["parameters"] == [
            {"name": "thread_id", "in": "path", "required": True, "schema": {"type": "string"}}
        ]


class TestVendoredValidatorItself:
    """Guard the dependency-free validator so a false-negative can't hide drift."""

    def test_validator_flags_missing_required_key(self) -> None:
        schema = _load_vendored_schema()
        broken: dict[str, Any] = {
            "version": 1,
            "request_body": None,
            "request_body_required": False,
            "parameters": [],
            # 'responses' intentionally omitted
        }
        errors = _validate(broken, schema)
        assert any("responses" in e for e in errors)

    def test_validator_flags_wrong_version_const(self) -> None:
        schema = _load_vendored_schema()
        broken: dict[str, Any] = {
            "version": 999,
            "request_body": None,
            "request_body_required": False,
            "parameters": [],
            "responses": None,
        }
        errors = _validate(broken, schema)
        assert any("version" in e for e in errors)

    def test_validator_flags_wrong_type(self) -> None:
        schema = _load_vendored_schema()
        broken: dict[str, Any] = {
            "version": 1,
            "request_body": None,
            "request_body_required": "yes",  # not a boolean
            "parameters": [],
            "responses": None,
        }
        errors = _validate(broken, schema)
        assert any("request_body_required" in e for e in errors)

    def test_validator_flags_unexpected_property(self) -> None:
        schema = _load_vendored_schema()
        broken: dict[str, Any] = {
            "version": 1,
            "request_body": None,
            "request_body_required": False,
            "parameters": [],
            "responses": None,
            "surprise": True,
        }
        errors = _validate(broken, schema)
        assert any("surprise" in e for e in errors)
