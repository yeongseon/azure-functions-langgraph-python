"""Generated rich metadata through the optional real OpenAPI companion."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.openapi import register_with_openapi
from tests.conftest import FakeCompiledGraph

azure_functions_openapi = pytest.importorskip(
    "azure_functions_openapi",
    reason='requires the optional companion: pip install "azure-functions-langgraph[openapi]"',
)


@pytest.fixture(autouse=True)
def _isolated_openapi_registry() -> Iterator[None]:
    from azure_functions_openapi.decorator import clear_openapi_registry

    clear_openapi_registry()
    yield
    clear_openapi_registry()


@pytest.mark.parametrize("description", [None, "Catalog graph documentation"])
def test_generated_graph_metadata_when_registered_through_bridge(description: str | None) -> None:
    # Given: a graph description distinct from the generated invoke summary.
    app = LangGraphApp()
    app.register(graph=FakeCompiledGraph(), name="catalog", description=description)
    register_with_openapi(app)

    # When: the real companion generates a bridge-only document, without app scanning.
    spec = azure_functions_openapi.generate_openapi_spec()

    # Then: the graph path/method and rich fields survive, including description fallback.
    operations = spec["paths"]["/api/graphs/catalog/invoke"]
    assert set(operations) == {"post"}
    operation = operations["post"]
    assert operation["summary"] == "Invoke graph 'catalog'"
    assert operation["description"] == (description or "")
    assert operation["tags"] == ["catalog"]


def test_generated_health_metadata_when_registered_through_bridge() -> None:
    # Given: app-level health metadata alongside a graph with a nonempty description.
    app = LangGraphApp()
    app.register(
        graph=FakeCompiledGraph(), name="catalog", description="Catalog graph documentation"
    )
    register_with_openapi(app)

    # When: the real companion generates the same bridge-only document.
    spec = azure_functions_openapi.generate_openapi_spec()

    # Then: health keeps its own GET metadata, not the graph's description or tag.
    operations = spec["paths"]["/api/health"]
    assert set(operations) == {"get"}
    operation = operations["get"]
    assert operation["summary"] == "Health check"
    assert operation["description"] == ""
    assert operation["tags"] == ["system"]
