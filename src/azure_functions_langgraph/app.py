"""LangGraphApp — Deploy LangGraph compiled graphs as Azure Functions HTTP endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any, Optional

import azure.functions as func

from azure_functions_langgraph import __version__
from azure_functions_langgraph.contracts import (
    ErrorResponse,
    GraphInfo,
    HealthResponse,
    InvokeRequest,
    InvokeResponse,
    StateResponse,
    StreamRequest,
)
from azure_functions_langgraph.protocols import InvocableGraph, StatefulGraph, StreamableGraph

logger = logging.getLogger(__name__)


@dataclass
class _GraphRegistration:
    """Internal registration record for a compiled graph."""

    graph: InvocableGraph
    name: str
    description: Optional[str] = None
    stream_enabled: bool = True
    auth_level: Optional[func.AuthLevel] = None

@dataclass
class LangGraphApp:
    """Wraps LangGraph compiled graphs into Azure Functions HTTP endpoints.

    Usage::

        from azure_functions_langgraph import LangGraphApp

        app = LangGraphApp()
        app.register(graph=compiled_graph, name="my_agent")
        func_app = app.function_app

    This auto-registers:

    - ``POST /api/graphs/{name}/invoke`` — synchronous invocation
    - ``POST /api/graphs/{name}/stream`` — buffered SSE response (not true streaming)
    - ``GET /api/health`` — health check with registered graph list
    - ``GET /api/graphs/{name}/threads/{thread_id}/state`` — thread state (StatefulGraph only)

    Note:
        The graph argument must satisfy the :class:`LangGraphLike` protocol
        (i.e. have ``.invoke()`` and ``.stream()`` methods). This avoids a
        hard import dependency on ``langgraph`` at the library level.

    Note:
        v0.1 streams are **buffered** — all chunks are collected and returned
        in a single SSE-formatted HTTP response. True streaming (chunked
        transfer encoding) is planned for a future release once Azure Functions
        Python HTTP streaming stabilises.
    """

    auth_level: func.AuthLevel = func.AuthLevel.ANONYMOUS
    max_stream_response_bytes: int = 1024 * 1024
    _registrations: dict[str, _GraphRegistration] = field(default_factory=dict)
    _function_app: Optional[func.FunctionApp] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.auth_level == func.AuthLevel.ANONYMOUS and os.environ.get(
            "AZURE_FUNCTIONS_ENVIRONMENT"
        ):
            logger.warning(
                "LangGraphApp is configured with anonymous HTTP auth. "
                "Use FUNCTION or ADMIN auth levels for production deployments."
            )

    def register(
        self,
        graph: Any,
        name: str,
        description: Optional[str] = None,
        stream: bool = True,
        auth_level: Optional[func.AuthLevel] = None,
    ) -> None:
        """Register a compiled LangGraph graph.

        Args:
            graph: Any object satisfying :class:`~protocols.LangGraphLike`
                (typically a ``CompiledStateGraph`` from ``langgraph``).
            name: Unique name for this graph (used in URL routes).
            description: Optional human-readable description.
            stream: Whether to enable the stream endpoint for this graph.
            auth_level: Override app-level auth for this graph's endpoints.
                When ``None`` (default), the app-level ``auth_level`` is used.

        Raises:
            TypeError: If *graph* does not satisfy the required protocol.
            ValueError: If *name* is already registered.
        """
        if not isinstance(graph, InvocableGraph):
            raise TypeError(f"Graph must have an invoke() method. Got {type(graph).__name__}")
        if name in self._registrations:
            raise ValueError(f"Graph {name!r} is already registered")
        self._registrations[name] = _GraphRegistration(
            graph=graph,
            name=name,
            description=description,
            stream_enabled=stream,
            auth_level=auth_level,
        )
        # Reset cached function app so routes are re-generated
        self._function_app = None

    @property
    def function_app(self) -> func.FunctionApp:
        """Return an ``azure.functions.FunctionApp`` with all routes registered."""
        if self._function_app is None:
            self._function_app = self._build_function_app()
        return self._function_app

    # ------------------------------------------------------------------
    # Internal route builders
    # ------------------------------------------------------------------

    def _build_function_app(self) -> func.FunctionApp:
        app = func.FunctionApp(http_auth_level=self.auth_level)

        # Health endpoint
        @app.function_name(name="aflg_health")
        @app.route(route="health", methods=["GET"], auth_level=self.auth_level)
        def health(req: func.HttpRequest) -> func.HttpResponse:
            graphs = [
                GraphInfo(
                    name=reg.name,
                    description=reg.description,
                    has_checkpointer=_has_checkpointer(reg.graph),
                )
                for reg in self._registrations.values()
            ]
            body = HealthResponse(graphs=graphs)
            return func.HttpResponse(
                body=body.model_dump_json(),
                mimetype="application/json",
                status_code=200,
            )

        # OpenAPI endpoint
        @app.function_name(name="aflg_openapi")
        @app.route(route="openapi.json", methods=["GET"], auth_level=self.auth_level)
        def openapi(req: func.HttpRequest) -> func.HttpResponse:
            _ = req
            return func.HttpResponse(
                body=json.dumps(self._build_openapi(), default=str),
                mimetype="application/json",
                status_code=200,
            )

        # Per-graph endpoints
        for reg in self._registrations.values():
            self._register_invoke_route(app, reg)
            self._register_stream_route(app, reg)
            if isinstance(reg.graph, StatefulGraph):
                self._register_state_route(app, reg)

        return app

    def _register_invoke_route(self, app: func.FunctionApp, reg: _GraphRegistration) -> None:
        route = f"graphs/{reg.name}/invoke"
        fn_name = f"aflg_{reg.name}_invoke"
        captured_reg = reg
        effective_auth = self._effective_auth_level(reg)

        @app.function_name(name=fn_name)
        @app.route(route=route, methods=["POST"], auth_level=effective_auth)
        def invoke_handler(req: func.HttpRequest) -> func.HttpResponse:
            return self._handle_invoke(req, captured_reg)

    def _register_stream_route(self, app: func.FunctionApp, reg: _GraphRegistration) -> None:
        route = f"graphs/{reg.name}/stream"
        fn_name = f"aflg_{reg.name}_stream"
        captured_reg = reg
        effective_auth = self._effective_auth_level(reg)

        @app.function_name(name=fn_name)
        @app.route(route=route, methods=["POST"], auth_level=effective_auth)
        def stream_handler(req: func.HttpRequest) -> func.HttpResponse:
            return self._handle_stream(req, captured_reg)


    def _register_state_route(self, app: func.FunctionApp, reg: _GraphRegistration) -> None:
        route = f"graphs/{reg.name}/threads/{{thread_id}}/state"
        fn_name = f"aflg_{reg.name}_state"
        captured_reg = reg
        effective_auth = self._effective_auth_level(reg)

        @app.function_name(name=fn_name)
        @app.route(route=route, methods=["GET"], auth_level=effective_auth)
        def state_handler(req: func.HttpRequest) -> func.HttpResponse:
            return self._handle_state(req, captured_reg)

    def _effective_auth_level(self, reg: _GraphRegistration) -> func.AuthLevel:
        """Return per-graph auth if set, otherwise app-level auth."""
        if reg.auth_level is not None:
            return reg.auth_level
        return self.auth_level

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    def _handle_invoke(self, req: func.HttpRequest, reg: _GraphRegistration) -> func.HttpResponse:
        """Handle a synchronous invoke request."""
        try:
            body = req.get_json()
        except ValueError:
            return _error_response(400, "Invalid JSON body")

        try:
            request = InvokeRequest.model_validate(body)
        except Exception as exc:
            return _error_response(422, f"Validation error: {exc}")

        config = request.config or {}
        try:
            result = reg.graph.invoke(request.input, config=config)
        except Exception as exc:
            logger.exception("Graph %s invoke failed", reg.name)
            _ = exc
            return _error_response(500, "Graph execution failed")

        output = result if isinstance(result, dict) else {"result": result}
        response = InvokeResponse(output=output)
        return func.HttpResponse(
            body=response.model_dump_json(),
            mimetype="application/json",
            status_code=200,
        )

    def _handle_stream(self, req: func.HttpRequest, reg: _GraphRegistration) -> func.HttpResponse:
        """Handle a streaming request.

        Returns a **buffered** SSE-formatted response.  All stream chunks are
        collected first, then returned in a single HTTP response.  This is a
        known v0.1 limitation — true chunked streaming will follow once Azure
        Functions Python HTTP streaming is fully stable.
        """
        if not reg.stream_enabled:
            return _error_response(501, f"Graph {reg.name!r} is configured as invoke-only")

        if not isinstance(reg.graph, StreamableGraph):
            return _error_response(501, f"Graph {reg.name!r} does not support streaming")

        try:
            body = req.get_json()
        except ValueError:
            return _error_response(400, "Invalid JSON body")

        try:
            request = StreamRequest.model_validate(body)
        except Exception as exc:
            return _error_response(422, f"Validation error: {exc}")

        config = request.config or {}
        chunks: list[str] = []
        buffered_bytes = 0

        def _append_chunk(chunk: str) -> bool:
            nonlocal buffered_bytes
            chunk_bytes = len(chunk.encode())
            if buffered_bytes + chunk_bytes > self.max_stream_response_bytes:
                error_payload = json.dumps(
                    {
                        "error": (
                            "stream response exceeded max buffered size "
                            f"({self.max_stream_response_bytes} bytes)"
                        )
                    }
                )
                chunks.append(f"event: error\ndata: {error_payload}\n\n")
                return False
            chunks.append(chunk)
            buffered_bytes += chunk_bytes
            return True

        try:
            for event in reg.graph.stream(
                request.input,
                config=config,
                stream_mode=request.stream_mode,
            ):
                serialized = json.dumps(
                    event if isinstance(event, dict) else {"data": str(event)},
                    default=str,
                )
                if not _append_chunk(f"event: data\ndata: {serialized}\n\n"):
                    break
        except Exception as exc:
            logger.exception("Graph %s stream failed", reg.name)
            _ = exc
            error_payload = json.dumps({"error": "stream processing failed"})
            _append_chunk(f"event: error\ndata: {error_payload}\n\n")

        _append_chunk("event: end\ndata: {}\n\n")

        return func.HttpResponse(
            body="".join(chunks),
            mimetype="text/event-stream",
            status_code=200,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )


    def _handle_state(
        self, req: func.HttpRequest, reg: _GraphRegistration
    ) -> func.HttpResponse:
        """Handle a GET request for thread state."""
        if not isinstance(reg.graph, StatefulGraph):
            return _error_response(
                409, f"Graph {reg.name!r} does not support state retrieval"
            )

        thread_id = req.route_params.get("thread_id")
        if not thread_id:
            return _error_response(400, "Missing thread_id in URL path")

        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

        try:
            snapshot = reg.graph.get_state(config)
        except Exception as exc:
            logger.exception(
                "Graph %s get_state failed for thread %s", reg.name, thread_id
            )
            _ = exc
            return _error_response(404, f"Thread {thread_id!r} not found")

        values = snapshot.values if isinstance(snapshot.values, dict) else {}
        next_nodes: list[str] = list(snapshot.next) if hasattr(snapshot, "next") else []
        metadata = (
            dict(snapshot.metadata) if hasattr(snapshot, "metadata") and snapshot.metadata else None
        )

        response = StateResponse(values=values, next=next_nodes, metadata=metadata)
        return func.HttpResponse(
            body=json.dumps(response.model_dump(), default=str),
            mimetype="application/json",
            status_code=200,
        )

    def _build_openapi(self) -> dict[str, Any]:
        """Build OpenAPI 3.0.3 specification from registered graphs."""
        paths: dict[str, Any] = {
            "/health": {
                "get": {
                    "summary": "Health check",
                    "responses": {"200": {"description": "Service is healthy"}},
                }
            }
        }

        for reg in self._registrations.values():
            paths[f"/graphs/{reg.name}/invoke"] = {
                "post": {
                    "summary": f"Invoke graph '{reg.name}'",
                    "responses": {"200": {"description": "Invocation result"}},
                }
            }
            if reg.stream_enabled:
                paths[f"/graphs/{reg.name}/stream"] = {
                    "post": {
                        "summary": f"Stream graph '{reg.name}'",
                        "responses": {"200": {"description": "SSE stream"}},
                    }
                }
            if isinstance(reg.graph, StatefulGraph):
                paths[f"/graphs/{reg.name}/threads/{{thread_id}}/state"] = {
                    "get": {
                        "summary": f"Get thread state for '{reg.name}'",
                        "parameters": [
                            {
                                "name": "thread_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {
                            "200": {"description": "Thread state"},
                            "404": {"description": "Thread not found"},
                        },
                    }
                }


        return {
            "openapi": "3.0.3",
            "info": {
                "title": "azure-functions-langgraph",
                "version": __version__,
            },
            "paths": paths,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _has_checkpointer(graph: Any) -> bool:
    """Check whether a compiled graph has a checkpointer attached."""
    return getattr(graph, "checkpointer", None) is not None


def _error_response(status_code: int, detail: str) -> func.HttpResponse:
    body = ErrorResponse(error="error", detail=detail)
    return func.HttpResponse(
        body=body.model_dump_json(),
        mimetype="application/json",
        status_code=status_code,
    )
