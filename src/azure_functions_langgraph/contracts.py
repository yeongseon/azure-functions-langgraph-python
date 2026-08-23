"""Pydantic contracts and metadata dataclasses for request/response models."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, TypeGuard

from pydantic import BaseModel, Field, create_model


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

# ------------------------------------------------------------------
# Transport-envelope model builders (issue #349)
#
# The native ``invoke``/``stream`` routes always speak the transport
# envelope on the wire: ``{"input": ..., "config": ...}`` in, ``{"output":
# ...}`` out (see ``_handlers.handle_invoke``). A user-supplied
# ``request_model``/``response_model`` describes the graph INPUT/OUTPUT
# payload — i.e. the *inner* schema — not the HTTP body. These builders wrap
# the caller's model inside the transport envelope so generated OpenAPI
# metadata matches the runtime contract instead of documenting the bare inner
# model. When no model is supplied, the generic ``dict``-typed envelope
# (``InvokeRequest``/``StreamRequest``/``InvokeResponse``) is used, which is
# still the honest wire contract.
# ------------------------------------------------------------------


def _is_model_type(model: object) -> TypeGuard[type[BaseModel]]:
    """Return ``True`` if *model* is a Pydantic ``BaseModel`` subclass."""
    return isinstance(model, type) and issubclass(model, BaseModel)


def build_invoke_request_model(input_model: Optional[type[Any]]) -> type[BaseModel]:
    """Wrap a graph-input model in the ``invoke`` transport envelope.

    Returns a model equivalent to ``{"input": <input_model>, "config"?: ...}``.
    Falls back to the generic :class:`InvokeRequest` when *input_model* is not a
    Pydantic model. ``input`` is always required, so the request body itself is
    required regardless of the inner model's field optionality.
    """
    if _is_model_type(input_model):
        return create_model(
            f"InvokeRequest_{input_model.__name__}",
            input=(input_model, Field(description="Input to the graph")),
            config=(
                Optional[dict[str, Any]],
                Field(
                    default=None,
                    description="LangGraph config, e.g. {'configurable': {'thread_id': '...'}}",
                ),
            ),
        )
    return InvokeRequest


def build_invoke_response_model(output_model: Optional[type[Any]]) -> type[BaseModel]:
    """Wrap a graph-output model in the ``invoke`` response envelope.

    Returns a model equivalent to ``{"output": <output_model>}``. Falls back to
    the generic :class:`InvokeResponse` when *output_model* is not a Pydantic
    model.
    """
    if _is_model_type(output_model):
        return create_model(
            f"InvokeResponse_{output_model.__name__}",
            output=(output_model, Field(description="Graph output state")),
        )
    return InvokeResponse


def build_stream_request_model(input_model: Optional[type[Any]]) -> type[BaseModel]:
    """Wrap a graph-input model in the ``stream`` transport envelope.

    Returns a model equivalent to
    ``{"input": <input_model>, "config"?: ..., "stream_mode"?: ...}``. Falls back
    to the generic :class:`StreamRequest` when *input_model* is not a Pydantic
    model.
    """
    if _is_model_type(input_model):
        return create_model(
            f"StreamRequest_{input_model.__name__}",
            input=(input_model, Field(description="Input to the graph")),
            config=(
                Optional[dict[str, Any]],
                Field(
                    default=None,
                    description="LangGraph config, e.g. {'configurable': {'thread_id': '...'}}",
                ),
            ),
            stream_mode=(
                str,
                Field(
                    default="values",
                    description="Stream mode: 'values', 'updates', 'messages', or 'custom'",
                ),
            ),
        )
    return StreamRequest

class GraphInfo(BaseModel):
    """Information about a registered graph."""

    name: str
    description: Optional[str] = None
    has_checkpointer: bool = False


class HealthStatus(BaseModel):
    """Minimal liveness response for the anonymous health probe.

    Intentionally free of any registered-graph inventory so an
    unauthenticated caller only learns "the process is up".
    """

    status: str = "ok"


class HealthResponse(BaseModel):
    """Detailed health response (protected ``/health/details`` surface)."""

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
    metadata: Optional[dict[str, Any]] = Field(default=None, description="State metadata")


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
    parameters: tuple[Mapping[str, Any], ...] = ()
    request_model: Optional[type[Any]] = None
    response_model: Optional[type[Any]] = None


@dataclass(frozen=True)
class RegisteredGraphMetadata:
    """Public metadata about a registered graph.

    Used by external consumers like ``azure-functions-openapi-python``.
    """

    name: str
    description: Optional[str] = None
    routes: tuple[RouteMetadata, ...] = ()


@dataclass(frozen=True)
class AppMetadata:
    """Top-level metadata snapshot for all registered routes.

    All collections are read-only.  ``graphs`` is exposed as a
    :class:`~types.MappingProxyType` so consumers cannot mutate it.
    Nested parameter dicts inside :class:`RouteMetadata` are also
    wrapped in :class:`~types.MappingProxyType` for deep immutability.
    """

    graphs: Mapping[str, RegisteredGraphMetadata] = field(
        default_factory=lambda: MappingProxyType({})
    )
    app_routes: tuple[RouteMetadata, ...] = ()
