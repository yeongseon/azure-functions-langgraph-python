"""Tests for public API surface."""

from __future__ import annotations


def test_version_matches_distribution_metadata() -> None:
    from importlib.metadata import version

    from azure_functions_langgraph import __version__

    assert __version__ == version("azure-functions-langgraph")


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
    # Metadata
    assert "get_langgraph_metadata" in azure_functions_langgraph.__all__
    assert "AppMetadata" in azure_functions_langgraph.__all__
    assert "RegisteredGraphMetadata" in azure_functions_langgraph.__all__
    assert "RouteMetadata" in azure_functions_langgraph.__all__
    # Protocols
    assert "InvocableGraph" in azure_functions_langgraph.__all__
    assert "StreamableGraph" in azure_functions_langgraph.__all__
    assert "AsyncInvocableGraph" in azure_functions_langgraph.__all__
    assert "AsyncStreamableGraph" in azure_functions_langgraph.__all__
    assert "LangGraphLike" in azure_functions_langgraph.__all__
    assert "StatefulGraph" in azure_functions_langgraph.__all__
    assert "CloneableGraph" in azure_functions_langgraph.__all__
    # Observability
    assert "RunObserver" in azure_functions_langgraph.__all__
    assert "RunContext" in azure_functions_langgraph.__all__
    assert "NoOpRunObserver" in azure_functions_langgraph.__all__
    assert "RunRejectedReason" in azure_functions_langgraph.__all__
    assert "LoggingRunObserver" in azure_functions_langgraph.__all__
    assert "OTelRunObserver" in azure_functions_langgraph.__all__
    assert "RunTransport" in azure_functions_langgraph.__all__

    # Service Bus trigger public surface
    assert "default_message_mapper" in azure_functions_langgraph.__all__
    assert "ServiceBusMessageLike" in azure_functions_langgraph.__all__
    assert "ThreadContentionError" in azure_functions_langgraph.__all__

    # True streaming transport public surface
    assert "StreamingLangGraphApp" in azure_functions_langgraph.__all__


def test_streaming_app_importable() -> None:
    from azure_functions_langgraph import StreamingLangGraphApp

    assert StreamingLangGraphApp.__name__ == "StreamingLangGraphApp"


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
        AsyncInvocableGraph,
        AsyncStreamableGraph,
        CloneableGraph,
        InvocableGraph,
        LangGraphLike,
        StatefulGraph,
        StreamableGraph,
    )

    assert InvocableGraph is not None
    assert StreamableGraph is not None
    assert LangGraphLike is not None
    assert StatefulGraph is not None
    assert CloneableGraph is not None
    assert AsyncInvocableGraph is not None
    assert AsyncStreamableGraph is not None


def test_invalid_attr_raises() -> None:
    import pytest

    import azure_functions_langgraph

    with pytest.raises(AttributeError, match="no attribute"):
        _ = azure_functions_langgraph.NonExistent


def test_metadata_contracts_importable() -> None:
    from azure_functions_langgraph.contracts import (
        AppMetadata,
        RegisteredGraphMetadata,
        RouteMetadata,
    )

    assert AppMetadata is not None
    assert RegisteredGraphMetadata is not None
    assert RouteMetadata is not None


def test_metadata_contracts_importable_from_package() -> None:
    from azure_functions_langgraph import (
        AppMetadata,
        RegisteredGraphMetadata,
        RouteMetadata,
    )

    assert AppMetadata is not None
    assert RegisteredGraphMetadata is not None
    assert RouteMetadata is not None


def test_openapi_bridge_importable() -> None:
    from azure_functions_langgraph.openapi import register_with_openapi

    assert register_with_openapi is not None


def test_azure_table_thread_store_from_table_client_factory() -> None:
    from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

    assert hasattr(AzureTableThreadStore, "from_table_client")
    assert callable(AzureTableThreadStore.from_table_client)
    assert hasattr(AzureTableThreadStore, "from_connection_string")
    assert callable(AzureTableThreadStore.from_connection_string)


def test_observability_importable_from_package() -> None:
    from azure_functions_langgraph import (
        LoggingRunObserver,
        NoOpRunObserver,
        RunContext,
        RunObserver,
        RunRejectedReason,
    )

    assert RunObserver is not None
    assert RunContext is not None
    assert NoOpRunObserver is not None
    assert RunRejectedReason is not None
    assert LoggingRunObserver is not None
    # LoggingRunObserver satisfies the RunObserver protocol.
    assert isinstance(LoggingRunObserver(), RunObserver)
    # NoOpRunObserver satisfies the RunObserver protocol.
    assert isinstance(NoOpRunObserver(), RunObserver)
