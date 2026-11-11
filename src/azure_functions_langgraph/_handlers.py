"""Internal request handlers for native graph endpoints.

These are standalone functions extracted from ``LangGraphApp`` to keep
``app.py`` focused on registration and route wiring.  Each function
receives only the explicit dependencies it needs — no reference to
``LangGraphApp`` itself.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Protocol, TypeVar

import azure.functions as func
from pydantic import ValidationError

from azure_functions_langgraph._execution_plan import (
    VERSION_UNSUPPORTED_STATUS,
    admit_request_structure,
    extract_thread_id,
    method_accepts_version,
    negotiate_version,
    plan_run_config,
)
from azure_functions_langgraph._json import dumps
from azure_functions_langgraph._run_authorization import (
    native_authorization_status,
    native_authorization_status_async,
)
from azure_functions_langgraph._validation import (
    ValidationErrorDetail,
    sanitized_validation_errors,
    validate_body_size,
    validate_thread_id,
)
from azure_functions_langgraph.authorization import (
    AuthorizationAction,
    AuthorizationContext,
    ThreadAuthorizer,
    authorization_status,
    authorization_status_async,
)
from azure_functions_langgraph.contracts import (
    ErrorResponse,
    InvokeRequest,
    StateResponse,
    StreamRequest,
)
from azure_functions_langgraph.locks import ThreadLock
from azure_functions_langgraph.locks._async import async_thread_lock
from azure_functions_langgraph.locks.guard import check_ownership_guard, guard_run_config
from azure_functions_langgraph.observability import (
    NOOP_OBSERVER,
    RunContext,
    RunObserver,
    RunRejectedReason,
    finish_context,
    new_run_context,
    request_id_from_headers,
    safe_observer_call,
)
from azure_functions_langgraph.protocols import (
    AsyncStatefulGraph,
    AsyncStreamableGraph,
    StatefulGraph,
    StreamableGraph,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Shared helper
# ------------------------------------------------------------------


def _extract_thread_id(config: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract thread_id from config, returning (thread_id, error_message)."""
    return extract_thread_id(config)


def _authorization_rejection(
    status: int | None, observer: RunObserver, ctx: RunContext
) -> func.HttpResponse | None:
    if status is None:
        return None
    return _reject(observer, ctx, "authorization_denied", _error_response(status, "Access denied"))


def _error_response(
    status_code: int,
    detail: str | list[ValidationErrorDetail],
    request_id: str | None = None,
) -> func.HttpResponse:
    body = ErrorResponse(error="error", detail=detail, request_id=request_id)
    return func.HttpResponse(
        body=body.model_dump_json(),
        mimetype="application/json",
        status_code=status_code,
        headers={"X-Request-ID": request_id} if request_id is not None else None,
    )


def _reject(
    observer: RunObserver,
    ctx: RunContext,
    reason: RunRejectedReason,
    response: func.HttpResponse,
) -> func.HttpResponse:
    """Emit ``on_run_rejected`` for a pre-execution rejection and return *response*."""
    safe_observer_call(observer, "on_run_rejected", finish_context(ctx), reason)
    payload = json.loads(response.get_body())
    payload["request_id"] = ctx.run_id
    return func.HttpResponse(
        body=json.dumps(payload),
        mimetype="application/json",
        status_code=response.status_code,
        headers={"X-Request-ID": ctx.run_id},
    )


def _method_accepts_version(method: Any) -> bool:
    """Return ``True`` if *method* accepts a ``version`` keyword argument."""
    return method_accepts_version(method)


def _resolve_version_kwarg(
    method: Any, version: str | None, graph_name: str
) -> dict[str, str] | func.HttpResponse:
    """Resolve the caller's ``version`` into forwardable kwargs or a ``422`` response."""
    kwargs, error = negotiate_version(method, version, graph_name)
    if error is not None:
        return _error_response(VERSION_UNSUPPORTED_STATUS, error)
    return kwargs


