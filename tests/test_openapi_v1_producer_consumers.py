"""Certify native v1 discovery separately from the deprecated shape bridge."""

from __future__ import annotations

from collections.abc import Iterator
from hashlib import sha256
from importlib.metadata import version
from importlib.resources import files
import json
import logging
from pathlib import Path
from typing import Final, assert_never

from pydantic import BaseModel, JsonValue
import pytest

from azure_functions_langgraph import LangGraphApp
from tests.conftest import FakeCompiledGraph, FakeStatefulGraph
from tests.test_endpoint_conformance import (
    _endpoint_payload,
    _get_registered_functions,
    _load_vendored_schema,
    _validate,
)

_FIXTURE: Final = Path(__file__).parent / "fixtures" / "openapi_native_producer.v1.json"
_VALIDATION_SCHEMA_SHA256: Final = (
    "fda7b9c4ccaca770c4af2c55b7e174f52424f78484af26e8d00360f462cc057e"
)


class ProducerBody(BaseModel):
    value: str


def _normalize_generated_schema(value: JsonValue) -> JsonValue:
    """Canonicalize only equivalent forms observed across supported Pydantic versions."""
    match value:
        case dict() as node:
            normalized = {key: _normalize_generated_schema(item) for key, item in node.items()}
            if (
                normalized.get("type") == "object"
                and normalized.get("additionalProperties") is True
            ):
                del normalized["additionalProperties"]
            match normalized.get("allOf"):
                case [{"$ref": str() as ref, **extras}] if not extras and "$ref" not in normalized:
                    del normalized["allOf"]
                    normalized["$ref"] = ref
            return normalized
        case list() as items:
            return [_normalize_generated_schema(item) for item in items]
        case str() | int() | float() | bool() | None:
            return value
        case unreachable:
            assert_never(unreachable)


def _component_ref(schema: JsonValue) -> str:
    match _normalize_generated_schema(schema):
        case {"$ref": str() as ref}:
            return ref
        case other:
            raise AssertionError(f"expected a component reference, got {other!r}")


@pytest.fixture
def producer_app() -> LangGraphApp:
    app = LangGraphApp()
    app.register(
        FakeCompiledGraph(),
        "ordinary",
        description="Recorded graph documentation",
        request_model=ProducerBody,
        response_model=ProducerBody,
    )
    app.register(
        FakeStatefulGraph(), "stateful", request_model=ProducerBody, response_model=ProducerBody
    )
    return app


@pytest.fixture
def companion_registry() -> Iterator[None]:
    pytest.importorskip(
        "azure_functions_openapi",
        reason="native v1 consumer certification requires optional azure-functions-openapi",
    )
    from azure_functions_openapi.decorator import clear_openapi_registry

    clear_openapi_registry()
    yield
    clear_openapi_registry()


@pytest.fixture
def validation_schema() -> JsonValue:
    pytest.importorskip(
        "azure_functions_validation",
        reason="upstream schema certification requires optional openapi Hatch test dependencies",
    )
    pytest.importorskip("jsonschema", reason="upstream schema certification requires jsonschema")
    assert version("azure-functions-validation") == "0.16.0"
    resource = files("azure_functions_validation").joinpath("schemas")
    raw = resource.joinpath("endpoint.schema.json").read_bytes()
    assert sha256(raw).hexdigest() == _VALIDATION_SCHEMA_SHA256
    assert resource.joinpath("endpoint.schema.sha256").read_text().split() == [
        _VALIDATION_SCHEMA_SHA256,
        "endpoint.schema.json",
    ]
    schema: JsonValue = json.loads(raw)
    return schema


def test_native_v1_payloads_validate_against_pinned_validation_schema(
    producer_app: LangGraphApp, validation_schema: JsonValue
) -> None:
    # Given: the actual upstream artifact, not the stricter local v1 contract.
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(validation_schema)
    validator = Draft202012Validator(validation_schema)

    # When: every graph handler exposes its unnormalized endpoint namespace.
    functions = _get_registered_functions(producer_app.function_app)
    payloads = {
        name: _endpoint_payload(handler)
        for name, handler in functions.items()
        if not name.startswith("aflg_health")
    }

    # Then: ordinary/stateful invoke, stream and state all satisfy the pinned schema.
    assert set(payloads) == set(json.loads(_FIXTURE.read_text(encoding="utf-8"))["routes"])
    for name, payload in payloads.items():
        assert not list(validator.iter_errors(payload)), name


@pytest.mark.parametrize(
    "replacement",
    [
        {"version": 999},
        {"request_body_required": "yes"},
        {"responses": {"600": {"schema": {}}}},
        {"responses": {"200": {"schema": "not a schema"}}},
        {"parameters": [{"name": "thread_id", "in": "unsupported"}]},
    ],
    ids=["unknown-version", "body-required-type", "status-code", "response-schema", "parameter"],
)
def test_pinned_validation_schema_rejects_malformed_native_payload(
    producer_app: LangGraphApp,
    validation_schema: JsonValue,
    replacement: dict[str, JsonValue],
) -> None:
    # Given: a native invoke payload with one intentionally invalid contract field.
    from jsonschema import Draft202012Validator

    functions = _get_registered_functions(producer_app.function_app)
    payload = {**_endpoint_payload(functions["aflg_ordinary_invoke"]), **replacement}

    # When: the full Draft 2020-12 validator resolves upstream references and constraints.
    errors = list(Draft202012Validator(validation_schema).iter_errors(payload))

    # Then: malformed fields cannot be certified by a permissive partial validator.
    assert errors


