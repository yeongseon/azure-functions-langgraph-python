"""LangGraphApp — Deploy LangGraph compiled graphs as Azure Functions HTTP endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from types import MappingProxyType
from typing import Any, Callable, Optional
import warnings

import azure.functions as func

from azure_functions_langgraph._handlers import (
    handle_invoke,
    handle_state,
    handle_stream,
)
from azure_functions_langgraph._validation import (
    validate_graph_name,
)
from azure_functions_langgraph.contracts import (
    AppMetadata,
    GraphInfo,
    HealthResponse,
    RegisteredGraphMetadata,
    RouteMetadata,
)
from azure_functions_langgraph.locks import InProcessThreadLock, ThreadLock
from azure_functions_langgraph.protocols import InvocableGraph, StatefulGraph

# Route path templates (single source of truth for both function_app and metadata)
_ROUTE_PREFIX = "/api"
_ROUTE_HEALTH = "health"
_ROUTE_INVOKE = "graphs/{name}/invoke"
_ROUTE_STREAM = "graphs/{name}/stream"
_ROUTE_STATE = "graphs/{name}/threads/{{thread_id}}/state"
_TOOLKIT_META_ATTR = "_azure_functions_metadata"


def _merge_toolkit_metadata(
    fn: Callable[..., Any],
    namespace: str,
    payload: dict[str, Any],
) -> None:
    """Merge toolkit metadata into the convention attribute, preserving other namespaces."""
    existing: dict[str, Any] = getattr(fn, _TOOLKIT_META_ATTR, {})
    if not isinstance(existing, dict):
        existing = {}
    existing = {**existing, namespace: payload}
    setattr(fn, _TOOLKIT_META_ATTR, existing)


@dataclass
class _GraphRegistration:
    """Internal registration record for a compiled graph."""

    graph: InvocableGraph
    name: str
    description: Optional[str] = None
    stream_enabled: bool = True
    auth_level: Optional[func.AuthLevel] = None
    request_model: Optional[type[Any]] = None
    response_model: Optional[type[Any]] = None


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

    Note:
        The default ``auth_level`` is :attr:`~azure.functions.AuthLevel.FUNCTION`,
        so deployed endpoints require a function key by default. Pass
        ``auth_level=func.AuthLevel.ANONYMOUS`` explicitly for public access
        (e.g. local development); doing so emits an unconditional ``UserWarning``.
        The ``health_auth_level`` parameter controls the auth level of the health
        endpoint independently and defaults to :attr:`~azure.functions.AuthLevel.ANONYMOUS`,
        which is the conventional choice for liveness/readiness probes.

    Note:
        Per-thread locking on the native invoke/stream endpoints is
        pluggable via :attr:`thread_lock`. The default,
        :class:`~azure_functions_langgraph.locks.inprocess.InProcessThreadLock`,
        is **in-process only** — it is not distributed across Function App
        instances, worker processes, or hosts. For multi-instance production
        deployments, supply a distributed backend such as
        :class:`~azure_functions_langgraph.locks.azure_blob.AzureBlobLeaseThreadLock`,
        or (for platform-compat runs) enable ``platform_compat=True`` with
        :class:`~azure_functions_langgraph.stores.azure_table.AzureTableThreadStore`,
        which provides ETag-based atomic locking. Set the
        ``AZFUNC_LANGGRAPH_LOCK_BACKEND`` environment variable to
        ``distributed`` to fail-fast at construction if the default
        in-process backend is still wired.
    """

    auth_level: func.AuthLevel = func.AuthLevel.FUNCTION
    health_auth_level: func.AuthLevel = func.AuthLevel.ANONYMOUS
    max_stream_response_bytes: int = 1024 * 1024
    max_request_body_bytes: int = 1024 * 1024
    max_input_depth: int = 32
    max_input_nodes: int = 10_000
    platform_compat: bool = False
    thread_lock: Optional[ThreadLock] = None
    route_prefix: str = _ROUTE_PREFIX  # metadata-only; must match host.json routePrefix
    _registrations: dict[str, _GraphRegistration] = field(default_factory=dict)
    _function_app: Optional[func.FunctionApp] = field(default=None, init=False, repr=False)
    _thread_store: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.auth_level == func.AuthLevel.ANONYMOUS:
            warnings.warn(
                "LangGraphApp is using ANONYMOUS auth. Endpoints are publicly "
                "accessible without authentication.\n"
                "  Recommended: LangGraphApp(auth_level=func.AuthLevel.FUNCTION)\n"
                "  Per-graph:   app.register(..., auth_level=func.AuthLevel.FUNCTION)\n"
                "  See the 'Production authentication' section in README.md",
                UserWarning,
                stacklevel=2,
            )
        # Normalize route_prefix: ensure leading slash, strip trailing slashes
        if not self.route_prefix.startswith("/"):
            self.route_prefix = "/" + self.route_prefix
        self.route_prefix = self.route_prefix.rstrip("/") or "/"
        if self.platform_compat and self._thread_store is None:
            from azure_functions_langgraph.platform.stores import InMemoryThreadStore

            self._thread_store = InMemoryThreadStore()

        # Instantiate default in-process lock backend if the user did not supply
        # a custom one. See azure_functions_langgraph.locks for backend details.
        if self.thread_lock is None:
            self.thread_lock = InProcessThreadLock()
        # AZFUNC_LANGGRAPH_LOCK_BACKEND is a safety guard that keeps operators
        # from accidentally deploying an in-process lock to a multi-instance
        # Function App. Set it to ``distributed`` (or the exact backend class
        # name) in Function App settings; if the wired backend is still
        # InProcessThreadLock we raise at construction time so the mistake is
        # visible before any request is served.
        env_backend = os.environ.get("AZFUNC_LANGGRAPH_LOCK_BACKEND", "").strip().lower()
        if env_backend and env_backend not in ("", "inprocess"):
            if isinstance(self.thread_lock, InProcessThreadLock):
                raise RuntimeError(
                    f"AZFUNC_LANGGRAPH_LOCK_BACKEND={env_backend!r} requires a "
                    "distributed thread_lock backend (for example "
                    "AzureBlobLeaseThreadLock), but the app is still using the "
                    "default InProcessThreadLock. Pass thread_lock=... explicitly "
                    "to LangGraphApp() or unset the env var for local dev."
                )

    def register(
        self,
        graph: Any,
        name: str,
        description: Optional[str] = None,
        stream: bool = True,
        auth_level: Optional[func.AuthLevel] = None,
        *,
        request_model: Optional[type[Any]] = None,
        response_model: Optional[type[Any]] = None,
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
            request_model: Optional Pydantic model class for request body
                (used by the metadata / bridge API, not for runtime validation).
            response_model: Optional Pydantic model class for response body
                (used by the metadata / bridge API, not for runtime validation).

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
            request_model=request_model,
            response_model=response_model,
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
        @app.route(route=_ROUTE_HEALTH, methods=["GET"], auth_level=self.health_auth_level)
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

        # Per-graph endpoints
        for reg in self._registrations.values():
            self._register_route(
                app,
                reg,
                endpoint="invoke",
                route_template=_ROUTE_INVOKE,
                methods=["POST"],
                handler_impl=self._handle_invoke,
            )
            if self._has_stream_route(reg):
                self._register_route(
                    app,
                    reg,
                    endpoint="stream",
                    route_template=_ROUTE_STREAM,
                    methods=["POST"],
                    handler_impl=self._handle_stream,
                )
            if self._has_state_route(reg):
                self._register_route(
                    app,
                    reg,
                    endpoint="state",
                    route_template=_ROUTE_STATE,
                    methods=["GET"],
                    handler_impl=self._handle_state,
                )

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

    @staticmethod
    def _has_stream_route(reg: _GraphRegistration) -> bool:
        """Whether a graph exposes a streaming endpoint (single source of truth)."""
        return reg.stream_enabled

    @staticmethod
    def _has_state_route(reg: _GraphRegistration) -> bool:
        """Whether a graph exposes a thread-state endpoint (single source of truth)."""
        return isinstance(reg.graph, StatefulGraph)

    def _register_route(
        self,
        app: func.FunctionApp,
        reg: _GraphRegistration,
        *,
        endpoint: str,
        route_template: str,
        methods: list[str],
        handler_impl: Callable[[func.HttpRequest, _GraphRegistration], func.HttpResponse],
    ) -> None:
        """Register one per-graph HTTP route, wiring metadata and auth uniformly."""
        route = route_template.format(name=reg.name)
        fn_name = f"aflg_{reg.name}_{endpoint}"
        captured_reg = reg
        effective_auth = self._effective_auth_level(reg)

        def handler(req: func.HttpRequest) -> func.HttpResponse:
            return handler_impl(req, captured_reg)

        _merge_toolkit_metadata(
            handler,
            "langgraph",
            {
                "version": 1,
                "graph_name": reg.name,
                "endpoint": endpoint,
            },
        )

        app.function_name(name=fn_name)(
            app.route(route=route, methods=methods, auth_level=effective_auth)(handler)
        )

    def _effective_auth_level(self, reg: _GraphRegistration) -> func.AuthLevel:
        """Return per-graph auth if set, otherwise app-level auth."""
        if reg.auth_level is not None:
            return reg.auth_level
        return self.auth_level

    def _metadata_path(self, route: str) -> str:
        """Join route_prefix and route, avoiding double slashes."""
        if self.route_prefix == "/":
            return f"/{route}"
        return f"{self.route_prefix}/{route}"

    # ------------------------------------------------------------------
    # Request handlers (thin delegation to _handlers module)
    # ------------------------------------------------------------------

    def _handle_invoke(self, req: func.HttpRequest, reg: _GraphRegistration) -> func.HttpResponse:
        """Handle a synchronous invoke request."""
        thread_lock = self.thread_lock
        if thread_lock is None:  # pragma: no cover - invariant set in __post_init__
            raise RuntimeError("thread_lock is None; __post_init__ did not run")
        return handle_invoke(
            req,
            reg,
            thread_lock=thread_lock,
            max_request_body_bytes=self.max_request_body_bytes,
            max_input_depth=self.max_input_depth,
            max_input_nodes=self.max_input_nodes,
        )

    def _handle_stream(self, req: func.HttpRequest, reg: _GraphRegistration) -> func.HttpResponse:
        """Handle a streaming request."""
        thread_lock = self.thread_lock
        if thread_lock is None:  # pragma: no cover - invariant set in __post_init__
            raise RuntimeError("thread_lock is None; __post_init__ did not run")
        return handle_stream(
            req,
            reg,
            thread_lock=thread_lock,
            max_stream_response_bytes=self.max_stream_response_bytes,
            max_request_body_bytes=self.max_request_body_bytes,
            max_input_depth=self.max_input_depth,
            max_input_nodes=self.max_input_nodes,
        )

    def _handle_state(self, req: func.HttpRequest, reg: _GraphRegistration) -> func.HttpResponse:
        """Handle a GET request for thread state."""
        return handle_state(
            req,
            reg,
        )

    # ------------------------------------------------------------------
    # Metadata API
    # ------------------------------------------------------------------

    def get_app_metadata(self) -> AppMetadata:
        """Return an immutable metadata snapshot of all registered routes.

        The returned :class:`~contracts.AppMetadata` contains per-graph routes
        and app-level routes (e.g. ``/health``).  External consumers such as
        the ``azure-functions-openapi-python`` bridge use this to generate specs.

        Note:
            Route paths use the configured ``route_prefix`` (default ``/api``).
            This metadata-only prefix should match the Azure Functions
            ``host.json`` ``routePrefix`` setting.
        """
        graphs: dict[str, RegisteredGraphMetadata] = {}
        for reg in self._registrations.values():
            routes: list[RouteMetadata] = []
            # invoke route
            routes.append(
                RouteMetadata(
                    path=self._metadata_path(_ROUTE_INVOKE.format(name=reg.name)),
                    method="POST",
                    summary=f"Invoke graph '{reg.name}'",
                    request_model=reg.request_model,
                    response_model=reg.response_model,
                )
            )
            # stream route (if enabled)
            if self._has_stream_route(reg):
                routes.append(
                    RouteMetadata(
                        path=self._metadata_path(_ROUTE_STREAM.format(name=reg.name)),
                        method="POST",
                        summary=f"Stream graph '{reg.name}'",
                        request_model=reg.request_model,
                        # Stream responses are SSE, not a single JSON body
                    )
                )
            # state route — use same capability test as _build_function_app
            if self._has_state_route(reg):
                routes.append(
                    RouteMetadata(
                        path=self._metadata_path(_ROUTE_STATE.format(name=reg.name)),
                        method="GET",
                        summary=f"Get thread state for '{reg.name}'",
                        parameters=(
                            MappingProxyType(
                                {
                                    "name": "thread_id",
                                    "in": "path",
                                    "required": True,
                                    "schema": {"type": "string"},
                                }
                            ),
                        ),
                    )
                )
            graphs[reg.name] = RegisteredGraphMetadata(
                name=reg.name,
                description=reg.description,
                routes=tuple(routes),
            )

        # App-level routes
        app_routes: tuple[RouteMetadata, ...] = (
            RouteMetadata(
                path=self._metadata_path(_ROUTE_HEALTH),
                method="GET",
                summary="Health check",
            ),
        )

        return AppMetadata(graphs=MappingProxyType(graphs), app_routes=app_routes)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _has_checkpointer(graph: Any) -> bool:
    """Check whether a compiled graph has a checkpointer attached."""
    return getattr(graph, "checkpointer", None) is not None


def get_langgraph_metadata(func: Any) -> dict[str, Any] | None:
    """Return langgraph metadata if the function was created by ``LangGraphApp``.

    Returns ``None`` if the function has no langgraph metadata attached.
    """
    toolkit_meta = getattr(func, _TOOLKIT_META_ATTR, None)
    if isinstance(toolkit_meta, dict):
        meta = toolkit_meta.get("langgraph")
        if isinstance(meta, dict):
            return meta
    return None