async def _aclose_async_iterator(agen: Any) -> None:
    """Await ``agen.aclose()`` if the structural graph's iterator supports it.

    ``AsyncStreamableGraph.astream`` is typed to return ``AsyncIterator``,
    which does not guarantee ``aclose()`` -- a structural implementation may
    be a plain async iterator rather than an async generator. Breaking out of
    an ``async for`` loop does not itself await the generator's pending
    ``finally`` block; without an explicit ``aclose()`` inside the lock scope,
    that cleanup can run after the handler has already released the thread
    lock and returned (#650).
    """
    aclose = getattr(agen, "aclose", None)
    if aclose is not None:
        await aclose()


class _ParsableRequest(Protocol):
    """Structural type for the native invoke/stream request bodies."""

    input: dict[str, Any]
    config: dict[str, Any] | None

    @classmethod
    def model_validate(cls: type[_ParsableRequestT], obj: Any) -> _ParsableRequestT: ...


_ParsableRequestT = TypeVar("_ParsableRequestT", bound=_ParsableRequest)


def _parse_native_request(
    req: func.HttpRequest,
    model: type[_ParsableRequestT],
    *,
    max_request_body_bytes: int,
    max_input_depth: int,
    max_input_nodes: int,
    allowed_configurable_keys: frozenset[str] = frozenset(),
) -> _ParsableRequestT | func.HttpResponse:
    """Parse and validate a native invoke/stream request body.

    Shared by ``handle_invoke`` and ``handle_stream``: enforces the body-size
    cap, JSON decoding, model validation, and structural depth/size checks on
    the ``input`` and ``config`` payloads. Returns the validated *model*
    instance on success, or a ``func.HttpResponse`` error otherwise.
    """
    raw_body = req.get_body()
    size_err = validate_body_size(raw_body, max_request_body_bytes)
    if size_err:
        return _error_response(413, size_err)

    try:
        body = req.get_json()
    except (ValueError, RecursionError):
        return _error_response(400, "Invalid JSON body")

    try:
        request = model.model_validate(body)
    except ValidationError as exc:
        details = sanitized_validation_errors(exc)
        logger.debug("Native request validation failed: %s", details)
        return _error_response(422, details)

    rejection = admit_request_structure(
        request,
        max_depth=max_input_depth,
        max_nodes=max_input_nodes,
        allowed_configurable_keys=allowed_configurable_keys,
    )
    if rejection is not None:
        return _error_response(rejection.status, rejection.detail)
    return request


def _serialize_graph_output(result: Any) -> dict[str, Any]:
    """Convert a graph invoke result to a JSON-serializable dict.

    Handles:
    - dict → pass through
    - Pydantic BaseModel → model_dump(mode="json")
    - dataclass → dataclasses.asdict()
    - other → {"result": str(result)} with warning
    """
    if isinstance(result, dict):
        return result

    # Pydantic BaseModel
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")  # type: ignore[no-any-return]
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug("model_dump failed: %s", exc)

    # dataclass
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        try:
            return dataclasses.asdict(result)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug("dataclasses.asdict failed: %s", exc)

    # Fallback
    logger.warning(
        "Graph returned non-dict result of type %s; wrapping as {'result': str(...)}",
        type(result).__name__,
    )
    return {"result": str(result)}


# ------------------------------------------------------------------
# Invoke handler
# ------------------------------------------------------------------


