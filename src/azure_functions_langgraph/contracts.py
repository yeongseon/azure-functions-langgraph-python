"""Pydantic contracts for request/response models."""

from __future__ import annotations

from typing import Any, Optional

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
