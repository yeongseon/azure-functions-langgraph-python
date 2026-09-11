"""StreamingLangGraphApp — opt-in **true** HTTP/SSE streaming for LangGraph graphs.

Unlike :class:`~azure_functions_langgraph.app.LangGraphApp`, whose ``/stream``
endpoint returns **buffered** SSE (all chunks collected, then flushed after the
run completes), this app serves ``/stream`` as a real, incrementally-flushed
``text/event-stream`` response: each event LangGraph emits is written to the
wire as soon as it is produced.

This is a deliberately **separate** app type. Azure Functions Python v2 exposes
true HTTP streaming only through the ``azurefunctions-extensions-http-fastapi``
extension (runtime 4.34.1+), which switches the **entire** function app to the
FastAPI/ASGI request/response model. That model cannot be mixed with the classic
``azure.functions.HttpRequest``/``HttpResponse`` routes ``LangGraphApp`` is built
on, so the classic app keeps its buffered contract untouched and callers opt in
to true streaming by constructing :class:`StreamingLangGraphApp` instead (issue
#406).

Runtime prerequisites (a real deploy must satisfy all three):

- Azure Functions runtime **4.34.1+**.
- The ``streaming`` extra installed (``pip install
  azure-functions-langgraph[streaming]``) so the FastAPI extension is present.
- ``PYTHON_ENABLE_INIT_INDEXING=1`` in the Function App settings.

Because enabling HTTP streaming makes the whole app FastAPI-based, **every**
route here (health, invoke, stream) returns an extension response type; there is
no classic-route fallback within a ``StreamingLangGraphApp``.

Scope note: thread-state retrieval
(``GET /graphs/{name}/threads/{thread_id}/state``) is intentionally **not**
exposed on the streaming surface. Use the classic :class:`LangGraphApp` for
state inspection; the streaming app is focused on invoke + true-streaming
delivery.
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any, AsyncIterator, Optional
import warnings

import azure.functions as func

from azure_functions_langgraph._handlers import (
    _method_accepts_version,
    _serialize_graph_output,
)
from azure_functions_langgraph._validation import (
    validate_body_size,
    validate_graph_name,
    validate_input_structure,
    validate_thread_id,
)
from azure_functions_langgraph.contracts import (
    HealthResponse,
    HealthStatus,
    InvokeRequest,
    InvokeResponse,
    StreamRequest,
    _is_model_type,
)
from azure_functions_langgraph.locks import InProcessThreadLock, ThreadLock
from azure_functions_langgraph.observability import (
    NoOpRunObserver,
    RunContext,
    RunObserver,
    RunRejectedReason,
    finish_context,
    new_run_context,
    safe_observer_call,
)
from azure_functions_langgraph.protocols import (
    AsyncInvocableGraph,
    AsyncStreamableGraph,
    InvocableGraph,
    StreamableGraph,
)

logger = logging.getLogger(__name__)

# Route path templates — kept identical to the native LangGraphApp surface so a
# streaming deployment answers the same URLs (minus the state endpoint).
_ROUTE_HEALTH = "health"
_ROUTE_HEALTH_DETAILS = "health/details"
_ROUTE_INVOKE = "graphs/{name}/invoke"
_ROUTE_STREAM = "graphs/{name}/stream"

# Friendly install hint surfaced when the FastAPI extension is missing.
_EXTRA_HINT = (
    "StreamingLangGraphApp requires the optional 'streaming' extra "
    "(the azurefunctions-extensions-http-fastapi extension). Install it with: "
    "pip install azure-functions-langgraph[streaming]"
)


def _import_fastapi_extension() -> Any:
    """Import the FastAPI extension response/request types, or raise a hint.

    Kept lazy (not a module-level import) so ``import
    azure_functions_langgraph.streaming`` stays cheap and the class can be
    referenced for typing/metadata even when the ``streaming`` extra is not
    installed. The friendly :class:`ImportError` names the extra to install.
    """
    try:
        from azurefunctions.extensions.http import fastapi as ext
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(_EXTRA_HINT) from exc
    return ext


# ------------------------------------------------------------------
# Registration record
# ------------------------------------------------------------------


@dataclass
class _StreamRegistration:
    """Internal registration record for a compiled graph on the streaming app."""

    graph: Any
    name: str
    description: Optional[str] = None
    stream_enabled: bool = True
    async_mode: bool = False
    auth_level: Optional[func.AuthLevel] = None
    request_model: Optional[type[Any]] = None
    response_model: Optional[type[Any]] = None


def _validate_optional_model(model: Optional[type[Any]], label: str) -> None:
    """Reject a non-``BaseModel`` *model* early (mirrors ``app._validate_optional_model``)."""
    if model is not None and not _is_model_type(model):
        raise TypeError(f"{label} must be a Pydantic BaseModel subclass, got {model!r}")


# ------------------------------------------------------------------
# Request parsing helpers (Starlette Request → validated contract model)
# ------------------------------------------------------------------


async def _read_json_body(req: Any, *, max_request_body_bytes: int) -> Any:
    """Read and JSON-decode a Starlette request body, enforcing the size cap.

    Returns the decoded JSON object on success, or a ``_JsonError`` sentinel
    carrying the ``(status_code, detail)`` to translate into a JSON response.
    """
    raw_body: bytes = await req.body()
    size_err = validate_body_size(raw_body, max_request_body_bytes)
    if size_err:
        return _JsonError(400, size_err)
    try:
        return json.loads(raw_body) if raw_body else {}
    except ValueError:
        return _JsonError(400, "Invalid JSON body")


@dataclass(frozen=True)
class _JsonError:
    """Internal marker for a pre-execution failure to render as a JSON error."""

    status_code: int
    detail: str


def _validate_request(
    body: Any,
    model: type[Any],
    *,
    max_input_depth: int,
    max_input_nodes: int,
) -> Any:
    """Validate a decoded body against *model* and its input/config structure.

    Returns the validated model instance, or a ``_JsonError`` on failure.
    """
    try:
        request = model.model_validate(body)
    except Exception as exc:  # noqa: BLE001 - surfaced as a 422 to the caller
        return _JsonError(422, f"Validation error: {exc}")
    structure_err = validate_input_structure(
        request.input, max_depth=max_input_depth, max_nodes=max_input_nodes
    )
    if structure_err:
        return _JsonError(400, structure_err)
    if request.config:
        config_err = validate_input_structure(
            request.config, max_depth=max_input_depth, max_nodes=max_input_nodes
        )
        if config_err:
            return _JsonError(400, config_err)
    return request


def _extract_thread_id(config: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract ``thread_id`` from ``config.configurable`` (mirrors ``_handlers``)."""
    configurable = config.get("configurable")
    if configurable is None:
        return None, None
    if not isinstance(configurable, dict):
        return None, "config.configurable must be an object"
    thread_id = configurable.get("thread_id")
    if thread_id is None:
        return None, None
    if not isinstance(thread_id, str):
        return None, "config.configurable.thread_id must be a string"
    tid_err = validate_thread_id(thread_id)
    if tid_err:
        return None, tid_err
    return thread_id, None


