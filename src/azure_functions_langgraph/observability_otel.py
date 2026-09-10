"""Optional OpenTelemetry :class:`RunObserver` (issue #434).

This module layers an OpenTelemetry-backed observer on top of the
dependency-free run-observability contract in
:mod:`azure_functions_langgraph.observability`. It is gated behind the
``otel`` extra::

    pip install azure-functions-langgraph[otel]

Design boundary — the package owns the **domain signal only**:

- It creates spans from the *safe* :class:`RunContext` correlation fields and
  nothing else (no input, output, config, headers, or secrets).
- It does **not** configure a global ``TracerProvider``, exporters, sampling,
  or resource attributes. The operator owns all SDK/exporter wiring; this
  observer merely acquires a tracer via :func:`opentelemetry.trace.get_tracer`
  and emits spans onto whatever provider the operator installed.

Span lifetime is threaded across two separate lifecycle callbacks
(``on_run_started`` starts a span; the terminal event ends it), keyed by
``run_id`` — exactly the error-prone glue this observer exists to remove.
"""

from __future__ import annotations

import threading

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.util.types import AttributeValue

from azure_functions_langgraph.observability import (
    RunContext,
    RunRejectedReason,
    _duration_ms,
)

# Instrumentation scope name for the tracer acquired by default. Operators can
# filter/route spans by this scope in their exporter configuration.
INSTRUMENTATION_NAME = "azure_functions_langgraph"

# Attribute namespace for every span attribute this observer sets, so operators
# get a stable, greppable, collision-free prefix in their backend.
_ATTR_PREFIX = "langgraph"


def _safe_attributes(ctx: RunContext) -> dict[str, AttributeValue]:
    """Build the OTel attribute set from *only* safe correlation fields.

    ``None`` values are dropped (OpenTelemetry attributes cannot be ``None``)
    and the immutable ``stream_mode`` tuple is coerced to a list so it is a
    valid homogeneous attribute sequence.
    """
    stream_mode: AttributeValue | None
    if isinstance(ctx.stream_mode, tuple):
        stream_mode = list(ctx.stream_mode)
    else:
        stream_mode = ctx.stream_mode

    raw: dict[str, AttributeValue | None] = {
        f"{_ATTR_PREFIX}.graph_name": ctx.graph_name,
        f"{_ATTR_PREFIX}.endpoint": ctx.endpoint,
        f"{_ATTR_PREFIX}.run_id": ctx.run_id,
        f"{_ATTR_PREFIX}.thread_id": ctx.thread_id,
        f"{_ATTR_PREFIX}.assistant_id": ctx.assistant_id,
        f"{_ATTR_PREFIX}.invocation_id": ctx.invocation_id,
        f"{_ATTR_PREFIX}.stream_mode": stream_mode,
        f"{_ATTR_PREFIX}.transport": ctx.transport,
        f"{_ATTR_PREFIX}.has_checkpointer": ctx.has_checkpointer,
        f"{_ATTR_PREFIX}.lock_backend": ctx.lock_backend,
    }
    return {key: value for key, value in raw.items() if value is not None}


class OTelRunObserver:
    """A :class:`RunObserver` that maps run lifecycle events to OTel spans.

    One span is produced per run:

    - :meth:`on_run_started` starts a span and stores it keyed by ``run_id``.
    - :meth:`on_run_completed` ends that span with ``StatusCode.OK``.
    - :meth:`on_run_failed` records the exception (class/type only — never a
      payload), sets ``StatusCode.ERROR``, and ends the span.
    - :meth:`on_run_rejected` — which has no preceding ``on_run_started`` — opens
      and immediately ends a short span marked with the rejection reason.

    All span attributes come from :func:`_safe_attributes`; a terminal
    ``langgraph.duration_ms`` and ``langgraph.status`` are added on close.

    Args:
        tracer: An OpenTelemetry :class:`~opentelemetry.trace.Tracer` to emit
            on. Defaults to ``trace.get_tracer(INSTRUMENTATION_NAME)`` resolved
            against whatever ``TracerProvider`` the operator has installed.
        span_name_prefix: Prefix for span names; the span is named
            ``"{span_name_prefix}.{endpoint}"`` (e.g. ``"langgraph.run.invoke"``).
    """

    def __init__(
        self,
        tracer: Tracer | None = None,
        *,
        span_name_prefix: str = "langgraph.run",
    ) -> None:
        self._tracer: Tracer = tracer or trace.get_tracer(INSTRUMENTATION_NAME)
        self._span_name_prefix = span_name_prefix
        # run_id -> live span, guarded so concurrent runs never corrupt the map.
        self._spans: dict[str, Span] = {}
        self._lock = threading.Lock()

    def _span_name(self, ctx: RunContext) -> str:
        return f"{self._span_name_prefix}.{ctx.endpoint}"

    def on_run_started(self, ctx: RunContext) -> None:
        span = self._tracer.start_span(
            self._span_name(ctx),
            attributes=_safe_attributes(ctx),
        )
        with self._lock:
            self._spans[ctx.run_id] = span

    def _pop_span(self, run_id: str) -> Span | None:
        with self._lock:
            return self._spans.pop(run_id, None)

    def _finalize(self, span: Span, ctx: RunContext, status: str) -> None:
        duration = _duration_ms(ctx)
        if duration is not None:
            span.set_attribute(f"{_ATTR_PREFIX}.duration_ms", duration)
        span.set_attribute(f"{_ATTR_PREFIX}.status", status)

    def on_run_completed(self, ctx: RunContext) -> None:
        span = self._pop_span(ctx.run_id)
        if span is None:
            return
        self._finalize(span, ctx, "completed")
        span.set_status(Status(StatusCode.OK))
        span.end()

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        span = self._pop_span(ctx.run_id)
        if span is None:
            return
        self._finalize(span, ctx, "failed")
        span.set_attribute(f"{_ATTR_PREFIX}.error_type", type(exc).__name__)
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
        span.end()

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None:
        # Rejections never emit on_run_started, so open a self-contained span.
        span = self._tracer.start_span(
            self._span_name(ctx),
            attributes=_safe_attributes(ctx),
        )
        self._finalize(span, ctx, "rejected")
        span.set_attribute(f"{_ATTR_PREFIX}.reject_reason", reason)
        span.set_status(Status(StatusCode.ERROR, f"rejected: {reason}"))
        span.end()
