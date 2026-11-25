"""Real-companion interoperability tests for the OpenAPI bridge (#655).

Exercises azure_functions_langgraph.openapi.register_with_openapi against the
actual installed azure-functions-openapi-python package (not a mock), so a
future regression in the companion's parameter-type contract (or this
bridge's conversion of LangGraphApp's immutable route metadata) cannot be
hidden by source-tree mocks or a CI lane that never installs the real
companion.

Requires the optional 'openapi' extra:

    pip install azure-functions-langgraph[openapi]
    make test-openapi

Skips cleanly (not an error) when azure-functions-openapi is not installed,
so the default dev/CI test lane -- which intentionally runs WITHOUT this
companion to exercise the "package absent" behavior in
tests/test_example_install_certification.py and
tests/test_openapi_bridge.py::TestImportGuard -- is unaffected.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

azure_functions_openapi = pytest.importorskip(
    "azure_functions_openapi",
    reason="requires the optional 'openapi' extra: pip install azure-functions-langgraph[openapi]",
)

from azure_functions_langgraph import LangGraphApp  # noqa: E402
from azure_functions_langgraph.openapi import register_with_openapi  # noqa: E402


class _OrdinaryGraph:
    """No get_state -- no state route should be generated."""

    checkpointer = None

    def invoke(self, value: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
        return value

    def stream(
        self, value: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> Generator[Any, None, None]:
        yield value


class _StatefulGraph:
    """Exposes get_state -- a state route with a thread_id path parameter
    must be generated and registered."""

    checkpointer = object()

    def invoke(self, value: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
        return value

    def stream(
        self, value: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> Generator[Any, None, None]:
        yield value

    def get_state(self, config: dict[str, Any]) -> None:
        return None


@pytest.fixture(autouse=True)
def _clear_openapi_registry() -> Generator[None, None, None]:
    """Reset the companion's module-level route registry between tests."""
    from azure_functions_openapi.decorator import clear_openapi_registry

    clear_openapi_registry()
    yield
    clear_openapi_registry()


def test_ordinary_graph_registers_without_raising() -> None:
    app = LangGraphApp()
    app.register(graph=_OrdinaryGraph(), name="ordinary")

    count = register_with_openapi(app)

    assert count == 4  # invoke, stream, health, health/details


def test_stateful_graph_registers_state_route_with_path_parameter() -> None:
    app = LangGraphApp()
    app.register(graph=_StatefulGraph(), name="stateful")

    count = register_with_openapi(app)

    assert count == 5  # invoke, stream, state, health, health/details

    from azure_functions_openapi.decorator import get_openapi_registry

    registry = get_openapi_registry()
    state_key = next(k for k in registry if "threads" in k and "state" in k)
    parameters = registry[state_key]["parameters"]
    assert isinstance(parameters, list)
    assert all(isinstance(p, dict) for p in parameters)
    assert parameters[0]["name"] == "thread_id"
    assert parameters[0]["in"] == "path"
    assert parameters[0]["required"] is True
    assert parameters[0]["schema"] == {"type": "string"}


def test_app_metadata_remains_immutable_after_registration() -> None:
    """Consuming the metadata through the bridge must not mutate the app's
    own immutable snapshot (#655 acceptance: preserve immutability)."""
    app = LangGraphApp()
    app.register(graph=_StatefulGraph(), name="stateful")

    before = app.get_app_metadata()
    register_with_openapi(app)
    after = app.get_app_metadata()

    state_route_before = next(r for r in before.graphs["stateful"].routes if r.parameters)
    state_route_after = next(r for r in after.graphs["stateful"].routes if r.parameters)
    assert state_route_before.parameters == state_route_after.parameters
    for param in state_route_before.parameters:
        # Still the original immutable mapping type -- never replaced by a
        # mutable dict on the app's own metadata object.
        assert type(param).__name__ == "mappingproxy"


def test_invoke_route_request_and_response_schemas_present() -> None:
    app = LangGraphApp()
    app.register(graph=_OrdinaryGraph(), name="ordinary")
    register_with_openapi(app)

    from azure_functions_openapi.decorator import get_openapi_registry

    registry = get_openapi_registry()
    invoke_key = next(k for k in registry if k.endswith("/invoke"))
    invoke_route = registry[invoke_key]
    assert invoke_route["request_body"] is not None
    assert "application/json" in invoke_route["request_body"]["content"]
    assert invoke_route["response_model"] is not None


@pytest.mark.parametrize("prefix", ["api", "v1", ""])
def test_generated_document_preserves_native_paths_when_prefix_changes(prefix: str) -> None:
    # Given: a stateful graph and metadata matching the host's route prefix.
    app = LangGraphApp(route_prefix=prefix)
    app.register(graph=_StatefulGraph(), name="agent")
    register_with_openapi(app)

    # When: the real companion generates the bridge-only document.
    spec = azure_functions_openapi.generate_openapi_spec(
        route_prefix=f"/{prefix}" if prefix else ""
    )

    # Then: prefixes occur once, and native path parameters survive generation.
    root = f"/{prefix}" if prefix else ""
    assert {path: set(operations) for path, operations in spec["paths"].items()} == {
        f"{root}/graphs/agent/invoke": {"post"},
        f"{root}/graphs/agent/stream": {"post"},
        f"{root}/graphs/agent/threads/{{thread_id}}/state": {"get"},
        f"{root}/health": {"get"},
        f"{root}/health/details": {"get"},
    }
    state = spec["paths"][f"{root}/graphs/agent/threads/{{thread_id}}/state"]["get"]
    assert state["parameters"] == [
        {"name": "thread_id", "in": "path", "required": True, "schema": {"type": "string"}}
    ]


def test_generated_document_keeps_invoke_output_envelope_when_using_real_companion() -> None:
    # Given: the default native invoke response model forwarded through the bridge.
    app = LangGraphApp()
    app.register(graph=_OrdinaryGraph(), name="agent")
    register_with_openapi(app)

    # When: registration metadata is compiled into an OpenAPI document.
    spec = azure_functions_openapi.generate_openapi_spec()

    # Then: the emitted successful response references a required output envelope.
    responses = spec["paths"]["/api/graphs/agent/invoke"]["post"]["responses"]
    assert set(responses) == {"200"}
    content = responses["200"]["content"]
    assert set(content) == {"application/json"}
    reference = content["application/json"]["schema"]["$ref"]
    assert reference.startswith("#/components/schemas/")
    schema = spec["components"]["schemas"][reference.removeprefix("#/components/schemas/")]
    assert schema["required"] == ["output"]
    assert schema["properties"]["output"]["type"] == "object"
