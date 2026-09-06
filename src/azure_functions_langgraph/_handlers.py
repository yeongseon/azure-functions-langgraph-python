"""Internal request handlers for native graph endpoints.

These are standalone functions extracted from ``LangGraphApp`` to keep
``app.py`` focused on registration and route wiring.  Each function
receives only the explicit dependencies it needs — no reference to
``LangGraphApp`` itself.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
from typing import Any, Protocol, TypeVar

import azure.functions as func

from azure_functions_langgraph._validation import (
    validate_body_size,
    validate_input_structure,
    validate_thread_id,
)
from azure_functions_langgraph.contracts import (
    ErrorResponse,
    InvokeRequest,
    InvokeResponse,
    StateResponse,
    StreamRequest,
)
from azure_functions_langgraph.locks import ThreadLock
from azure_functions_langgraph.protocols import (
    AsyncStreamableGraph,
    StatefulGraph,
    StreamableGraph,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Shared helper
# ------------------------------------------------------------------

def _extract_thread_id(config: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract thread_id from config, returning (thread_id, error_message).

    Returns ``(None, None)`` when configurable is absent or has no thread_id.
    Returns ``(None, error_msg)`` when configurable or thread_id has a wrong type.
    """
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


def _error_response(status_code: int, detail: str) -> func.HttpResponse:
    body = ErrorResponse(error="error", detail=detail)
    return func.HttpResponse(
        body=body.model_dump_json(),
        mimetype="application/json",
        status_code=status_code,
    )


