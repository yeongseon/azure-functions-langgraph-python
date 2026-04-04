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
    # Contracts
    assert "InvokeRequest" in azure_functions_langgraph.__all__
    assert "InvokeResponse" in azure_functions_langgraph.__all__
    assert "StreamRequest" in azure_functions_langgraph.__all__
    assert "HealthResponse" in azure_functions_langgraph.__all__
    assert "GraphInfo" in azure_functions_langgraph.__all__
    assert "ErrorResponse" in azure_functions_langgraph.__all__
    assert "StateResponse" in azure_functions_langgraph.__all__
    # Protocols
    assert "InvocableGraph" in azure_functions_langgraph.__all__
    assert "StreamableGraph" in azure_functions_langgraph.__all__
    assert "LangGraphLike" in azure_functions_langgraph.__all__
    assert "StatefulGraph" in azure_functions_langgraph.__all__


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


def test_all_contracts_importable() -> None:
    from azure_functions_langgraph import (
        ErrorResponse,
        GraphInfo,
        HealthResponse,
        InvokeRequest,
        InvokeResponse,
        StateResponse,
        StreamRequest,
    )

    assert InvokeRequest is not None
    assert InvokeResponse is not None
    assert StreamRequest is not None
    assert HealthResponse is not None
    assert GraphInfo is not None
    assert ErrorResponse is not None
    assert StateResponse is not None


def test_all_protocols_importable() -> None:
    from azure_functions_langgraph import (
        InvocableGraph,
        LangGraphLike,
        StatefulGraph,
        StreamableGraph,
    )

    assert InvocableGraph is not None
    assert StreamableGraph is not None
    assert LangGraphLike is not None
    assert StatefulGraph is not None


def test_invalid_attr_raises() -> None:
    import pytest

    import azure_functions_langgraph

    with pytest.raises(AttributeError, match="no attribute"):
        _ = azure_functions_langgraph.NonExistent
