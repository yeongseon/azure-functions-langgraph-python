"""Pydantic contracts and metadata dataclasses for request/response models."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

from pydantic import BaseModel, Field


class InvokeRequest(BaseModel):
    """Request body for graph invocation."""

    input: dict[str, Any] = Field(description="Input to the graph")
    config: Optional[dict[str, Any]] = Field(
        default=None,
        description="LangGraph config, e.g. {'configurable': {'thread_id': '...'}}",
    )


class StreamRequest(BaseModel):
    """Request body for graph streaming."""

    input: dict[str, Any] = Field(description="Input to the graph")
    config: Optional[dict[str, Any]] = Field(
        default=None,
        description="LangGraph config, e.g. {'configurable': {'thread_id': '...'}}",
    )
    stream_mode: str = Field(
        default="values",
        description="Stream mode: 'values', 'updates', 'messages', or 'custom'",
    )


class InvokeResponse(BaseModel):
    """Response body for graph invocation."""

    output: dict[str, Any] = Field(description="Graph output state")


class GraphInfo(BaseModel):
    """Information about a registered graph."""

    name: str
    description: Optional[str] = None
    has_checkpointer: bool = False


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    graphs: list[GraphInfo] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Error response body."""

    error: str
    detail: Optional[str] = None


class StateResponse(BaseModel):
    """Response body for thread state retrieval."""

    values: dict[str, Any] = Field(description="Current state values")
    next: list[str] = Field(default_factory=list, description="Next node(s) to execute")
    metadata: Optional[dict[str, Any]] = Field(
        default=None, description="State metadata"
    )


# ------------------------------------------------------------------
# Metadata dataclasses (stdlib only — no Pydantic dependency)
# ------------------------------------------------------------------


@dataclass(frozen=True)
class RouteMetadata:
    """Metadata for a single HTTP route."""

    path: str
    method: str
    summary: str = ""
    description: str = ""
    parameters: tuple[dict[str, Any], ...] = ()
    request_model: Optional[type[Any]] = None
    response_model: Optional[type[Any]] = None


@dataclass(frozen=True)
class RegisteredGraphMetadata:
    """Public metadata about a registered graph.

    Used by external consumers like ``azure-functions-openapi``.
    """

    name: str
    description: Optional[str] = None
    routes: tuple[RouteMetadata, ...] = ()


@dataclass(frozen=True)
class AppMetadata:
    """Top-level metadata snapshot for all registered routes.

    All collections are read-only.  ``graphs`` is exposed as a
    :class:`~types.MappingProxyType` so consumers cannot mutate it.
    """

    graphs: Mapping[str, RegisteredGraphMetadata] = field(
        default_factory=lambda: MappingProxyType({})
    )
    app_routes: tuple[RouteMetadata, ...] = ()