def _method_accepts_version(method: Any) -> bool:
    """Return ``True`` if *method* accepts a ``version`` keyword argument.

    Used to decide whether a caller-supplied ``version`` can be forwarded to a
    structural graph. Matches an explicit ``version`` parameter or a
    ``**kwargs`` catch-all; returns ``False`` when the signature cannot be
    introspected (e.g. some C-level callables).
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.name == "version" or param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _resolve_version_kwarg(
    method: Any, version: str | None, graph_name: str
) -> dict[str, str] | func.HttpResponse:
    """Resolve the caller's ``version`` request into forwardable kwargs.

    Returns ``{}`` when no version was requested, ``{"version": version}`` when
    it can be forwarded, or a ``422`` error response when the caller asked for a
    version but the target graph method does not accept one. Capability is
    detected structurally so any ``LangGraphLike`` graph works without requiring
    ``version`` on the base protocol.
    """
    if version is None:
        return {}
    if not _method_accepts_version(method):
        return _error_response(
            422,
            f"Graph {graph_name!r} does not accept a 'version' argument "
            "(requires langgraph>=1.1)",
        )
    return {"version": version}

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
        return _error_response(400, size_err)

    try:
        body = req.get_json()
    except ValueError:
        return _error_response(400, "Invalid JSON body")

    try:
        request = model.model_validate(body)
    except Exception as exc:
        return _error_response(422, f"Validation error: {exc}")

    structure_err = validate_input_structure(
        request.input,
        max_depth=max_input_depth,
        max_nodes=max_input_nodes,
    )
    if structure_err:
        return _error_response(400, structure_err)
    if request.config:
        config_err = validate_input_structure(
            request.config,
            max_depth=max_input_depth,
            max_nodes=max_input_nodes,
        )
        if config_err:
            return _error_response(400, config_err)
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
) -> func.HttpResponse:
    """Handle a synchronous invoke request."""
    parsed = _parse_native_request(
        req,
        InvokeRequest,
        max_request_body_bytes=max_request_body_bytes,
        max_input_depth=max_input_depth,
        max_input_nodes=max_input_nodes,
    )
    if isinstance(parsed, func.HttpResponse):
        return parsed
    request = parsed

    config = request.config or {}
    thread_id, cfg_err = _extract_thread_id(config)
    if cfg_err:
        return _error_response(400, cfg_err)
    version_kwargs = _resolve_version_kwarg(reg.graph.invoke, request.version, reg.name)
    if isinstance(version_kwargs, func.HttpResponse):
        return version_kwargs
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    lock_token: str | None = None
    if has_cp and thread_id:
        lock_token = thread_lock.acquire(reg.name, thread_id)
        if not lock_token:
            return _error_response(
                409, f"Thread {thread_id!r} is currently in use by another request"
            )
    try:
        result = reg.graph.invoke(request.input, config=config, **version_kwargs)
    except Exception as exc:
        logger.exception("Graph %s invoke failed", reg.name)
        _ = exc
        return _error_response(500, "Graph execution failed")
    finally:
        if lock_token is not None and thread_id is not None:
            thread_lock.release(reg.name, thread_id, lock_token)

    output = _serialize_graph_output(result)
    response = InvokeResponse(output=output)
    return func.HttpResponse(
        body=response.model_dump_json(),
        mimetype="application/json",
        status_code=200,
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
) -> func.HttpResponse:
    """Handle a streaming request.

    Returns a **buffered** SSE-formatted response.  All stream chunks are
    collected first, then returned in a single HTTP response.  This is a
    known v0.1 limitation, tracked in issue #378 — true chunked streaming will
    follow once Azure Functions Python HTTP streaming is fully stable. See the
    "Streaming: buffered SSE and the true-streaming migration" section of
    ``DESIGN.md`` for the constraints and migration path.
    """
    if not reg.stream_enabled:
        return _error_response(501, f"Graph {reg.name!r} is configured as invoke-only")

    if not isinstance(reg.graph, StreamableGraph):
        return _error_response(501, f"Graph {reg.name!r} does not support streaming")

    parsed = _parse_native_request(
        req,
        StreamRequest,
        max_request_body_bytes=max_request_body_bytes,
        max_input_depth=max_input_depth,
        max_input_nodes=max_input_nodes,
    )
    if isinstance(parsed, func.HttpResponse):
        return parsed
    request = parsed

    config = request.config or {}
    thread_id, cfg_err = _extract_thread_id(config)
    if cfg_err:
        return _error_response(400, cfg_err)
    version_kwargs = _resolve_version_kwarg(reg.graph.stream, request.version, reg.name)
    if isinstance(version_kwargs, func.HttpResponse):
        return version_kwargs
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    lock_token: str | None = None
    if has_cp and thread_id:
        lock_token = thread_lock.acquire(reg.name, thread_id)
        if not lock_token:
            return _error_response(
                409, f"Thread {thread_id!r} is currently in use by another request"
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
            **version_kwargs,
        ):
            serialized = json.dumps(
                event if isinstance(event, dict) else {"data": str(event)},
                default=str,
                allow_nan=False,
            )
            if not _append_chunk(f"event: data\ndata: {serialized}\n\n"):
                break
    except Exception as exc:
        logger.exception("Graph %s stream failed", reg.name)
        _ = exc
        error_payload = json.dumps({"error": "stream processing failed"})
        _append_chunk(f"event: error\ndata: {error_payload}\n\n")
    finally:
        if lock_token is not None and thread_id is not None:
            thread_lock.release(reg.name, thread_id, lock_token)

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
) -> func.HttpResponse:
    """Handle an invoke request against an async graph via ``ainvoke``.

    Mirrors :func:`handle_invoke` exactly (validation, thread-lock, error, and
    response semantics) but awaits ``reg.graph.ainvoke`` and offloads the sync
    thread-lock calls to a worker thread so a blocking lock backend (e.g. the
    Azure Blob lease) never stalls the event loop.
    """
    parsed = _parse_native_request(
        req,
        InvokeRequest,
        max_request_body_bytes=max_request_body_bytes,
        max_input_depth=max_input_depth,
        max_input_nodes=max_input_nodes,
    )
    if isinstance(parsed, func.HttpResponse):
        return parsed
    request = parsed

    config = request.config or {}
    thread_id, cfg_err = _extract_thread_id(config)
    if cfg_err:
        return _error_response(400, cfg_err)
    version_kwargs = _resolve_version_kwarg(reg.graph.ainvoke, request.version, reg.name)
    if isinstance(version_kwargs, func.HttpResponse):
        return version_kwargs
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    lock_token: str | None = None
    if has_cp and thread_id:
        lock_token = await asyncio.to_thread(thread_lock.acquire, reg.name, thread_id)
        if not lock_token:
            return _error_response(
                409, f"Thread {thread_id!r} is currently in use by another request"
            )
    try:
        result = await reg.graph.ainvoke(request.input, config=config, **version_kwargs)
    except Exception as exc:
        logger.exception("Graph %s ainvoke failed", reg.name)
        _ = exc
        return _error_response(500, "Graph execution failed")
    finally:
        if lock_token is not None and thread_id is not None:
            await asyncio.to_thread(thread_lock.release, reg.name, thread_id, lock_token)

    output = _serialize_graph_output(result)
    response = InvokeResponse(output=output)
    return func.HttpResponse(
        body=response.model_dump_json(),
        mimetype="application/json",
        status_code=200,
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
) -> func.HttpResponse:
    """Handle a streaming request against an async graph via ``astream``.

    Async counterpart of :func:`handle_stream`: returns the same **buffered**
    SSE response (issue #378) built by consuming ``reg.graph.astream`` with
    ``async for``. The byte-size cap is enforced mid-stream, and the thread
    lock is released in ``finally`` even when the async generator raises.
    """
    if not reg.stream_enabled:
        return _error_response(501, f"Graph {reg.name!r} is configured as invoke-only")

    if not isinstance(reg.graph, AsyncStreamableGraph):
        return _error_response(501, f"Graph {reg.name!r} does not support streaming")

    parsed = _parse_native_request(
        req,
        StreamRequest,
        max_request_body_bytes=max_request_body_bytes,
        max_input_depth=max_input_depth,
        max_input_nodes=max_input_nodes,
    )
    if isinstance(parsed, func.HttpResponse):
        return parsed
    request = parsed

    config = request.config or {}
    thread_id, cfg_err = _extract_thread_id(config)
    if cfg_err:
        return _error_response(400, cfg_err)
    version_kwargs = _resolve_version_kwarg(reg.graph.astream, request.version, reg.name)
    if isinstance(version_kwargs, func.HttpResponse):
        return version_kwargs
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    lock_token: str | None = None
    if has_cp and thread_id:
        lock_token = await asyncio.to_thread(thread_lock.acquire, reg.name, thread_id)
        if not lock_token:
            return _error_response(
                409, f"Thread {thread_id!r} is currently in use by another request"
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
                    )
                }
            )
            chunks.append(f"event: error\ndata: {error_payload}\n\n")
            return False
        chunks.append(chunk)
        buffered_bytes += chunk_bytes
        return True

    try:
        async for event in reg.graph.astream(
            request.input,
            config=config,
            stream_mode=request.stream_mode,
            **version_kwargs,
        ):
            serialized = json.dumps(
                event if isinstance(event, dict) else {"data": str(event)},
                default=str,
                allow_nan=False,
            )
            if not _append_chunk(f"event: data\ndata: {serialized}\n\n"):
                break
    except Exception as exc:
        logger.exception("Graph %s astream failed", reg.name)
        _ = exc
        error_payload = json.dumps({"error": "stream processing failed"})
        _append_chunk(f"event: error\ndata: {error_payload}\n\n")
    finally:
        if lock_token is not None and thread_id is not None:
            await asyncio.to_thread(thread_lock.release, reg.name, thread_id, lock_token)

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

# ------------------------------------------------------------------
# State handler
# ------------------------------------------------------------------


def handle_state(
    req: func.HttpRequest,
    reg: Any,
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

    response = StateResponse(values=values, next=next_nodes, metadata=metadata)
    return func.HttpResponse(
        body=json.dumps(response.model_dump(), default=str),
        mimetype="application/json",
        status_code=200,
    )