def handle_invoke(
    req: func.HttpRequest,
    reg: Any,
    *,
    thread_lock: ThreadLock,
    max_request_body_bytes: int,
    max_input_depth: int,
    max_input_nodes: int,
    observer: RunObserver = NOOP_OBSERVER,
    authorize: ThreadAuthorizer | None = None,
) -> func.HttpResponse:
    """Handle a synchronous invoke request."""
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    ctx = new_run_context(
        reg.name,
        "invoke",
        has_checkpointer=has_cp,
        lock_backend=type(thread_lock).__name__,
        run_id=request_id_from_headers(req.headers),
    )
    parsed = _parse_native_request(
        req,
        InvokeRequest,
        allowed_configurable_keys=getattr(reg, "allowed_configurable_keys", frozenset()),
        max_request_body_bytes=max_request_body_bytes,
        max_input_depth=max_input_depth,
        max_input_nodes=max_input_nodes,
    )
    if isinstance(parsed, func.HttpResponse):
        return _reject(observer, ctx, "invalid_request", parsed)
    request = parsed

    config, thread_id, cfg_err = plan_run_config(request.config, reg.graph)
    if cfg_err:
        return _reject(observer, ctx, "invalid_config", _error_response(400, cfg_err))
    ctx = dataclasses.replace(ctx, thread_id=thread_id)
    status = native_authorization_status(
        authorize, AuthorizationContext(AuthorizationAction.RUN, reg.name, thread_id, req)
    )
    if denial := _authorization_rejection(status, observer, ctx):
        return denial
    version_kwargs = _resolve_version_kwarg(reg.graph.invoke, request.version, reg.name)
    if isinstance(version_kwargs, func.HttpResponse):
        return _reject(observer, ctx, "unsupported_version", version_kwargs)
    lock_token: str | None = None
    if has_cp and thread_id:
        lock_token = thread_lock.acquire(reg.name, thread_id)
        if not lock_token:
            return _reject(
                observer,
                ctx,
                "lock_contention",
                _error_response(
                    409, f"Thread {thread_id!r} is currently in use by another request"
                ),
            )
    safe_observer_call(observer, "on_run_started", ctx)
    try:
        run_config, guard = guard_run_config(thread_lock, reg.name, thread_id, lock_token, config)
        result = reg.graph.invoke(request.input, config=run_config, **version_kwargs)
        check_ownership_guard(guard)
        body = dumps({"output": _serialize_graph_output(result)}, allow_nan=False)
    except Exception as exc:
        logger.exception("Graph %s invoke failed", reg.name)
        safe_observer_call(observer, "on_run_failed", finish_context(ctx), exc)
        return _error_response(500, "Graph execution failed", ctx.run_id)
    finally:
        if lock_token is not None and thread_id is not None:
            thread_lock.release(reg.name, thread_id, lock_token)

    safe_observer_call(observer, "on_run_completed", finish_context(ctx))
    return func.HttpResponse(
        body=body,
        mimetype="application/json",
        status_code=200,
        headers={"X-Request-ID": ctx.run_id},
    )


# ------------------------------------------------------------------
# Stream handler
# ------------------------------------------------------------------


