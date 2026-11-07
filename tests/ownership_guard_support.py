"""Shared fakes for the opt-in ownership-guard tests (#790).

Nothing here talks to Azure. The fakes are deliberately small so each test file
can state its scenario in a few lines:

* :class:`ScriptedGuard` -- an :class:`OwnershipGuard` that counts checks and can
  be told to trip, either on the Nth check or on demand from inside a "run".
* :class:`GuardableLock` -- a real in-process lock (real mutual exclusion) that
  also implements :class:`OwnershipGuardProvider` and records every token it
  hands out, releases and issues a guard for.
* :class:`RaisingProviderLock` -- a provider whose ``ownership_guard`` raises.
* :class:`ProbeGraph` -- a checkpointed fake graph exposing ``invoke`` /
  ``ainvoke`` / ``stream`` / ``astream`` / ``update_state`` that records the exact
  config object each call received.
* :class:`RecordingObserver` -- records the run lifecycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
import threading
from typing import Any

from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY,
    LeaseLossReason,
    OwnershipGuard,
    ThreadLeaseLostError,
    resolve_ownership_guard,
)
from azure_functions_langgraph.observability import NoOpRunObserver, RunContext, RunRejectedReason

__all__ = [
    "GuardableLock",
    "ProbeGraph",
    "RaisingProviderLock",
    "RecordingObserver",
    "ScriptedGuard",
    "guard_in",
]


def guard_in(config: Any) -> Any:
    """Return whatever a run config carries under the private guard key, or ``None``.

    Looks in ``config["configurable"]`` only, which is where the adapter puts it.
    """
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    return configurable.get(OWNERSHIP_GUARD_CONFIG_KEY)


class ScriptedGuard:
    """An :class:`OwnershipGuard` that counts checks and trips on command.

    Once tripped it stays tripped, like a real lock that has lost its lease.
    Counting and tripping are serialized by a private lock because LangGraph can
    drive checkpoint writes from several worker threads at once.
    """

    def __init__(
        self,
        *,
        trip_on: int | None = None,
        reason: LeaseLossReason = "lost",
        graph_name: str | None = "graph",
        thread_id: str | None = "thread",
    ) -> None:
        self.trip_on = trip_on
        self.reason: LeaseLossReason = reason
        self.graph_name = graph_name
        self.thread_id = thread_id
        self.checks = 0
        self._mutex = threading.Lock()

    def trip(self) -> None:
        """Make the *next* check (and every later one) raise."""
        with self._mutex:
            self.trip_on = self.checks + 1

    def check(self) -> None:
        with self._mutex:
            self.checks += 1
            tripped = self.trip_on is not None and self.checks >= self.trip_on
        if tripped:
            raise ThreadLeaseLostError(
                reason=self.reason, graph_name=self.graph_name, thread_id=self.thread_id
            )

    def __repr__(self) -> str:
        return "<OwnershipGuard>"


class GuardableLock(InProcessThreadLock):
    """Real in-process lock that also hands out :class:`ScriptedGuard` objects.

    Attributes:
        guards: Every guard issued, in order.
        issued: ``(graph_name, thread_id, token)`` for every guard issued.
        acquired: Every token returned by :meth:`acquire`.
        released: Every token passed to :meth:`release` (calls, not effects).
    """

    def __init__(
        self,
        *,
        trip_on: int | None = None,
        reason: LeaseLossReason = "lost",
        enabled: bool = True,
    ) -> None:
        super().__init__()
        self._trip_on = trip_on
        self._reason: LeaseLossReason = reason
        self._enabled = enabled
        self.guards: list[ScriptedGuard] = []
        self.issued: list[tuple[str, str, str]] = []
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        token = super().acquire(graph_name, thread_id, timeout)
        if token is not None:
            self.acquired.append(token)
        return token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.released.append(token)
        super().release(graph_name, thread_id, token)

    def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> OwnershipGuard | None:
        if not self._enabled:
            return None
        guard = ScriptedGuard(
            trip_on=self._trip_on,
            reason=self._reason,
            graph_name=graph_name,
            thread_id=thread_id,
        )
        self.guards.append(guard)
        self.issued.append((graph_name, thread_id, token))
        return guard


class RaisingProviderLock(InProcessThreadLock):
    """A provider whose ``ownership_guard`` raises, to prove the token is still released."""

    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        token = super().acquire(graph_name, thread_id, timeout)
        if token is not None:
            self.acquired.append(token)
        return token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.released.append(token)
        super().release(graph_name, thread_id, token)

    def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> OwnershipGuard | None:
        raise RuntimeError("guard provider failed")


class ProbeGraph:
    """Checkpointed fake graph recording the exact config each call received.

    ``during_run`` is called with the received config at the *end* of every
    execution (after the last chunk for the streaming methods), which is where a
    test can trip the guard to model "ownership was lost while the run executed".
    """

    def __init__(
        self,
        *,
        checkpointer: object | None = "checkpointer",
        output: dict[str, Any] | None = None,
        chunks: Sequence[dict[str, Any]] | None = None,
        during_run: Callable[[Any], None] | None = None,
    ) -> None:
        self.checkpointer = checkpointer
        self.output: dict[str, Any] = output if output is not None else {"ok": True}
        self.chunks: list[dict[str, Any]] = (
            list(chunks) if chunks is not None else [{"n": 1}, {"n": 2}, {"n": 3}]
        )
        self.during_run = during_run
        self.configs: list[Any] = []
        self.scoped_guards: list[OwnershipGuard | None] = []

    def _finish(self, config: Any) -> None:
        if self.during_run is not None:
            self.during_run(config)

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.configs.append(config)
        self._finish(config)
        return self.output

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.configs.append(config)
        self._finish(config)
        return self.output

    def stream(
        self, input: Any, config: Any = None, stream_mode: Any = "values", **kwargs: Any
    ) -> Iterator[dict[str, Any]]:
        self.configs.append(config)
        yield from self.chunks
        self._finish(config)

    async def astream(
        self, input: Any, config: Any = None, stream_mode: Any = "values", **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.configs.append(config)
        for chunk in self.chunks:
            yield chunk
        self._finish(config)

    def update_state(self, config: Any, values: Any, *, as_node: str | None = None) -> Any:
        self.configs.append(config)
        self.scoped_guards.append(resolve_ownership_guard(None))
        self._finish(config)
        return {"configurable": {"thread_id": "t", "checkpoint_ns": "", "checkpoint_id": "cp-1"}}


class RecordingObserver(NoOpRunObserver):
    """Records the run lifecycle; ``failures`` keeps the exact exception objects."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.failures: list[BaseException] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append("started")

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append("completed")

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self.events.append("failed")
        self.failures.append(exc)

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None:
        self.events.append(f"rejected:{reason}")