def _resolve_version_kwarg(
    method: Any, version: str | None, graph_name: str
) -> dict[str, str] | _JsonError:
    """Resolve a caller ``version`` request into forwardable kwargs, or a 422."""
    if version is None:
        return {}
    if not _method_accepts_version(method):
        return _JsonError(
            422,
            f"Graph {graph_name!r} does not accept a 'version' argument (requires langgraph>=1.1)",
        )
    return {"version": version}


# ------------------------------------------------------------------
# The true-streaming SSE generator (extension-independent, fully testable)
# ------------------------------------------------------------------


async def _aiter_graph_events(
    graph: Any,
    input_: dict[str, Any],
    config: dict[str, Any],
    stream_mode: str,
    version_kwargs: dict[str, str],
) -> AsyncIterator[Any]:
    """Yield graph stream events incrementally for sync or async graphs.

    An :class:`AsyncStreamableGraph` is consumed with ``async for``. A
    synchronous :class:`StreamableGraph` is pulled one item at a time via
    :func:`asyncio.to_thread`, so a blocking sync generator never stalls the
    event loop between chunks (preserving incremental delivery).
    """
    if isinstance(graph, AsyncStreamableGraph):
        async for event in graph.astream(
            input_, config=config, stream_mode=stream_mode, **version_kwargs
        ):
            yield event
        return

    iterator = graph.stream(input_, config=config, stream_mode=stream_mode, **version_kwargs)
    sentinel = object()
    while True:
        item = await asyncio.to_thread(next, iterator, sentinel)
        if item is sentinel:
            break
        yield item


