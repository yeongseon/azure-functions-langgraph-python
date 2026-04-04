"""LangGraphApp — Deploy LangGraph compiled graphs as Azure Functions HTTP endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any, Optional

import azure.functions as func

from azure_functions_langgraph import __version__
from azure_functions_langgraph._handlers import (
    handle_invoke,
    handle_state,
    handle_stream,
)
from azure_functions_langgraph._validation import (
    validate_graph_name,
)
from azure_functions_langgraph.contracts import (
    GraphInfo,
    HealthResponse,
)
from azure_functions_langgraph.protocols import InvocableGraph, StatefulGraph

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
    max_request_body_bytes: int = 1024 * 1024
    max_input_depth: int = 32
    max_input_nodes: int = 10_000
    platform_compat: bool = False
    _registrations: dict[str, _GraphRegistration] = field(default_factory=dict)
    _function_app: Optional[func.FunctionApp] = field(default=None, init=False, repr=False)
    _thread_store: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.auth_level == func.AuthLevel.ANONYMOUS and os.environ.get(
            "AZURE_FUNCTIONS_ENVIRONMENT"
        ):
            logger.warning(
                "LangGraphApp is configured with anonymous HTTP auth. "
                "Use FUNCTION or ADMIN auth levels for production deployments."
            )
        if self.platform_compat and self._thread_store is None:
            from azure_functions_langgraph.platform.stores import InMemoryThreadStore

            self._thread_store = InMemoryThreadStore()

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
            ValueError: If *name* is already registered or invalid.
        """
        if not isinstance(graph, InvocableGraph):
            raise TypeError(f"Graph must have an invoke() method. Got {type(graph).__name__}")
        name_err = validate_graph_name(name)
        if name_err:
            raise ValueError(name_err)
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

    @property
    def thread_store(self) -> Any:
        """Return the thread store, or ``None`` if platform compat is disabled."""
        return self._thread_store

    @thread_store.setter
    def thread_store(self, store: Any) -> None:
        """Set a custom thread store implementation.

        Must be called before accessing :attr:`function_app`.
        """
        self._thread_store = store
        self._function_app = None  # invalidate cached routes

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

        # Platform API compatibility routes
        if self.platform_compat:
            from azure_functions_langgraph.platform.routes import (
                PlatformRouteDeps,
                register_platform_routes,
            )

            deps = PlatformRouteDeps(
                registrations=self._registrations,
                thread_store=self._thread_store,
                auth_level=self.auth_level,
                max_stream_response_bytes=self.max_stream_response_bytes,
                max_request_body_bytes=self.max_request_body_bytes,
                max_input_depth=self.max_input_depth,
                max_input_nodes=self.max_input_nodes,
            )
            register_platform_routes(app, deps)

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
    # Request handlers (thin delegation to _handlers module)
    # ------------------------------------------------------------------

    def _handle_invoke(self, req: func.HttpRequest, reg: _GraphRegistration) -> func.HttpResponse:
        """Handle a synchronous invoke request."""
        return handle_invoke(
            req,
            reg,
            max_request_body_bytes=self.max_request_body_bytes,
            max_input_depth=self.max_input_depth,
            max_input_nodes=self.max_input_nodes,
        )

    def _handle_stream(self, req: func.HttpRequest, reg: _GraphRegistration) -> func.HttpResponse:
        """Handle a streaming request."""
        return handle_stream(
            req,
            reg,
            max_stream_response_bytes=self.max_stream_response_bytes,
            max_request_body_bytes=self.max_request_body_bytes,
            max_input_depth=self.max_input_depth,
            max_input_nodes=self.max_input_nodes,
        )

    def _handle_state(
        self, req: func.HttpRequest, reg: _GraphRegistration
    ) -> func.HttpResponse:
        """Handle a GET request for thread state."""
        return handle_state(
            req,
            reg,
        )

    # ------------------------------------------------------------------
    # OpenAPI
    # ------------------------------------------------------------------

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