def handle_stream(
    req: func.HttpRequest,
    reg: Any,
    *,
    thread_lock: ThreadLock,
    max_stream_response_bytes: int,
    max_request_body_bytes: int,
    max_input_depth: int,
    max_input_nodes: int,
    observer: RunObserver = NOOP_OBSERVER,
    authorize: ThreadAuthorizer | None = None,
) -> func.HttpResponse:
    """Handle a streaming request.

    Returns a **buffered** SSE-formatted response: all stream chunks are
    collected first, then returned in a single HTTP response. This is the
    classic ``HttpRequest``/``HttpResponse`` contract of ``LangGraphApp``, not
    a platform limit. Incrementally flushed streaming is served by the
    separate experimental ``StreamingLangGraphApp`` (``streaming`` extra); see
    "Streaming: buffered SSE and true streaming" in ``DESIGN.md``.
    """
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    ctx = new_run_context(
        reg.name,
        "stream",
        has_checkpointer=has_cp,
        lock_backend=type(thread_lock).__name__,
        run_id=request_id_from_headers(req.headers),
    )
    if not reg.stream_enabled:
        return _reject(
            observer,
            ctx,
            "streaming_unsupported",
            _error_response(501, f"Graph {reg.name!r} is configured as invoke-only"),
        )

    if not isinstance(reg.graph, StreamableGraph):
        return _reject(
            observer,
            ctx,
            "streaming_unsupported",
            _error_response(501, f"Graph {reg.name!r} does not support streaming"),
        )

    parsed = _parse_native_request(
        req,
        StreamRequest,
        allowed_configurable_keys=getattr(reg, "allowed_configurable_keys", frozenset()),
        max_request_body_bytes=max_request_body_bytes,
        max_input_depth=max_input_depth,
        max_input_nodes=max_input_nodes,
    )
    if isinstance(parsed, func.HttpResponse):
        return _reject(observer, ctx, "invalid_request", parsed)
    request = parsed

    config, thread_id, cfg_err = plan_run_config(request.config, reg.graph)
    if cfg_err:
        return _reject(observer, ctx, "invalid_config", _error_response(400, cfg_err))
    ctx = dataclasses.replace(ctx, thread_id=thread_id, stream_mode=request.stream_mode)
    status = native_authorization_status(
        authorize, AuthorizationContext(AuthorizationAction.RUN, reg.name, thread_id, req)
    )
    if denial := _authorization_rejection(status, observer, ctx):
        return denial
    version_kwargs = _resolve_version_kwarg(reg.graph.stream, request.version, reg.name)
    if isinstance(version_kwargs, func.HttpResponse):
        return _reject(observer, ctx, "unsupported_version", version_kwargs)
    lock_token: str | None = None
    if has_cp and thread_id:
        lock_token = thread_lock.acquire(reg.name, thread_id)
        if not lock_token:
            return _reject(
                observer,
                ctx,
                "lock_contention",
                _error_response(
                    409, f"Thread {thread_id!r} is currently in use by another request"
                ),
            )

    chunks: list[str] = []
    buffered_bytes = 0

    def _append_chunk(chunk: str) -> bool:
        nonlocal buffered_bytes
        chunk_bytes = len(chunk.encode())
        if buffered_bytes + chunk_bytes > max_stream_response_bytes:
            error_payload = json.dumps(
                {
                    "error": (
                        "stream response exceeded max buffered size "
                        f"({max_stream_response_bytes} bytes)"
                    ),
                    "request_id": ctx.run_id,
                }
            )
            chunks.append(f"event: error\ndata: {error_payload}\n\n")
            return False
        chunks.append(chunk)
        buffered_bytes += chunk_bytes
        return True

    stream_error: BaseException | None = None
    safe_observer_call(observer, "on_run_started", ctx)
    try:
        run_config, guard = guard_run_config(thread_lock, reg.name, thread_id, lock_token, config)
        for event in reg.graph.stream(
            request.input,
            config=run_config,
            stream_mode=request.stream_mode,
            **version_kwargs,
        ):
            serialized = dumps(event, allow_nan=False)
            if not _append_chunk(f"event: data\ndata: {serialized}\n\n"):
                stream_error = RuntimeError("stream response exceeded max buffered size")
                break
        if stream_error is None:
            check_ownership_guard(guard)
    except Exception as exc:
        logger.exception("Graph %s stream failed", reg.name)
        stream_error = exc
        error_payload = json.dumps({"error": "stream processing failed", "request_id": ctx.run_id})
        _append_chunk(f"event: error\ndata: {error_payload}\n\n")
    finally:
        if lock_token is not None and thread_id is not None:
            thread_lock.release(reg.name, thread_id, lock_token)

    chunks.append("event: end\ndata: {}\n\n")

    if stream_error is not None:
        safe_observer_call(observer, "on_run_failed", finish_context(ctx), stream_error)
    else:
        safe_observer_call(observer, "on_run_completed", finish_context(ctx))

    return func.HttpResponse(
        body="".join(chunks),
        mimetype="text/event-stream",
        status_code=200,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-ID": ctx.run_id,
        },
    )


# ------------------------------------------------------------------
# Async invoke handler
# ------------------------------------------------------------------


