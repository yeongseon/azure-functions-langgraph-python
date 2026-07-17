"""Azure Functions LangGraph — Deploy LangGraph agents as Azure Functions."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__version__ = "0.7.2"

if TYPE_CHECKING:
    from azure_functions_langgraph.app import LangGraphApp, get_langgraph_metadata
    from azure_functions_langgraph.contracts import (
        AppMetadata,
        ErrorResponse,
        GraphInfo,
        HealthResponse,
        InvokeRequest,
        InvokeResponse,
        RegisteredGraphMetadata,
        RouteMetadata,
        StateResponse,
        StreamRequest,
    )
    from azure_functions_langgraph.protocols import (
        CloneableGraph,
        InvocableGraph,
        LangGraphLike,
        StatefulGraph,
        StreamableGraph,
    )


# Maps a public attribute name to the (module_path, attribute) it lazily imports.
# Keeps ``import azure_functions_langgraph`` cheap: heavy deps (azure-functions,
# langgraph) are only imported when the corresponding symbol is first accessed.
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "LangGraphApp": ("azure_functions_langgraph.app", "LangGraphApp"),
    "get_langgraph_metadata": ("azure_functions_langgraph.app", "get_langgraph_metadata"),
    # Contracts
    "InvokeRequest": ("azure_functions_langgraph.contracts", "InvokeRequest"),
    "InvokeResponse": ("azure_functions_langgraph.contracts", "InvokeResponse"),
    "StreamRequest": ("azure_functions_langgraph.contracts", "StreamRequest"),
    "HealthResponse": ("azure_functions_langgraph.contracts", "HealthResponse"),
    "GraphInfo": ("azure_functions_langgraph.contracts", "GraphInfo"),
    "ErrorResponse": ("azure_functions_langgraph.contracts", "ErrorResponse"),
    "StateResponse": ("azure_functions_langgraph.contracts", "StateResponse"),
    # Metadata dataclasses
    "AppMetadata": ("azure_functions_langgraph.contracts", "AppMetadata"),
    "RegisteredGraphMetadata": ("azure_functions_langgraph.contracts", "RegisteredGraphMetadata"),
    "RouteMetadata": ("azure_functions_langgraph.contracts", "RouteMetadata"),
    # Protocols
    "InvocableGraph": ("azure_functions_langgraph.protocols", "InvocableGraph"),
    "StreamableGraph": ("azure_functions_langgraph.protocols", "StreamableGraph"),
    "LangGraphLike": ("azure_functions_langgraph.protocols", "LangGraphLike"),
    "StatefulGraph": ("azure_functions_langgraph.protocols", "StatefulGraph"),
    "CloneableGraph": ("azure_functions_langgraph.protocols", "CloneableGraph"),
}

# Symbols whose import failure means the optional runtime deps are missing;
# surface a friendly install hint instead of the raw ImportError.
_REQUIRES_RUNTIME_DEPS = frozenset({"LangGraphApp"})


def __getattr__(name: str) -> object:
    try:
        module_path, attr = _LAZY_IMPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        if name in _REQUIRES_RUNTIME_DEPS:
            raise ImportError(
                "LangGraphApp requires 'azure-functions' and 'langgraph'. "
                "Install them with: pip install azure-functions-langgraph"
            ) from exc
        raise
    return getattr(module, attr)


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
    # Toolkit metadata
    "get_langgraph_metadata",
    "AppMetadata",
    "RegisteredGraphMetadata",
    "RouteMetadata",
    # Protocols
    "InvocableGraph",
    "StreamableGraph",
    "LangGraphLike",
    "StatefulGraph",
    "CloneableGraph",
]
