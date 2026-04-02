"""Tests for public API surface."""

from __future__ import annotations


def test_version_string() -> None:
    from azure_functions_langgraph import __version__

    assert __version__ == "0.1.0a0"


def test_langgraph_app_importable() -> None:
    from azure_functions_langgraph import LangGraphApp

    assert LangGraphApp is not None


def test_all_exports() -> None:
    import azure_functions_langgraph

    assert "LangGraphApp" in azure_functions_langgraph.__all__
    assert "__version__" in azure_functions_langgraph.__all__


def test_contracts_importable() -> None:
    from azure_functions_langgraph.contracts import (
        ErrorResponse,
        GraphInfo,
        HealthResponse,
        InvokeRequest,
        InvokeResponse,
        StreamRequest,
    )

    assert InvokeRequest is not None
    assert InvokeResponse is not None
    assert StreamRequest is not None
    assert HealthResponse is not None
    assert GraphInfo is not None
    assert ErrorResponse is not None


def test_invalid_attr_raises() -> None:
    import pytest

    import azure_functions_langgraph

    with pytest.raises(AttributeError, match="no attribute"):
        _ = azure_functions_langgraph.NonExistent