async def handle_invoke_async(
    req: func.HttpRequest,
    reg: Any,
    *,
    thread_lock: ThreadLock,
    max_request_body_bytes: int,
    max_input_depth: int,
    max_input_nodes: int,
    observer: RunObserver = NOOP_OBSERVER,
    authorize: ThreadAuthorizer | None = None,
) -> func.HttpResponse:
    """Handle an invoke request against an async graph via ``ainvoke``.

    Mirrors :func:`handle_invoke` exactly (validation, thread-lock, error, and
    response semantics) but awaits ``reg.graph.ainvoke`` and offloads the sync
    thread-lock calls to a worker thread so a blocking lock backend (e.g. the
    Azure Blob lease) never stalls the event loop.
    """
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    ctx = new_run_context(
        reg.name,
        "invoke",
        has_checkpointer=has_cp,
        lock_backend=type(thread_lock).__name__,
        run_id=request_id_from_headers(req.headers),
    )
    parsed = _parse_native_request(
        req,
        InvokeRequest,
        allowed_configurable_keys=getattr(reg, "allowed_configurable_keys", frozenset()),
        max_request_body_bytes=max_request_body_bytes,
        max_input_depth=max_input_depth,
        max_input_nodes=max_input_nodes,
    )
    if isinstance(parsed, func.HttpResponse):
        return _reject(observer, ctx, "invalid_request", parsed)
    request = parsed

    config, thread_id, cfg_err = plan_run_config(request.config, reg.graph)
    if cfg_err:
        return _reject(observer, ctx, "invalid_config", _error_response(400, cfg_err))
    ctx = dataclasses.replace(ctx, thread_id=thread_id)
    status = await native_authorization_status_async(
        authorize, AuthorizationContext(AuthorizationAction.RUN, reg.name, thread_id, req)
    )
    if denial := _authorization_rejection(status, observer, ctx):
        return denial
    version_kwargs = _resolve_version_kwarg(reg.graph.ainvoke, request.version, reg.name)
    if isinstance(version_kwargs, func.HttpResponse):
        return _reject(observer, ctx, "unsupported_version", version_kwargs)
    lock_thread_id = thread_id if has_cp else None
    async with async_thread_lock(thread_lock, reg.name, lock_thread_id) as lock_token:
        if lock_thread_id is not None and lock_token is None:
            return _reject(
                observer,
                ctx,
                "lock_contention",
                _error_response(
                    409, f"Thread {thread_id!r} is currently in use by another request"
                ),
            )
        safe_observer_call(observer, "on_run_started", ctx)
        try:
            run_config, guard = guard_run_config(
                thread_lock, reg.name, lock_thread_id, lock_token, config
            )
            result = await reg.graph.ainvoke(request.input, config=run_config, **version_kwargs)
            check_ownership_guard(guard)
            body = dumps({"output": _serialize_graph_output(result)}, allow_nan=False)
        except Exception as exc:
            logger.exception("Graph %s ainvoke failed", reg.name)
            safe_observer_call(observer, "on_run_failed", finish_context(ctx), exc)
            return _error_response(500, "Graph execution failed", ctx.run_id)

    safe_observer_call(observer, "on_run_completed", finish_context(ctx))
    return func.HttpResponse(
        body=body,
        mimetype="application/json",
        status_code=200,
        headers={"X-Request-ID": ctx.run_id},
    )


# ------------------------------------------------------------------
# Async stream handler
# ------------------------------------------------------------------


