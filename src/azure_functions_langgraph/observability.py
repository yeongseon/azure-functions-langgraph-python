"""Run-lifecycle observability contract for native invoke/stream handlers.

This module defines the *foundation* for run observability (issue #425): a
small, stable :class:`RunObserver` protocol plus an immutable
:class:`RunContext` carrying graph/thread/run correlation identifiers. It
deliberately emits **no request/response payloads, config, or secrets** — only
correlation identifiers and timing.

Concrete exporters (Application Insights, OpenTelemetry, KQL examples) are out
of scope here and are layered on top of this contract separately (issue #407).
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# The endpoint that produced a run. Kept as a ``Literal`` so downstream
# exporters can switch on a stable, closed set of values.
EndpointName = Literal["invoke", "stream"]

# The transport used to deliver a run's result. ``"buffered"`` is the only
# transport today (buffered SSE / single JSON response); ``"streaming"`` is
# reserved so opt-in true HTTP streaming (issue #406) can adopt this contract
# without a breaking change.
RunTransport = Literal["buffered", "streaming"]

# Why a run never reached graph execution. Every value corresponds to a
# pre-execution failure path in the native handlers; execution-time failures
# use :meth:`RunObserver.on_run_failed` instead.
RunRejectedReason = Literal[
    "invalid_request",
    "invalid_config",
    "unsupported_version",
    "streaming_unsupported",
    "lock_contention",
]


@dataclasses.dataclass(frozen=True, slots=True)
class RunContext:
    """Immutable correlation + timing context for a single native run.

    Carries only identifiers and monotonic timings — never input, output,
    config, or headers — so an observer cannot accidentally leak user payloads
    or secrets. ``ended_at_ns`` is ``None`` until a terminal event
    (completed/failed/rejected) is emitted.
    """

    graph_name: str
    endpoint: EndpointName
    run_id: str
    thread_id: str | None
    started_at_ns: int
    ended_at_ns: int | None = None

    # --- Optional safe correlation metadata (issue #407) -----------------
    # Every field below is defaulted so the context stays constructible from
    # positional identifiers alone, and each carries only non-sensitive
    # correlation data — never payloads, config, headers, or secrets.
    assistant_id: str | None = None
    invocation_id: str | None = None
    stream_mode: str | tuple[str, ...] | None = None
    transport: RunTransport = "buffered"
    has_checkpointer: bool | None = None
    lock_backend: str | None = None
    # Generic trigger classification (issue #409). ``None`` for HTTP invoke/stream
    # runs; set to e.g. ``"service_bus"`` for event-driven trigger adapters so
    # exporters can distinguish transport-agnostic run sources. Carries no
    # payload — only a stable, low-cardinality trigger label.
    trigger_type: str | None = None



@runtime_checkable
class RunObserver(Protocol):
    """Observer notified across a native run's lifecycle.

    Ordering invariant:

    - A run that passes all pre-execution validation emits exactly one
      :meth:`on_run_started` followed by exactly one terminal event
      (:meth:`on_run_completed` or :meth:`on_run_failed`).
    - A run rejected before graph execution (bad request/config/version,
      streaming unsupported, or lock contention) emits **only**
      :meth:`on_run_rejected` — with no prior ``on_run_started``.

    Implementations must be cheap and must not raise; the handlers isolate
    observer exceptions, but a well-behaved observer should not rely on that.
    """

    def on_run_started(self, ctx: RunContext) -> None: ...

    def on_run_completed(self, ctx: RunContext) -> None: ...

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None: ...

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None: ...


class NoOpRunObserver:
    """Default observer that does nothing.

    Used when no observer is registered on the app, so handlers can always call
    the observer unconditionally without ``None`` checks.
    """

    def on_run_started(self, ctx: RunContext) -> None:  # noqa: D102
        return None

    def on_run_completed(self, ctx: RunContext) -> None:  # noqa: D102
        return None

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:  # noqa: D102
        return None

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None:  # noqa: D102
        return None


# Shared stateless singleton used as the default when no observer is wired.
NOOP_OBSERVER: RunObserver = NoOpRunObserver()


def _normalize_stream_mode(
    stream_mode: str | list[str] | tuple[str, ...] | None,
) -> str | tuple[str, ...] | None:
    """Coerce a stream-mode value into an immutable, context-safe form.

    Lists are converted to tuples so :class:`RunContext` stays hashable/frozen;
    strings and ``None`` pass through unchanged.
    """
    if isinstance(stream_mode, list):
        return tuple(stream_mode)
    return stream_mode


def new_run_context(
    graph_name: str,
    endpoint: EndpointName,
    *,
    run_id: str | None = None,
    thread_id: str | None = None,
    assistant_id: str | None = None,
    invocation_id: str | None = None,
    stream_mode: str | list[str] | tuple[str, ...] | None = None,
    transport: RunTransport = "buffered",
    has_checkpointer: bool | None = None,
    lock_backend: str | None = None,
    trigger_type: str | None = None,
) -> RunContext:
    """Create a fresh :class:`RunContext` with a unique run id and start time.

    All keyword arguments are optional safe correlation metadata; unavailable
    fields simply stay ``None`` (or their default). Pass ``run_id`` to reuse an
    externally generated id (e.g. a Platform run id) instead of a new one.
    """
    from uuid import uuid4

    return RunContext(
        graph_name=graph_name,
        endpoint=endpoint,
        run_id=run_id or uuid4().hex,
        thread_id=thread_id,
        started_at_ns=time.monotonic_ns(),
        assistant_id=assistant_id,
        invocation_id=invocation_id,
        stream_mode=_normalize_stream_mode(stream_mode),
        transport=transport,
        has_checkpointer=has_checkpointer,
        lock_backend=lock_backend,
        trigger_type=trigger_type,
    )


def finish_context(ctx: RunContext) -> RunContext:
    """Return a copy of *ctx* stamped with a terminal ``ended_at_ns``."""
    return dataclasses.replace(ctx, ended_at_ns=time.monotonic_ns())


def safe_observer_call(observer: RunObserver, method: str, *args: object) -> None:
    """Invoke ``observer.<method>(*args)``, swallowing and logging any error.

    Observer bugs must never change HTTP behaviour or interfere with thread-lock
    release, so every emission site routes through here.
    """
    try:
        getattr(observer, method)(*args)
    except Exception:  # noqa: BLE001 - observer isolation is intentional
        logger.exception("RunObserver.%s raised; ignoring", method)


def _duration_ms(ctx: RunContext) -> float | None:
    """Return the run's elapsed wall time in milliseconds, if terminated."""
    if ctx.ended_at_ns is None:
        return None
    return (ctx.ended_at_ns - ctx.started_at_ns) / 1_000_000


