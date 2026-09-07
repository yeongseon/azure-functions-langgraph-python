"""Run-observer example - Azure Functions entry point.

Demonstrates the run-lifecycle observability contract (issue #425): a pluggable
:class:`RunObserver` is registered on ``LangGraphApp`` and notified across every
native invoke/stream run (started / completed / failed / rejected).

The observer below logs only **correlation identifiers and timing** — never
input, output, config, or secrets — mirroring the contract's no-payload
guarantee. Concrete exporters (Application Insights, OpenTelemetry) layer on top
of this same contract and are intentionally out of scope here.

Run from this directory:
    func start
"""

from __future__ import annotations

import logging

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.observability import RunContext, RunRejectedReason

logger = logging.getLogger("run_observer.example")


class LoggingRunObserver:
    """A minimal :class:`RunObserver` that logs run lifecycle events.

    Emits only correlation identifiers (graph name, endpoint, run id, thread id)
    and elapsed time — never request/response payloads, config, or secrets — so
    the logs are safe to ship to any sink.
    """

    @staticmethod
    def _elapsed_ms(ctx: RunContext) -> float | None:
        if ctx.ended_at_ns is None:
            return None
        return (ctx.ended_at_ns - ctx.started_at_ns) / 1_000_000

    def on_run_started(self, ctx: RunContext) -> None:
        logger.info(
            "run.started graph=%s endpoint=%s run_id=%s thread_id=%s",
            ctx.graph_name,
            ctx.endpoint,
            ctx.run_id,
            ctx.thread_id,
        )

    def on_run_completed(self, ctx: RunContext) -> None:
        logger.info(
            "run.completed graph=%s endpoint=%s run_id=%s thread_id=%s elapsed_ms=%s",
            ctx.graph_name,
            ctx.endpoint,
            ctx.run_id,
            ctx.thread_id,
            self._elapsed_ms(ctx),
        )

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        logger.error(
            "run.failed graph=%s endpoint=%s run_id=%s thread_id=%s elapsed_ms=%s error=%s",
            ctx.graph_name,
            ctx.endpoint,
            ctx.run_id,
            ctx.thread_id,
            self._elapsed_ms(ctx),
            type(exc).__name__,
        )

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None:
        logger.warning(
            "run.rejected graph=%s endpoint=%s run_id=%s thread_id=%s reason=%s",
            ctx.graph_name,
            ctx.endpoint,
            ctx.run_id,
            ctx.thread_id,
            reason,
        )


langgraph_app = LangGraphApp(observer=LoggingRunObserver())
langgraph_app.register(
    graph=compiled_graph,
    name="run_observer",
    description="A deterministic agent with a logging RunObserver wired in (issue #425)",
)

app = langgraph_app.function_app