def _format_data_event(event: Any) -> str:
    """Serialize one graph event as an SSE ``data`` frame (native wire format)."""
    serialized = json.dumps(
        event if isinstance(event, dict) else {"data": str(event)},
        default=str,
        allow_nan=False,
    )
    return f"event: data\ndata: {serialized}\n\n"


async def _sse_event_stream(
    *,
    graph: Any,
    graph_name: str,
    input_: dict[str, Any],
    config: dict[str, Any],
    stream_mode: str,
    version_kwargs: dict[str, str],
    thread_id: str | None,
    lock_token: str | None,
    thread_lock: ThreadLock,
    observer: RunObserver,
    ctx: RunContext,
    max_stream_events: int,
) -> AsyncIterator[str]:
    """Produce a true-streaming SSE body, one frame at a time.

    Contract (mirrors the native buffered handler's event vocabulary, but each
    frame is flushed as produced):

    - ``event: data`` per graph event, encoded exactly like the buffered path.
    - ``event: error`` if the graph raises **after** streaming has begun, or if
      the ``max_stream_events`` safety cap is hit — followed by ``event: end``.
    - ``event: end`` terminates every completed stream.

    Safety-cap note (replaces ``LangGraphApp.max_stream_response_bytes``): a
    true stream is never buffered, so a byte cap on a materialized body is
    meaningless. Instead ``max_stream_events`` bounds the number of frames a
    single run may emit; exceeding it emits an ``error`` frame and stops.

    The thread lock (acquired by the caller before the response headers were
    sent) is released in ``finally`` for the **entire** stream lifetime — on
    normal completion, on graph error, and on client disconnect
    (:class:`asyncio.CancelledError`).
    """
    stream_error: BaseException | None = None
    count = 0
    released = False
    try:
        async for event in _aiter_graph_events(graph, input_, config, stream_mode, version_kwargs):
            yield _format_data_event(event)
            count += 1
            if count >= max_stream_events:
                stream_error = RuntimeError(f"stream exceeded max events ({max_stream_events})")
                payload = json.dumps({"error": f"stream exceeded max events ({max_stream_events})"})
                yield f"event: error\ndata: {payload}\n\n"
                break
    except asyncio.CancelledError:
        # Client disconnected mid-stream. Release the lock (finally) and report
        # the run as failed, then propagate the cancellation.
        logger.info("Graph %s stream cancelled (client disconnect)", graph_name)
        if lock_token is not None and thread_id is not None:
            await asyncio.to_thread(thread_lock.release, graph_name, thread_id, lock_token)
            released = True
        safe_observer_call(observer, "on_run_failed", finish_context(ctx), asyncio.CancelledError())
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as an SSE error frame
        logger.exception("Graph %s stream failed", graph_name)
        stream_error = exc
        payload = json.dumps({"error": "stream processing failed"})
        yield f"event: error\ndata: {payload}\n\n"
    finally:
        # Normal / error path (the cancellation path released above and re-raised).
        if lock_token is not None and thread_id is not None and not released:
            await asyncio.to_thread(thread_lock.release, graph_name, thread_id, lock_token)

    yield "event: end\ndata: {}\n\n"

    if stream_error is not None:
        safe_observer_call(observer, "on_run_failed", finish_context(ctx), stream_error)
    else:
        safe_observer_call(observer, "on_run_completed", finish_context(ctx))

