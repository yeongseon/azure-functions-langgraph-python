"""Azure Functions LangGraph — Deploy LangGraph agents as Azure Functions."""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.2.0"

if TYPE_CHECKING:
    from azure_functions_langgraph.app import LangGraphApp
    from azure_functions_langgraph.contracts import (
        ErrorResponse,
        GraphInfo,
        HealthResponse,
        InvokeRequest,
        InvokeResponse,
        StateResponse,
        StreamRequest,
    )
    from azure_functions_langgraph.protocols import (
        InvocableGraph,
        LangGraphLike,
        StatefulGraph,
        StreamableGraph,
    )


def __getattr__(name: str) -> object:
    if name == "LangGraphApp":
        try:
            from azure_functions_langgraph.app import LangGraphApp

            return LangGraphApp
        except ImportError as exc:
            raise ImportError(
                "LangGraphApp requires 'azure-functions' and 'langgraph'. "
                "Install them with: pip install azure-functions-langgraph"
            ) from exc
    # Contracts
    if name == "InvokeRequest":
        from azure_functions_langgraph.contracts import InvokeRequest

        return InvokeRequest
    if name == "InvokeResponse":
        from azure_functions_langgraph.contracts import InvokeResponse

        return InvokeResponse
    if name == "StreamRequest":
        from azure_functions_langgraph.contracts import StreamRequest

        return StreamRequest
    if name == "HealthResponse":
        from azure_functions_langgraph.contracts import HealthResponse

        return HealthResponse
    if name == "GraphInfo":
        from azure_functions_langgraph.contracts import GraphInfo

        return GraphInfo
    if name == "ErrorResponse":
        from azure_functions_langgraph.contracts import ErrorResponse

        return ErrorResponse
    if name == "StateResponse":
        from azure_functions_langgraph.contracts import StateResponse

        return StateResponse
    # Protocols
    if name == "InvocableGraph":
        from azure_functions_langgraph.protocols import InvocableGraph

        return InvocableGraph
    if name == "StreamableGraph":
        from azure_functions_langgraph.protocols import StreamableGraph

        return StreamableGraph
    if name == "LangGraphLike":
        from azure_functions_langgraph.protocols import LangGraphLike

        return LangGraphLike
    if name == "StatefulGraph":
        from azure_functions_langgraph.protocols import StatefulGraph

        return StatefulGraph
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "LangGraphApp",
    "__version__",
    # Contracts
    "InvokeRequest",
    "InvokeResponse",
    "StreamRequest",
    "HealthResponse",
    "GraphInfo",
    "ErrorResponse",
    "StateResponse",
    # Protocols
    "InvocableGraph",
    "StreamableGraph",
    "LangGraphLike",
    "StatefulGraph",
]
