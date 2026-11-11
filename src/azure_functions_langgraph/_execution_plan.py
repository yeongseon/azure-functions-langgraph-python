"""Transport-neutral request-to-execution planning helpers (#861).

The classic ``HttpRequest``/``HttpResponse`` adapter (``_handlers.py``) and the
FastAPI true-streaming adapter (``streaming.py``) previously carried byte-for-byte
copies of the thread-id, version-negotiation, configuration and optional-model
rules. Those rules live here once, free of any transport types, so a new route
surface cannot drift from them. Each adapter keeps its own request decoding,
error-response encoding, locking lifecycle and result delivery.

Everything here is private; nothing is exported from the package.
"""

from __future__ import annotations

import inspect
from typing import Any, NamedTuple

from langgraph.checkpoint.base import BaseCheckpointSaver

from azure_functions_langgraph._endpoint import _is_model_type
from azure_functions_langgraph._validation import (
    validate_input_structure,
    validate_native_config,
    validate_thread_id,
)

VERSION_UNSUPPORTED_STATUS = 422


class RunPlan(NamedTuple):
    """Admission outcome for a run's execution config.

    ``config`` is the client-supplied config (``{}`` when absent), ``thread_id``
    the validated thread identity (or ``None``) and ``error`` a 400-class message
    when admission failed (in which case ``thread_id`` is ``None``).
    """

    config: dict[str, Any]
    thread_id: str | None
    error: str | None


def extract_thread_id(config: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract ``thread_id`` from *config*, returning ``(thread_id, error_message)``.

    Returns ``(None, None)`` when ``configurable`` is absent or has no thread_id.
    Returns ``(None, error)`` when ``configurable`` or ``thread_id`` is malformed.
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


def require_checkpoint_thread_id(checkpointer: Any, thread_id: str | None) -> str | None:
    """Return an error message if a checkpointed graph is run without a thread."""
    if isinstance(checkpointer, BaseCheckpointSaver) and thread_id is None:
        return "config.configurable.thread_id is required for checkpointed graphs"
    return None


def validated_run_thread(
    config: dict[str, Any], checkpointer: Any
) -> tuple[str | None, str | None]:
    """Validate the thread identity of a run against the graph's checkpointer."""
    thread_id, error = extract_thread_id(config)
    if error:
        return None, error
    return thread_id, require_checkpoint_thread_id(checkpointer, thread_id)


def plan_run_config(request_config: dict[str, Any] | None, graph: Any) -> RunPlan:
    """Build the execution config and thread identity for a native run.

    Client config structure/policy admission has already happened at parse time;
    this step only derives the thread identity and enforces the checkpointer rule.
    """
    config = request_config or {}
    thread_id, error = validated_run_thread(config, getattr(graph, "checkpointer", None))
    if error:
        return RunPlan(config, None, error)
    return RunPlan(config, thread_id, None)


def method_accepts_kwarg(method: Any, name: str) -> bool:
    """Return ``True`` if *method* accepts a keyword argument called *name*.

    Matches an explicit parameter named *name* or a ``**kwargs`` catch-all;
    returns ``False`` when the signature cannot be introspected (e.g. some
    C-level callables). Native/Streaming use it for ``version`` and Platform
    for ``context``; only the probe is shared, each surface keeps its own
    request field, status/message and forwarding rule.
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.name == name or param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def method_accepts_version(method: Any) -> bool:
    """Return ``True`` if *method* accepts a ``version`` keyword argument."""
    return method_accepts_kwarg(method, "version")


def negotiate_version(
    method: Any, version: str | None, graph_name: str
) -> tuple[dict[str, str], str | None]:
    """Resolve a caller's ``version`` into ``(forwardable_kwargs, error_message)``.

    ``({}, None)`` when no version was requested, ``({"version": v}, None)`` when
    it can be forwarded, or ``({}, message)`` (a 422 for the adapter) when the
    target graph method does not accept one. Capability is detected
    structurally so any ``LangGraphLike`` graph works.
    """
    if version is None:
        return {}, None
    if not method_accepts_version(method):
        return {}, (
            f"Graph {graph_name!r} does not accept a 'version' argument (requires langgraph>=1.1)"
        )
    return {"version": version}, None


def validate_optional_model(model: type[Any] | None, label: str) -> None:
    """Reject a non-``BaseModel`` *model* early, unless it is ``None``.

    ``request_model``/``response_model`` are wrapped in the transport envelope
    for metadata generation (issue #349), and the wrapper silently falls back to
    the generic envelope for non-model inputs. Validating at ``register()`` keeps
    the failure fast and close to the caller.
    """
    if model is not None and not _is_model_type(model):
        raise TypeError(f"{label} must be a Pydantic BaseModel subclass, got {model!r}")


class Rejection(NamedTuple):
    """A pre-execution rejection: HTTP status plus client-safe detail."""

    status: int
    detail: str


def admit_request_structure(
    request: Any,
    *,
    max_depth: int,
    max_nodes: int,
    allowed_configurable_keys: frozenset[str],
) -> Rejection | None:
    """Admit a validated invoke/stream model's ``input`` and ``config`` payloads.

    Enforces structural depth/size limits on both payloads and the native
    config policy (only approved ``config.configurable`` keys). Returns a
    ``Rejection`` (always 400) or ``None`` when the request is admissible.
    """
    structure_err = validate_input_structure(
        request.input, max_depth=max_depth, max_nodes=max_nodes
    )
    if structure_err:
        return Rejection(400, structure_err)
    if request.config:
        config_err = validate_input_structure(
            request.config, max_depth=max_depth, max_nodes=max_nodes
        )
        if config_err:
            return Rejection(400, config_err)
        policy_err = validate_native_config(request.config, allowed_configurable_keys)
        if policy_err:
            return Rejection(400, policy_err)
    return None