# ------------------------------------------------------------------
# The app
# ------------------------------------------------------------------


@dataclass
class StreamingLangGraphApp:
    """Deploy LangGraph graphs as Azure Functions with **true** HTTP streaming.

    Usage::

        from azure_functions_langgraph.streaming import StreamingLangGraphApp

        app = StreamingLangGraphApp()
        app.register(graph=compiled_graph, name="my_agent")
        func_app = app.function_app

    Auto-registers:

    - ``POST /api/graphs/{name}/invoke`` — synchronous invocation (JSON body)
    - ``POST /api/graphs/{name}/stream`` — **true** incremental SSE stream
    - ``GET /api/health`` — anonymous liveness probe (``{"status": "ok"}``)
    - ``GET /api/health/details`` — registered-graph inventory (protected)

    Requires the ``streaming`` extra and Azure Functions runtime 4.34.1+ (see
    the module docstring for the full prerequisite list). Accessing
    :attr:`function_app` without the extra installed raises a friendly
    :class:`ImportError`.
    """

    auth_level: func.AuthLevel = func.AuthLevel.FUNCTION
    health_auth_level: func.AuthLevel = func.AuthLevel.ANONYMOUS
    health_details_auth_level: Optional[func.AuthLevel] = None
    max_request_body_bytes: int = 1024 * 1024
    max_input_depth: int = 32
    max_input_nodes: int = 10_000
    # Replaces LangGraphApp.max_stream_response_bytes: a true stream is not
    # buffered, so it is bounded by a per-run event count instead of a body size.
    max_stream_events: int = 100_000
    thread_lock: Optional[ThreadLock] = None
    observer: Optional[RunObserver] = None
    route_prefix: str = "/api"
    _registrations: dict[str, _StreamRegistration] = field(default_factory=dict)
    _function_app: Optional[func.FunctionApp] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.auth_level == func.AuthLevel.ANONYMOUS:
            warnings.warn(
                "StreamingLangGraphApp is using ANONYMOUS auth. Endpoints are "
                "publicly accessible without authentication.\n"
                "  Recommended: StreamingLangGraphApp(auth_level=func.AuthLevel.FUNCTION)",
                UserWarning,
                stacklevel=2,
            )
        if not self.route_prefix.startswith("/"):
            self.route_prefix = "/" + self.route_prefix
        self.route_prefix = self.route_prefix.rstrip("/") or "/"
        if self.thread_lock is None:
            self.thread_lock = InProcessThreadLock()
        if self.observer is None:
            self.observer = NoOpRunObserver()
        env_backend = os.environ.get("AZFUNC_LANGGRAPH_LOCK_BACKEND", "").strip().lower()
        if env_backend and env_backend not in ("", "inprocess"):
            if isinstance(self.thread_lock, InProcessThreadLock):
                raise RuntimeError(
                    f"AZFUNC_LANGGRAPH_LOCK_BACKEND={env_backend!r} requires a "
                    "distributed thread_lock backend (for example "
                    "AzureBlobLeaseThreadLock), but the app is still using the "
                    "default InProcessThreadLock. Pass thread_lock=... explicitly "
                    "to StreamingLangGraphApp() or unset the env var for local dev."
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
        async_mode: bool = False,
    ) -> None:
        """Register a compiled LangGraph graph (mirrors ``LangGraphApp.register``).

        Args:
            graph: Any object satisfying the graph protocol (invoke/stream).
            name: Unique name for this graph (used in URL routes).
            description: Optional human-readable description.
            stream: Whether to enable the true-streaming endpoint for this graph.
            auth_level: Override app-level auth for this graph's endpoints.
            request_model: Optional Pydantic model class for request body (metadata only).
            response_model: Optional Pydantic model class for response body (metadata only).
            async_mode: Route invoke/stream through the async graph methods.

        Raises:
            TypeError: If *graph* lacks invoke/ainvoke, or ``async_mode=True``
                but the graph has no ``ainvoke``, or a supplied model is not a
                Pydantic ``BaseModel`` subclass.
            ValueError: If *name* is already registered or invalid.
        """
        has_sync_invoke = isinstance(graph, InvocableGraph)
        has_async_invoke = isinstance(graph, AsyncInvocableGraph)
        if async_mode and not has_async_invoke:
            raise TypeError(
                f"async_mode=True requires an ainvoke() method. Got {type(graph).__name__}"
            )
        if not has_sync_invoke and not has_async_invoke:
            raise TypeError(
                f"Graph must have an invoke() or ainvoke() method. Got {type(graph).__name__}"
            )
        _validate_optional_model(request_model, "request_model")
        _validate_optional_model(response_model, "response_model")
        name_err = validate_graph_name(name)
        if name_err:
            raise ValueError(name_err)
        if name in self._registrations:
            raise ValueError(f"Graph {name!r} is already registered")
        self._registrations[name] = _StreamRegistration(
            graph=graph,
            name=name,
            description=description,
            stream_enabled=stream,
            async_mode=async_mode,
            auth_level=auth_level,
            request_model=request_model,
            response_model=response_model,
        )
        self._function_app = None

    @property
    def function_app(self) -> func.FunctionApp:
        """Return an ``azure.functions.FunctionApp`` with all streaming routes wired.

        Raises:
            ImportError: If the ``streaming`` extra (FastAPI extension) is not
                installed — the message names the extra to install.
        """
        if self._function_app is None:
            self._function_app = self._build_function_app()
        return self._function_app

    @property
    def _resolved_health_details_auth_level(self) -> func.AuthLevel:
        if self.health_details_auth_level is None:
            return self.auth_level
        return self.health_details_auth_level

    def _effective_auth_level(self, reg: _StreamRegistration) -> func.AuthLevel:
        if reg.auth_level is not None:
            return reg.auth_level
        return self.auth_level

    @staticmethod
    def _is_async(reg: _StreamRegistration) -> bool:
        """Whether a graph is served through the async invoke/stream path."""
        return reg.async_mode or not isinstance(reg.graph, InvocableGraph)

    # ------------------------------------------------------------------
    # Route building
    # ------------------------------------------------------------------

    def _build_function_app(self) -> func.FunctionApp:
        ext = _import_fastapi_extension()
        Request = ext.Request
        JSONResponse = ext.JSONResponse
        StreamingResponse = ext.StreamingResponse

        app = func.FunctionApp(http_auth_level=self.auth_level)
        observer = self.observer or NoOpRunObserver()
        thread_lock = self.thread_lock
        if thread_lock is None:  # pragma: no cover - invariant set in __post_init__
            raise RuntimeError("thread_lock is None; __post_init__ did not run")

        # --- Health endpoints -----------------------------------------
        @app.function_name(name="aflg_health")
        @app.route(route=_ROUTE_HEALTH, methods=["GET"], auth_level=self.health_auth_level)
        async def health(req: Any) -> Any:
            return JSONResponse(content=json.loads(HealthStatus().model_dump_json()))

        @app.function_name(name="aflg_health_details")
        @app.route(
            route=_ROUTE_HEALTH_DETAILS,
            methods=["GET"],
            auth_level=self._resolved_health_details_auth_level,
        )
        async def health_details(req: Any) -> Any:
            from azure_functions_langgraph.contracts import GraphInfo

            graphs = [
                GraphInfo(
                    name=reg.name,
                    description=reg.description,
                    has_checkpointer=getattr(reg.graph, "checkpointer", None) is not None,
                )
                for reg in self._registrations.values()
            ]
            body = HealthResponse(graphs=graphs)
            return JSONResponse(content=json.loads(body.model_dump_json()))

        # --- Per-graph endpoints --------------------------------------
        for reg in self._registrations.values():
            self._register_invoke(app, reg, Request, JSONResponse, observer, thread_lock)
            if reg.stream_enabled:
                self._register_stream(
                    app, reg, Request, JSONResponse, StreamingResponse, observer, thread_lock
                )

        return app

    def _register_invoke(
        self,
        app: func.FunctionApp,
        reg: _StreamRegistration,
        Request: Any,
        JSONResponse: Any,
        observer: RunObserver,
        thread_lock: ThreadLock,
    ) -> None:
        captured = reg
        is_async = self._is_async(reg)
        effective_auth = self._effective_auth_level(reg)
        route = _ROUTE_INVOKE.format(name=reg.name)
        fn_name = f"aflg_{reg.name}_invoke"

        async def invoke(req: Any) -> Any:
            return await self._handle_invoke(
                req, captured, is_async, JSONResponse, observer, thread_lock
            )

        app.function_name(name=fn_name)(
            app.route(route=route, methods=["POST"], auth_level=effective_auth)(invoke)
        )

    def _register_stream(
        self,
        app: func.FunctionApp,
        reg: _StreamRegistration,
        Request: Any,
        JSONResponse: Any,
        StreamingResponse: Any,
        observer: RunObserver,
        thread_lock: ThreadLock,
    ) -> None:
        captured = reg
        is_async = self._is_async(reg)
        effective_auth = self._effective_auth_level(reg)
        route = _ROUTE_STREAM.format(name=reg.name)
        fn_name = f"aflg_{reg.name}_stream"

        async def stream(req: Any) -> Any:
            return await self._handle_stream(
                req, captured, is_async, JSONResponse, StreamingResponse, observer, thread_lock
            )

        app.function_name(name=fn_name)(
            app.route(route=route, methods=["POST"], auth_level=effective_auth)(stream)
        )

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_invoke(
        self,
        req: Any,
        reg: _StreamRegistration,
        is_async: bool,
        JSONResponse: Any,
        observer: RunObserver,
        thread_lock: ThreadLock,
    ) -> Any:
        has_cp = getattr(reg.graph, "checkpointer", None) is not None
        ctx = new_run_context(
            reg.name,
            "invoke",
            transport="streaming",
            has_checkpointer=has_cp,
            lock_backend=type(thread_lock).__name__,
        )

        body = await _read_json_body(req, max_request_body_bytes=self.max_request_body_bytes)
        if isinstance(body, _JsonError):
            return self._reject(JSONResponse, observer, ctx, "invalid_request", body)
        parsed = _validate_request(
            body,
            InvokeRequest,
            max_input_depth=self.max_input_depth,
            max_input_nodes=self.max_input_nodes,
        )
        if isinstance(parsed, _JsonError):
            return self._reject(JSONResponse, observer, ctx, "invalid_request", parsed)
        request = parsed

        config = request.config or {}
        thread_id, cfg_err = _extract_thread_id(config)
        if cfg_err:
            return self._reject(
                JSONResponse, observer, ctx, "invalid_config", _JsonError(400, cfg_err)
            )
        ctx = dataclasses.replace(ctx, thread_id=thread_id)
        method = reg.graph.ainvoke if is_async else reg.graph.invoke
        version_kwargs = _resolve_version_kwarg(method, request.version, reg.name)
        if isinstance(version_kwargs, _JsonError):
            return self._reject(JSONResponse, observer, ctx, "unsupported_version", version_kwargs)

        lock_token: str | None = None
        if has_cp and thread_id:
            lock_token = await asyncio.to_thread(thread_lock.acquire, reg.name, thread_id)
            if not lock_token:
                return self._reject(
                    JSONResponse,
                    observer,
                    ctx,
                    "lock_contention",
                    _JsonError(409, f"Thread {thread_id!r} is currently in use by another request"),
                )

        safe_observer_call(observer, "on_run_started", ctx)
        try:
            if is_async:
                result = await reg.graph.ainvoke(request.input, config=config, **version_kwargs)
            else:
                result = await asyncio.to_thread(
                    lambda: reg.graph.invoke(request.input, config=config, **version_kwargs)
                )
        except Exception as exc:  # noqa: BLE001 - reported and returned as 500
            logger.exception("Graph %s invoke failed", reg.name)
            safe_observer_call(observer, "on_run_failed", finish_context(ctx), exc)
            return self._json_error(JSONResponse, 500, "Graph execution failed")
        finally:
            if lock_token is not None and thread_id is not None:
                await asyncio.to_thread(thread_lock.release, reg.name, thread_id, lock_token)

        safe_observer_call(observer, "on_run_completed", finish_context(ctx))
        output = _serialize_graph_output(result)
        response = InvokeResponse(output=output)
        return JSONResponse(content=json.loads(response.model_dump_json()), status_code=200)

    async def _handle_stream(
        self,
        req: Any,
        reg: _StreamRegistration,
        is_async: bool,
        JSONResponse: Any,
        StreamingResponse: Any,
        observer: RunObserver,
        thread_lock: ThreadLock,
    ) -> Any:
        has_cp = getattr(reg.graph, "checkpointer", None) is not None
        ctx = new_run_context(
            reg.name,
            "stream",
            transport="streaming",
            has_checkpointer=has_cp,
            lock_backend=type(thread_lock).__name__,
        )

        streamable = isinstance(reg.graph, AsyncStreamableGraph if is_async else StreamableGraph)
        if not streamable:
            return self._reject(
                JSONResponse,
                observer,
                ctx,
                "streaming_unsupported",
                _JsonError(501, f"Graph {reg.name!r} does not support streaming"),
            )

        body = await _read_json_body(req, max_request_body_bytes=self.max_request_body_bytes)
        if isinstance(body, _JsonError):
            return self._reject(JSONResponse, observer, ctx, "invalid_request", body)
        parsed = _validate_request(
            body,
            StreamRequest,
            max_input_depth=self.max_input_depth,
            max_input_nodes=self.max_input_nodes,
        )
        if isinstance(parsed, _JsonError):
            return self._reject(JSONResponse, observer, ctx, "invalid_request", parsed)
        request = parsed

        config = request.config or {}
        thread_id, cfg_err = _extract_thread_id(config)
        if cfg_err:
            return self._reject(
                JSONResponse, observer, ctx, "invalid_config", _JsonError(400, cfg_err)
            )
        ctx = dataclasses.replace(ctx, thread_id=thread_id, stream_mode=request.stream_mode)
        method = reg.graph.astream if is_async else reg.graph.stream
        version_kwargs = _resolve_version_kwarg(method, request.version, reg.name)
        if isinstance(version_kwargs, _JsonError):
            return self._reject(JSONResponse, observer, ctx, "unsupported_version", version_kwargs)

        lock_token: str | None = None
        if has_cp and thread_id:
            lock_token = await asyncio.to_thread(thread_lock.acquire, reg.name, thread_id)
            if not lock_token:
                return self._reject(
                    JSONResponse,
                    observer,
                    ctx,
                    "lock_contention",
                    _JsonError(409, f"Thread {thread_id!r} is currently in use by another request"),
                )

        # Past this point the response headers are committed and the run has
        # started; any further failure is delivered as an in-band SSE error
        # frame by the generator, not an HTTP status code.
        safe_observer_call(observer, "on_run_started", ctx)
        generator = _sse_event_stream(
            graph=reg.graph,
            graph_name=reg.name,
            input_=request.input,
            config=config,
            stream_mode=request.stream_mode,
            version_kwargs=version_kwargs,
            thread_id=thread_id,
            lock_token=lock_token,
            thread_lock=thread_lock,
            observer=observer,
            ctx=ctx,
            max_stream_events=self.max_stream_events,
        )
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _json_error(JSONResponse: Any, status_code: int, detail: str) -> Any:
        from azure_functions_langgraph.contracts import ErrorResponse

        body = ErrorResponse(error="error", detail=detail)
        return JSONResponse(content=json.loads(body.model_dump_json()), status_code=status_code)

    def _reject(
        self,
        JSONResponse: Any,
        observer: RunObserver,
        ctx: RunContext,
        reason: RunRejectedReason,
        err: _JsonError,
    ) -> Any:
        """Emit ``on_run_rejected`` for a pre-execution failure and return a JSON error."""
        safe_observer_call(observer, "on_run_rejected", finish_context(ctx), reason)
        return self._json_error(JSONResponse, err.status_code, err.detail)