def _safe_fields(ctx: RunContext, **extra: object) -> dict[str, object]:
    """Build the safe, structured field set logged for a run.

    Contains only correlation identifiers, timing, and run metadata — never
    input, output, config, headers, or secrets. Extra derived fields (status,
    error_type) are merged in by the caller.
    """
    fields: dict[str, object] = {
        "graph_name": ctx.graph_name,
        "endpoint": ctx.endpoint,
        "run_id": ctx.run_id,
        "thread_id": ctx.thread_id,
        "assistant_id": ctx.assistant_id,
        "invocation_id": ctx.invocation_id,
        "stream_mode": ctx.stream_mode,
        "transport": ctx.transport,
        "has_checkpointer": ctx.has_checkpointer,
        "lock_backend": ctx.lock_backend,
        "trigger_type": ctx.trigger_type,
        "duration_ms": _duration_ms(ctx),
    }
    fields.update(extra)
    return fields


class LoggingRunObserver:
    """A dependency-free :class:`RunObserver` backed by stdlib ``logging``.

    Emits one log record per lifecycle event carrying only safe correlation
    metadata (graph/run/thread/assistant ids, endpoint, transport, stream mode,
    checkpointer/lock backend, timing) plus a derived ``status`` — and, for
    failures, the exception ``error_type`` (class name only). It deliberately
    logs **no** request/response payloads, config, headers, or secrets.

    The observer owns neither log formatting nor handler/exporter configuration:
    it writes structured fields under a single ``extra`` key so operators can
    route them to Application Insights / OpenTelemetry / any sink via their own
    logging configuration (e.g. the companion ``azure-functions-logging``
    package). See ``examples/observability_app_insights/`` for a KQL walkthrough.

    Args:
        logger: Logger to emit on. Defaults to
            ``azure_functions_langgraph.observability.run``.
        level: Level for non-error events (started/completed/rejected).
            Failures are always logged at ``logging.ERROR``.
    """

    _EXTRA_KEY = "langgraph_run"

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        level: int = logging.INFO,
    ) -> None:
        self._logger = logger or logging.getLogger(f"{__name__}.run")
        self._level = level

    def _emit(
        self, level: int, message: str, ctx: RunContext, **extra: object
    ) -> None:
        self._logger.log(
            level,
            message,
            self._safe_message_args(ctx, extra),
            extra={self._EXTRA_KEY: _safe_fields(ctx, **extra)},
        )

    @staticmethod
    def _safe_message_args(ctx: RunContext, extra: dict[str, object]) -> dict[str, object]:
        # Compact human-readable summary interpolated into the log message.
        return {
            "graph_name": ctx.graph_name,
            "endpoint": ctx.endpoint,
            "run_id": ctx.run_id,
            **extra,
        }

    def on_run_started(self, ctx: RunContext) -> None:
        self._emit(self._level, "langgraph.run.started %s", ctx, status="started")

    def on_run_completed(self, ctx: RunContext) -> None:
        self._emit(self._level, "langgraph.run.completed %s", ctx, status="completed")

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self._emit(
            logging.ERROR,
            "langgraph.run.failed %s",
            ctx,
            status="failed",
            error_type=type(exc).__name__,
        )

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None:
        self._emit(
            self._level,
            "langgraph.run.rejected %s",
            ctx,
            status="rejected",
            reason=reason,
        )
