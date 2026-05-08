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
import threading
from typing import Any

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
from azure_functions_langgraph.protocols import StatefulGraph, StreamableGraph

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
    return thread_id, None


def _error_response(status_code: int, detail: str) -> func.HttpResponse:
    body = ErrorResponse(error="error", detail=detail)
    return func.HttpResponse(
        body=body.model_dump_json(),
        mimetype="application/json",
        status_code=status_code,
    )


# Per-thread in-memory locks for native endpoints with checkpointed graphs.
# Keyed by (graph_name, thread_id). Protects single-writer checkpointers
# (e.g. AzureBlobCheckpointSaver) from concurrent writes within the SAME
# Python worker process only.  This is NOT a distributed lock — multiple
# Function App instances or worker processes are not coordinated.
# For distributed run locking, use Platform-compatible runs with
# AzureTableThreadStore (ETag CAS).
_thread_locks: dict[tuple[str, str], threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _acquire_thread_lock(graph_name: str, thread_id: str) -> bool:
    """Try to acquire a per-thread lock. Returns False if already held."""
    with _thread_locks_guard:
        lock = _thread_locks.setdefault((graph_name, thread_id), threading.Lock())
        return lock.acquire(blocking=False)


def _release_thread_lock(graph_name: str, thread_id: str) -> None:
    """Release a per-thread lock and remove it from the dict if unused."""
    key = (graph_name, thread_id)
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
    if lock is None:
        return
    lock.release()
    # Clean up to prevent unbounded growth in long-lived workers.
    # Re-check under guard: only remove if the lock is not currently held
    # (another request may have acquired it between release and this check).
    with _thread_locks_guard:
        current = _thread_locks.get(key)
        if current is lock and not lock.locked():
            _thread_locks.pop(key, None)


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
        except Exception:
            pass

    # dataclass
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        try:
            return dataclasses.asdict(result)
        except Exception:
            pass

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
    max_request_body_bytes: int,
    max_input_depth: int,
    max_input_nodes: int,
) -> func.HttpResponse:
    """Handle a synchronous invoke request."""
    # Body size check — reject before parsing
    raw_body = req.get_body()
    size_err = validate_body_size(raw_body, max_request_body_bytes)
    if size_err:
        return _error_response(400, size_err)

    try:
        body = req.get_json()
    except ValueError:
        return _error_response(400, "Invalid JSON body")

    try:
        request = InvokeRequest.model_validate(body)
    except Exception as exc:
        return _error_response(422, f"Validation error: {exc}")

    # Structural validation on user-supplied fields
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

    config = request.config or {}
    thread_id, cfg_err = _extract_thread_id(config)
    if cfg_err:
        return _error_response(400, cfg_err)
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    locked = False
    if has_cp and thread_id:
        locked = _acquire_thread_lock(reg.name, thread_id)
        if not locked:
            return _error_response(
                409, f"Thread {thread_id!r} is currently in use by another request"
            )
    try:
        result = reg.graph.invoke(request.input, config=config)
    except Exception as exc:
        logger.exception("Graph %s invoke failed", reg.name)
        _ = exc
        return _error_response(500, "Graph execution failed")
    finally:
        if locked and thread_id is not None:
            _release_thread_lock(reg.name, thread_id)

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
    max_stream_response_bytes: int,
    max_request_body_bytes: int,
    max_input_depth: int,
    max_input_nodes: int,
) -> func.HttpResponse:
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

    # Body size check — reject before parsing
    raw_body = req.get_body()
    size_err = validate_body_size(raw_body, max_request_body_bytes)
    if size_err:
        return _error_response(400, size_err)

    try:
        body = req.get_json()
    except ValueError:
        return _error_response(400, "Invalid JSON body")

    try:
        request = StreamRequest.model_validate(body)
    except Exception as exc:
        return _error_response(422, f"Validation error: {exc}")

    # Structural validation on user-supplied fields
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

    config = request.config or {}
    thread_id, cfg_err = _extract_thread_id(config)
    if cfg_err:
        return _error_response(400, cfg_err)
    has_cp = getattr(reg.graph, "checkpointer", None) is not None
    locked = False
    if has_cp and thread_id:
        locked = _acquire_thread_lock(reg.name, thread_id)
        if not locked:
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
        if locked and thread_id is not None:
            _release_thread_lock(reg.name, thread_id)

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
