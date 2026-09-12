"""The deterministic Durable *orchestrator* for async runs.

The orchestrator is intentionally trivial and **must remain deterministic**: it
reads the orchestration input, calls the single ``execute_langgraph_run``
activity, and returns its normalized result. It performs no I/O, no clock or
random access, no logging side effects, no observer calls, and no graph
execution — all of that lives in the activity — so Durable replay is safe
(issue #408).

The orchestration logic is factored into :func:`run_orchestration`, a plain
generator that depends only on a small :class:`OrchestrationContextLike`
protocol, so it can be unit-tested by driving the generator with a fake context
(no Azure host, no ``azure.durable_functions`` import required).
"""

from __future__ import annotations

from typing import Any, Generator, Protocol, runtime_checkable

__all__ = [
    "ACTIVITY_NAME",
    "OrchestrationContextLike",
    "run_orchestration",
]

# Stable Durable activity function name; also referenced by the runtime wiring.
ACTIVITY_NAME = "execute_langgraph_run"


@runtime_checkable
class OrchestrationContextLike(Protocol):
    """Structural subset of ``DurableOrchestrationContext`` used here.

    Only the two members the orchestrator touches are declared, so the
    deterministic logic can be exercised with a fake context in unit tests.
    """

    def get_input(self) -> Any:
        """Return the client input the orchestration was started with."""
        ...

    def call_activity(self, name: str, input_: Any) -> Any:
        """Schedule an activity call and return a Durable ``Task`` to yield."""
        ...


def run_orchestration(
    context: OrchestrationContextLike,
) -> Generator[Any, Any, Any]:
    """Deterministically call the run activity and return its result.

    Yields exactly one activity task (``execute_langgraph_run``) with the
    orchestration input passed through verbatim, then returns the activity's
    normalized result dict as the orchestration output.
    """

    payload = context.get_input()
    result = yield context.call_activity(ACTIVITY_NAME, payload)
    return result
