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


def new_run_context(
    graph_name: str, endpoint: EndpointName, *, thread_id: str | None = None
) -> RunContext:
    """Create a fresh :class:`RunContext` with a unique run id and start time."""
    from uuid import uuid4

    return RunContext(
        graph_name=graph_name,
        endpoint=endpoint,
        run_id=uuid4().hex,
        thread_id=thread_id,
        started_at_ns=time.monotonic_ns(),
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