def test_recorded_fixture_matches_native_v1_producer(producer_app: LangGraphApp) -> None:
    # Given: a compact recording sharing identical payloads between graph routes.
    recorded = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    functions = _get_registered_functions(producer_app.function_app)

    # When: actual registered handlers expose their endpoint metadata.
    actual = {
        name: _endpoint_payload(handler)
        for name, handler in functions.items()
        if not name.startswith("aflg_health")
    }

    # Then: adapter semantics match, allowing only equivalent generated schema forms.
    expected = {name: recorded["payloads"][kind] for name, kind in recorded["routes"].items()}
    assert {name: _normalize_generated_schema(payload) for name, payload in actual.items()} == {
        name: _normalize_generated_schema(payload) for name, payload in expected.items()
    }
    for payload in actual.values():
        assert not _validate(payload, _load_vendored_schema())
    assert (
        producer_app.get_app_metadata().graphs["ordinary"].description
        == recorded["documentation"]["description"]
    )


@pytest.mark.usefixtures("companion_registry")
def test_installed_consumer_generates_recorded_v1_routes(producer_app: LangGraphApp) -> None:
    # Given: documentation-only registration avoids the deprecated bridge's shapes.
    from azure_functions_openapi import (
        OpenAPIRegistry,
        generate_openapi_spec,
        register_openapi_metadata,
    )

    recorded = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    registry = OpenAPIRegistry()
    register_openapi_metadata(
        path="/api/graphs/ordinary/invoke",
        method="POST",
        description=recorded["documentation"]["description"],
        registry=registry,
    )

    # When: the installed consumer scans the real producer's handler namespaces.
    spec = generate_openapi_spec(app=producer_app.function_app, registry=registry)

    # Then: binding methods/paths and successful statuses survive version-aware reading.
    expected = {
        "/api/graphs/ordinary/invoke": "post",
        "/api/graphs/ordinary/stream": "post",
        "/api/graphs/stateful/invoke": "post",
        "/api/graphs/stateful/stream": "post",
        "/api/graphs/stateful/threads/{thread_id}/state": "get",
    }
    assert {path: set(ops) for path, ops in spec["paths"].items()} == {
        path: {method} for path, method in expected.items()
    }
    for path, method in expected.items():
        assert set(spec["paths"][path][method]["responses"]) == {"200"}
    for graph in ("ordinary", "stateful"):
        operation = spec["paths"][f"/api/graphs/{graph}/invoke"]["post"]
        request = operation["requestBody"]
        assert request["required"] is True
        schema = request["content"]["application/json"]["schema"]
        assert schema["required"] == ["input"]
        assert _component_ref(schema["properties"]["input"]).startswith("#/components/schemas/")
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert response["required"] == ["output"]
        ref = _component_ref(response["properties"]["output"])
        body = spec["components"]["schemas"][ref.removeprefix("#/components/schemas/")]
        assert body["properties"]["value"]["type"] == "string"
        stream = spec["paths"][f"/api/graphs/{graph}/stream"]["post"]
        assert stream["requestBody"]["content"]["application/json"]["schema"]["required"] == [
            "input"
        ]
    state = spec["paths"]["/api/graphs/stateful/threads/{thread_id}/state"]["get"]
    assert "requestBody" not in state
    assert state["parameters"] == recorded["payloads"]["state"]["parameters"]
    assert (
        spec["paths"]["/api/graphs/ordinary/invoke"]["post"]["description"]
        == recorded["documentation"]["description"]
    )


@pytest.mark.usefixtures("companion_registry")
def test_unknown_metadata_version_has_visible_consumer_warning(
    producer_app: LangGraphApp, caplog: pytest.LogCaptureFixture
) -> None:
    # Given: a future producer namespace, not LangGraph's output-version option.
    from azure_functions_openapi import generate_openapi_spec

    functions = _get_registered_functions(producer_app.function_app)
    payload = _endpoint_payload(functions["aflg_ordinary_invoke"])
    payload["version"] = 999

    # When: installed generation encounters the unknown endpoint version.
    with caplog.at_level(logging.WARNING, logger="azure_functions_openapi.bridge"):
        spec = generate_openapi_spec(app=producer_app.function_app)

    # Then: a visible warning accompanies binding-only fallback, not schema success.
    assert any(
        record.name == "azure_functions_openapi.bridge"
        and record.levelno >= logging.WARNING
        and "999" in record.getMessage()
        for record in caplog.records
    )
    assert "requestBody" not in spec["paths"]["/api/graphs/ordinary/invoke"]["post"]
    response = spec["paths"]["/api/graphs/ordinary/invoke"]["post"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"] == {"type": "object"}
