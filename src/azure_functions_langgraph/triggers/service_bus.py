"""Azure Service Bus trigger adapter for LangGraph graphs (issue #409).

This module lets a compiled LangGraph graph be driven by an **Azure Service Bus**
queue or topic-subscription message instead of (or in addition to) the native
HTTP endpoints, without hand-writing the trigger glue for each project.

Design boundaries (issue #409):

- **No exactly-once semantics.** Service Bus delivery is at-least-once; a graph
  invocation may run more than once for the same logical message (redelivery
  after lock expiry, failover, etc.). Graphs that mutate external state must be
  idempotent. This adapter does **not** add dedup.
- **A message is not implicitly a thread.** By default runs are threadless; the
  operator opts into checkpointed conversations by supplying a
  ``thread_id_factory``. When a ``thread_id`` is derived *and* the graph has a
  checkpointer, the adapter reuses the package's existing
  :class:`~azure_functions_langgraph.locks.base.ThreadLock` so concurrent
  deliveries for the same logical thread cannot mutate checkpoints
  concurrently — a redelivery that collides with an in-flight run raises so the
  broker retries.
- **Graph exceptions are never swallowed.** A failed graph run propagates so the
  Service Bus runtime abandons/dead-letters the message per its retry policy.
- **No telemetry payload leakage.** Only correlation identifiers and timing are
  observed; message bodies and application-property values are never recorded.
- **No output publishing.** Emitting results back to Service Bus (or elsewhere)
  is left to a caller-supplied ``result_handler``; this adapter ships no output
  binding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from azure_functions_langgraph.locks import ThreadLock
from azure_functions_langgraph.observability import (
    NOOP_OBSERVER,
    RunObserver,
    finish_context,
    new_run_context,
    safe_observer_call,
)

# The generic trigger label stamped onto every RunContext this adapter emits, so
# exporters can distinguish Service Bus runs from HTTP invoke/stream runs.
TRIGGER_TYPE = "service_bus"


@runtime_checkable
class ServiceBusMessageLike(Protocol):
    """Structural subset of :class:`azure.functions.ServiceBusMessage`.

    Only the members this adapter reads are declared, so tests (and alternative
    runtimes) can supply a lightweight fake without depending on the concrete
    Azure Functions message type. Every attribute mirrors the corresponding
    ``azure.functions.ServiceBusMessage`` member.
    """

    def get_body(self) -> bytes:
        """Return the raw message body as bytes."""
        ...

    @property
    def message_id(self) -> Optional[str]:
        """Broker-assigned unique message identifier, if present."""
        ...


# Mapper turning a raw Service Bus message into the ``input`` passed to the
# graph. Kept as a plain callable so operators can supply their own.
InputMapper = Callable[[ServiceBusMessageLike], Any]

# Optional factory deriving a ``thread_id`` (checkpointed conversation key) from
# a message. Returning ``None`` keeps the run threadless.
ThreadIdFactory = Callable[[ServiceBusMessageLike], Optional[str]]

# Optional sink invoked with ``(result, message)`` after a successful run. Its
# exceptions propagate (failing the message) so downstream delivery guarantees
# are the operator's to reason about.
ResultHandler = Callable[[Any, ServiceBusMessageLike], None]


def _decode_body(msg: ServiceBusMessageLike) -> str:
    """Decode a message body to text, tolerating invalid UTF-8 bytes."""
    return msg.get_body().decode("utf-8", errors="replace")


def default_message_mapper(msg: ServiceBusMessageLike) -> dict[str, Any]:
    """Map a Service Bus message body to a default LangGraph ``input`` dict.

    The body is decoded as UTF-8 (invalid bytes are replaced, never raised) and
    parsed as JSON:

    - a JSON **object** is passed straight through as the graph input;
    - any other JSON value, or non-JSON text, is wrapped as a single human
      message: ``{"messages": [{"role": "human", "content": <text>}]}``.

    This keeps the zero-config path useful for both structured producers (that
    already emit a graph-shaped payload) and plain-text producers, while letting
    operators override with a custom ``input_mapper`` when neither shape fits.
    """
    text = _decode_body(msg)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return {"messages": [{"role": "human", "content": text}]}


@dataclass
class _ServiceBusRegistration:
    """Internal registration record for a Service-Bus-triggered graph.

    Exactly one of ``queue_name`` or (``topic_name`` + ``subscription_name``)
    is set; :func:`~azure_functions_langgraph.app.LangGraphApp.register_service_bus`
    validates the combination before constructing this record.
    """

    graph: Any
    name: str
    connection: str
    queue_name: Optional[str] = None
    topic_name: Optional[str] = None
    subscription_name: Optional[str] = None
    input_mapper: InputMapper = default_message_mapper
    thread_id_factory: Optional[ThreadIdFactory] = None
    result_handler: Optional[ResultHandler] = None
    async_mode: bool = False
    binding_kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def is_topic(self) -> bool:
        """Whether this registration binds a topic subscription (vs a queue)."""
        return self.topic_name is not None


def _has_checkpointer(graph: Any) -> bool:
    """Whether a compiled graph has a checkpointer attached."""
    return getattr(graph, "checkpointer", None) is not None


class ThreadContentionError(RuntimeError):
    """Raised when a redelivery collides with an in-flight run for a thread.

    Propagated (never swallowed) so the Service Bus runtime abandons the message
    and the broker retries it after the current holder releases the lock. This
    preserves single-writer checkpoint safety without dropping the message.
    """




def process_service_bus_message(
    reg: _ServiceBusRegistration,
    msg: ServiceBusMessageLike,
    *,
    thread_lock: ThreadLock,
    observer: RunObserver = NOOP_OBSERVER,
) -> Any:
    """Drive a graph synchronously from one Service Bus message.

    Emits the observer lifecycle (started → completed/failed) around the graph
    ``invoke``. When a ``thread_id`` is derived and the graph is checkpointed, a
    non-blocking thread lock guards the run; contention raises
    :class:`ThreadContentionError` so the broker retries. Graph exceptions are
    re-raised unchanged (never swallowed). A supplied ``result_handler`` runs
    only on success, **outside** the observed block, so its failure does not
    emit a second terminal observer event — but it still propagates and fails
    the message.

    Returns the graph result so alternative drivers can post-process it.
    """
    graph_input = reg.input_mapper(msg)
    thread_id = reg.thread_id_factory(msg) if reg.thread_id_factory is not None else None
    ctx = new_run_context(
        reg.name,
        "invoke",
        thread_id=thread_id,
        invocation_id=msg.message_id,
        has_checkpointer=_has_checkpointer(reg.graph),
        lock_backend=type(thread_lock).__name__,
        trigger_type=TRIGGER_TYPE,
    )

    lock_token: str | None = None
    use_lock = bool(thread_id) and _has_checkpointer(reg.graph)
    if use_lock and thread_id is not None:
        lock_token = thread_lock.acquire(reg.name, thread_id)
        if not lock_token:
            raise ThreadContentionError(
                f"Thread {thread_id!r} for graph {reg.name!r} is currently in use; "
                "message will be retried by the broker"
            )

    config = {"configurable": {"thread_id": thread_id}} if thread_id else None
    safe_observer_call(observer, "on_run_started", ctx)
    try:
        if config is not None:
            result = reg.graph.invoke(graph_input, config=config)
        else:
            result = reg.graph.invoke(graph_input)
    except BaseException as exc:
        safe_observer_call(observer, "on_run_failed", finish_context(ctx), exc)
        raise
    else:
        safe_observer_call(observer, "on_run_completed", finish_context(ctx))
    finally:
        if lock_token is not None and thread_id is not None:
            thread_lock.release(reg.name, thread_id, lock_token)

    if reg.result_handler is not None:
        reg.result_handler(result, msg)
    return result


async def process_service_bus_message_async(
    reg: _ServiceBusRegistration,
    msg: ServiceBusMessageLike,
    *,
    thread_lock: ThreadLock,
    observer: RunObserver = NOOP_OBSERVER,
) -> Any:
    """Async counterpart of :func:`process_service_bus_message`.

    Awaits ``reg.graph.ainvoke`` and offloads the synchronous thread-lock calls
    to a worker thread so a blocking lock backend never stalls the event loop.
    All ordering, contention, and result-handler semantics match the sync path.
    """
    import asyncio

    graph_input = reg.input_mapper(msg)
    thread_id = reg.thread_id_factory(msg) if reg.thread_id_factory is not None else None
    ctx = new_run_context(
        reg.name,
        "invoke",
        thread_id=thread_id,
        invocation_id=msg.message_id,
        has_checkpointer=_has_checkpointer(reg.graph),
        lock_backend=type(thread_lock).__name__,
        trigger_type=TRIGGER_TYPE,
    )

    lock_token: str | None = None
    use_lock = bool(thread_id) and _has_checkpointer(reg.graph)
    if use_lock and thread_id is not None:
        lock_token = await asyncio.to_thread(thread_lock.acquire, reg.name, thread_id)
        if not lock_token:
            raise ThreadContentionError(
                f"Thread {thread_id!r} for graph {reg.name!r} is currently in use; "
                "message will be retried by the broker"
            )

    config = {"configurable": {"thread_id": thread_id}} if thread_id else None
    safe_observer_call(observer, "on_run_started", ctx)
    try:
        if config is not None:
            result = await reg.graph.ainvoke(graph_input, config=config)
        else:
            result = await reg.graph.ainvoke(graph_input)
    except BaseException as exc:
        safe_observer_call(observer, "on_run_failed", finish_context(ctx), exc)
        raise
    else:
        safe_observer_call(observer, "on_run_completed", finish_context(ctx))
    finally:
        if lock_token is not None and thread_id is not None:
            await asyncio.to_thread(thread_lock.release, reg.name, thread_id, lock_token)

    if reg.result_handler is not None:
        reg.result_handler(result, msg)
    return result