async def handle_stream_async(
    req: func.HttpRequest,
    reg: Any,
    *,
    thread_lock: ThreadLock,
    max_stream_response_bytes: int,
    max_request_body_bytes: int,
    max_input_depth: int,
    max_input_nodes: int,
    observer: RunObserver = NOOP_OBSERVER,
    authorize: ThreadAuthorizer | None = None,
) -> func.HttpResponse:
    """Handle a streaming request against an async graph via ``astream``.

    Async counterpart of :func:`handle_stream`: returns the same **buffered**
    SSE response (issue #378) built by consuming ``reg.graph.astream`` with
    ``async for``. The byte-size cap is enforced mid-stream, and the thread
    lock is released in ``finally`` even when the async generator raises.
    """
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    ctx = new_run_context(
        reg.name,
        "stream",
        has_checkpointer=has_cp,
        lock_backend=type(thread_lock).__name__,
        run_id=request_id_from_headers(req.headers),
    )
    if not reg.stream_enabled:
        return _reject(
            observer,
            ctx,
            "streaming_unsupported",
            _error_response(501, f"Graph {reg.name!r} is configured as invoke-only"),
        )

    if not isinstance(reg.graph, AsyncStreamableGraph):
        return _reject(
            observer,
            ctx,
            "streaming_unsupported",
            _error_response(501, f"Graph {reg.name!r} does not support streaming"),
        )

    parsed = _parse_native_request(
        req,
        StreamRequest,
        allowed_configurable_keys=getattr(reg, "allowed_configurable_keys", frozenset()),
        max_request_body_bytes=max_request_body_bytes,
        max_input_depth=max_input_depth,
        max_input_nodes=max_input_nodes,
    )
    if isinstance(parsed, func.HttpResponse):
        return _reject(observer, ctx, "invalid_request", parsed)
    request = parsed

    config, thread_id, cfg_err = plan_run_config(request.config, reg.graph)
    if cfg_err:
        return _reject(observer, ctx, "invalid_config", _error_response(400, cfg_err))
    ctx = dataclasses.replace(ctx, thread_id=thread_id, stream_mode=request.stream_mode)
    status = await native_authorization_status_async(
        authorize, AuthorizationContext(AuthorizationAction.RUN, reg.name, thread_id, req)
    )
    if denial := _authorization_rejection(status, observer, ctx):
        return denial
    version_kwargs = _resolve_version_kwarg(reg.graph.astream, request.version, reg.name)
    if isinstance(version_kwargs, func.HttpResponse):
        return _reject(observer, ctx, "unsupported_version", version_kwargs)
    lock_thread_id = thread_id if has_cp else None
    async with async_thread_lock(thread_lock, reg.name, lock_thread_id) as lock_token:
        if lock_thread_id is not None and lock_token is None:
            return _reject(
                observer,
                ctx,
                "lock_contention",
                _error_response(
                    409, f"Thread {thread_id!r} is currently in use by another request"
                ),
            )

        chunks: list[str] = []
        buffered_bytes = 0

        def _append_chunk(chunk: str) -> bool:
            nonlocal buffered_bytes
            chunk_bytes = len(chunk.encode())
            if buffered_bytes + chunk_bytes > max_stream_response_bytes:
                error_payload = json.dumps(
                    {
                        "error": (
                            "stream response exceeded max buffered size "
                            f"({max_stream_response_bytes} bytes)"
                        ),
                        "request_id": ctx.run_id,
                    }
                )
                chunks.append(f"event: error\ndata: {error_payload}\n\n")
                return False
            chunks.append(chunk)
            buffered_bytes += chunk_bytes
            return True

        stream_error: BaseException | None = None
        safe_observer_call(observer, "on_run_started", ctx)
        agen = None
        try:
            try:
                run_config, guard = guard_run_config(
                    thread_lock, reg.name, lock_thread_id, lock_token, config
                )
                agen = reg.graph.astream(
                    request.input,
                    config=run_config,
                    stream_mode=request.stream_mode,
                    **version_kwargs,
                )
                async for event in agen:
                    serialized = dumps(event, allow_nan=False)
                    if not _append_chunk(f"event: data\ndata: {serialized}\n\n"):
                        stream_error = RuntimeError("stream response exceeded max buffered size")
                        break
                if stream_error is None:
                    check_ownership_guard(guard)
            except Exception as exc:
                logger.exception("Graph %s astream failed", reg.name)
                stream_error = exc
                error_payload = json.dumps(
                    {"error": "stream processing failed", "request_id": ctx.run_id}
                )
                _append_chunk(f"event: error\ndata: {error_payload}\n\n")
        finally:
            # Explicitly finish the async generator's cleanup (its own
            # ``finally``) while the thread lock is still held, on every
            # exit path: normal completion, the byte-cap break above, and
            # the astream() exception above. Breaking out of ``async for``
            # does not itself await that cleanup (#650).
            if agen is not None:
                await _aclose_async_iterator(agen)

    chunks.append("event: end\ndata: {}\n\n")

    if stream_error is not None:
        safe_observer_call(observer, "on_run_failed", finish_context(ctx), stream_error)
    else:
        safe_observer_call(observer, "on_run_completed", finish_context(ctx))

    return func.HttpResponse(
        body="".join(chunks),
        mimetype="text/event-stream",
        status_code=200,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-ID": ctx.run_id,
        },
    )


