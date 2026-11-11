"""Offline walkthrough of Service Bus session-aware routing.

No Service Bus namespace, credentials or network are used. A synthetic message
object stands in for ``azure.functions.ServiceBusMessage`` and is fed to the same
``process_service_bus_message`` function the generated trigger calls.

Run from this directory::

    PYTHONPATH=../../src python session_demo.py
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from graph import build_graph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.triggers.service_bus import (
    ThreadContentionError,
    process_service_bus_message,
)


@dataclass
class SyntheticMessage:
    """Minimal stand-in for ``azure.functions.ServiceBusMessage``."""

    body: bytes
    message_id: str
    session_id: str | None = None

    def get_body(self) -> bytes:
        return self.body


def message(text: str, *, session_id: str | None, message_id: str) -> SyntheticMessage:
    return SyntheticMessage(json.dumps({"seen": [text]}).encode(), message_id, session_id)


def _app(graph: Any, **kwargs: Any) -> Any:
    app = LangGraphApp()
    app.register_service_bus(
        graph, "agent", connection="ServiceBusConnection", queue_name="jobs", **kwargs
    )
    return app._sb_registrations["agent"]


def _rejection(fn: Any) -> str:
    try:
        fn()
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected the topology to be rejected")


def run_demo() -> dict[str, Any]:
    """Run every scenario and return what happened (also asserted by the smoke test)."""
    out: dict[str, Any] = {}
    lock = InProcessThreadLock()

    # 1. session_id -> checkpoint thread_id: same session accumulates state.
    graph = build_graph()
    invocations: list[Any] = []
    real_invoke = graph.invoke

    def counting_invoke(*args: Any, **kwargs: Any) -> Any:
        invocations.append(args)
        return real_invoke(*args, **kwargs)

    graph.invoke = counting_invoke
    reg = _app(graph, session=True)
    process_service_bus_message(
        reg, message("a", session_id="order-1", message_id="m1"), thread_lock=lock
    )
    second = process_service_bus_message(
        reg, message("b", session_id="order-1", message_id="m2"), thread_lock=lock
    )
    other = process_service_bus_message(
        reg, message("c", session_id="order-2", message_id="m3"), thread_lock=lock
    )
    out["same_session"] = second
    out["other_session"] = other

    # 2. At-least-once: a redelivered message_id runs the graph again (no dedup).
    redelivered = process_service_bus_message(
        reg, message("b", session_id="order-1", message_id="m2"), thread_lock=lock
    )
    out["redelivered"] = redelivered

    # 3. Missing session_id is rejected before the graph runs.
    runs_before = len(invocations)
    try:
        process_service_bus_message(
            reg, message("x", session_id=None, message_id="m4"), thread_lock=lock
        )
    except ValueError as exc:
        out["missing_session"] = str(exc)
    out["missing_session_ran_graph"] = len(invocations) != runs_before

    # 4. A thread_id_factory must agree with the session id.
    mismatch = _app(build_graph(), session=True, thread_id_factory=lambda m: "other-thread")
    try:
        process_service_bus_message(
            mismatch, message("x", session_id="order-1", message_id="m5"), thread_lock=lock
        )
    except ValueError as exc:
        out["factory_mismatch"] = str(exc)

    # 5. The package thread lock is separate from the broker session lock.
    contended = _app(build_graph(), session=True, lock_timeout=0.0)
    token = lock.acquire("agent", "order-1")
    assert token is not None
    try:
        process_service_bus_message(
            contended, message("x", session_id="order-1", message_id="m6"), thread_lock=lock
        )
    except ThreadContentionError as exc:
        out["contention"] = str(exc)
    finally:
        lock.release("agent", "order-1", token)

    # 6. session_exclusive: operator asserts the broker session is the only writer,
    #    so the application lock is skipped (it is never touched here).
    class ForbiddenLock(InProcessThreadLock):
        def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
            raise AssertionError("exclusive sessions must not use the application lock")

    exclusive = _app(build_graph(), session=True, session_exclusive=True)
    out["exclusive"] = process_service_bus_message(
        exclusive, message("e", session_id="order-9", message_id="m7"), thread_lock=ForbiddenLock()
    )

    # 7. Registration-time topology rejections.
    def exclusive_with_http() -> None:
        app = LangGraphApp()
        app.register(build_graph(), "http_graph")
        app.register_service_bus(
            build_graph(),
            "agent",
            connection="ServiceBusConnection",
            queue_name="jobs",
            session=True,
            session_exclusive=True,
        )

    out["reject_http"] = _rejection(exclusive_with_http)
    out["reject_no_session"] = _rejection(lambda: _app(build_graph(), session_exclusive=True))
    out["reject_batch"] = _rejection(
        lambda: _app(
            build_graph(),
            session=True,
            session_exclusive=True,
            binding_kwargs={"cardinality": "many"},
        )
    )
    return out


def main() -> None:
    result = run_demo()
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
