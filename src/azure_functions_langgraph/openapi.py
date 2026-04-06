"""Bridge between azure-functions-langgraph and azure-functions-openapi.

This module forwards route metadata from :class:`LangGraphApp` to the
``azure-functions-openapi`` package for OpenAPI spec generation.

Usage::

    from azure_functions_langgraph import LangGraphApp
    from azure_functions_langgraph.openapi import register_with_openapi

    app = LangGraphApp()
    app.register(graph=compiled_graph, name="agent")
    register_with_openapi(app)

.. versionadded:: 0.5.0
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from azure_functions_langgraph.app import LangGraphApp


def register_with_openapi(app: LangGraphApp) -> int:
    """Register all graph and app-level endpoints with azure-functions-openapi.

    Reads the metadata exposed by :meth:`LangGraphApp.get_app_metadata` and
    calls :func:`azure_functions_openapi.register_openapi_metadata` for each
    route.

    Args:
        app: A :class:`LangGraphApp` instance with graphs already registered.

    Returns:
        Number of routes registered with the openapi package.

    Raises:
        ImportError: If ``azure-functions-openapi`` is not installed.
        TypeError: If a ``request_model`` or ``response_model`` is not a
            Pydantic ``BaseModel`` subclass.
    """
    try:
        from azure_functions_openapi import register_openapi_metadata
    except ImportError as exc:
        raise ImportError(
            "azure-functions-openapi is required for OpenAPI integration. "
            "Install it with: pip install azure-functions-openapi"
        ) from exc

    metadata = app.get_app_metadata()
    count = 0

    # Per-graph routes
    for graph_meta in metadata.graphs.values():
        for route in graph_meta.routes:
            request_body = None
            if route.request_model is not None:
                request_body = _build_request_body(route.request_model)

            response_model = route.response_model
            if response_model is not None:
                _validate_model(response_model, "response_model")
            register_openapi_metadata(
                path=route.path,
                method=route.method,
                summary=route.summary,
                description=route.description or graph_meta.description or "",
                tags=[graph_meta.name],
                request_body=request_body,
                response_model=response_model,
                parameters=list(route.parameters) if route.parameters else None,
            )
            count += 1

    # App-level routes (e.g. /health)
    for route in metadata.app_routes:
        register_openapi_metadata(
            path=route.path,
            method=route.method,
            summary=route.summary,
            description=route.description,
            tags=["system"],
            parameters=list(route.parameters) if route.parameters else None,
        )
        count += 1

    return count


def _validate_model(model: object, label: str) -> None:
    """Raise :class:`TypeError` if *model* is not a Pydantic ``BaseModel`` subclass."""
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise TypeError(
            f"{label} must be a Pydantic BaseModel subclass, got {model!r}"
        )


def _build_request_body(model: type[Any]) -> dict[str, Any]:
    """Build OpenAPI request body dict from a Pydantic model.

    Raises:
        TypeError: If *model* is not a Pydantic ``BaseModel`` subclass.
    """
    _validate_model(model, "request_model")

    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": model.model_json_schema()
            }
        },
    }