# ------------------------------------------------------------------
# State handler
# ------------------------------------------------------------------


def handle_state(
    req: func.HttpRequest,
    reg: Any,
    authorize: ThreadAuthorizer | None = None,
) -> func.HttpResponse:
    """Handle a GET request for thread state."""
    if not isinstance(reg.graph, StatefulGraph):
        return _error_response(409, f"Graph {reg.name!r} does not support state retrieval")

    thread_id = req.route_params.get("thread_id")
    if not thread_id:
        return _error_response(400, "Missing thread_id in URL path")
    tid_err = validate_thread_id(thread_id)
    if tid_err:
        return _error_response(400, tid_err)
    status = authorization_status(
        authorize,
        AuthorizationContext(AuthorizationAction.STATE_READ, reg.name, thread_id, req),
    )
    if status is not None:
        return _error_response(status, "Access denied")

    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    try:
        snapshot = reg.graph.get_state(config)
    except (KeyError, ValueError):
        logger.warning("Graph %s: thread %s not found", reg.name, thread_id)
        return _error_response(404, f"Thread {thread_id!r} not found")
    except Exception:
        logger.exception("Graph %s get_state failed for thread %s", reg.name, thread_id)
        return _error_response(500, "Internal error while retrieving thread state")

    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    next_nodes: list[str] = list(snapshot.next) if hasattr(snapshot, "next") else []
    metadata = (
        dict(snapshot.metadata) if hasattr(snapshot, "metadata") and snapshot.metadata else None
    )

    return _state_response(values, next_nodes, metadata)


async def handle_state_async(
    req: func.HttpRequest,
    reg: Any,
    authorize: ThreadAuthorizer | None = None,
) -> func.HttpResponse:
    if not isinstance(reg.graph, AsyncStatefulGraph):
        return _error_response(409, f"Graph {reg.name!r} does not support state retrieval")

    thread_id = req.route_params.get("thread_id")
    if not thread_id:
        return _error_response(400, "Missing thread_id in URL path")
    tid_err = validate_thread_id(thread_id)
    if tid_err:
        return _error_response(400, tid_err)
    status = await authorization_status_async(
        authorize,
        AuthorizationContext(AuthorizationAction.STATE_READ, reg.name, thread_id, req),
    )
    if status is not None:
        return _error_response(status, "Access denied")

    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await reg.graph.aget_state(config)
    except (KeyError, ValueError):
        logger.warning("Graph %s: thread %s not found", reg.name, thread_id)
        return _error_response(404, f"Thread {thread_id!r} not found")
    except Exception:
        logger.exception("Graph %s aget_state failed for thread %s", reg.name, thread_id)
        return _error_response(500, "Internal error while retrieving thread state")

    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    next_nodes: list[str] = list(snapshot.next) if hasattr(snapshot, "next") else []
    metadata = (
        dict(snapshot.metadata) if hasattr(snapshot, "metadata") and snapshot.metadata else None
    )
    return _state_response(values, next_nodes, metadata)


def _state_response(
    values: dict[str, Any], next_nodes: list[str], metadata: dict[str, Any] | None
) -> func.HttpResponse:
    response = StateResponse(values=values, next=next_nodes, metadata=metadata)
    try:
        body = dumps(response.model_dump(), allow_nan=False)
    except (TypeError, ValueError):
        logger.exception("Thread state could not be serialized as JSON")
        return _error_response(500, "Thread state could not be serialized as JSON")
    return func.HttpResponse(body=body, mimetype="application/json", status_code=200)
