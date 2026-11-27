"""Deterministic Activity lock lifecycle regressions; no Azure certification."""

from __future__ import annotations

from asyncio import CancelledError
import logging
from typing import Literal, assert_never

import pytest

from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
from azure_functions_langgraph.durable._state import DurableRunPayload
from azure_functions_langgraph.observability import RunContext
from tests.test_durable_execution_parity import Graph, Observer, RecordingLock, Reg


class FailureObserver(Observer):
    def __init__(self) -> None:
        super().__init__()
        self.failures: list[BaseException] = []

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        super().on_run_failed(ctx, exc)
        self.failures.append(exc)


class BackendLock(RecordingLock):
    def __init__(self, observer: Observer) -> None:
        super().__init__()
        self.observer = observer
        self.acquire_error: BaseException | None = None
        self.release_error: BaseException | None = None
        self.events_at_release: list[str] | None = None

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        if self.acquire_error is not None:
            raise self.acquire_error
        return super().acquire(graph_name, thread_id, timeout)

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.events_at_release = self.observer.names
        super().release(graph_name, thread_id, token)
        if self.release_error is not None:
            raise self.release_error


@pytest.mark.parametrize(
    "failures",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (False, True, True),
    ],
    ids=["success", "acquire", "graph", "release", "graph-and-release"],
)
async def test_terminal_outcome_waits_for_lock_lifecycle(
    failures: tuple[bool, bool, bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a checkpointed graph and independently failing backend operations.
    acquire_fails, graph_fails, release_fails = failures
    observer = FailureObserver()
    lock = BackendLock(observer)
    acquire_error = OSError("private acquire diagnostic")
    graph_error = ValueError("private graph diagnostic")
    release_error = RuntimeError("private release diagnostic")
    lock.acquire_error = acquire_error if acquire_fails else None
    lock.release_error = release_error if release_fails else None
    graph = Graph(checkpointer=True, error=graph_error if graph_fails else None)
    expected_error = (
        acquire_error
        if acquire_fails
        else (graph_error if graph_fails else (release_error if release_fails else None))
    )

    # When the Activity executes the complete lock lifecycle.
    with caplog.at_level(logging.ERROR):
        result = await execute_langgraph_run_impl(
            DurableRunPayload("run-1", "echo", {}, thread_id="t-1"),
            registry={"echo": Reg(graph)},
            thread_lock=lock,
            observer=observer,
        )

    # Then only the final outcome is observed, after release when acquired.
    assert lock.events_at_release == (None if acquire_fails else ["started"])
    assert len(graph.calls) == (0 if acquire_fails else 1)
    assert lock.released == ([] if acquire_fails else [("echo", "t-1")])
    assert result.run_id == "run-1"
    if expected_error is None:
        assert result.activity_status == "success"
        assert result.result == {"ok": True}
        assert result.error is None
        assert observer.names == ["started", "completed"]
        assert observer.failures == []
    else:
        assert result.activity_status == "error"
        assert result.result is None
        assert result.error == {"type": type(expected_error).__name__, "message": "The run failed."}
        assert "private" not in repr(result.to_dict())
        assert observer.names == (["failed"] if acquire_fails else ["started", "failed"])
        assert observer.failures == [expected_error]
        assert observer.events[-1][1].ended_at_ns is not None
        logged = [record.exc_info[1] for record in caplog.records if record.exc_info]
        assert expected_error in logged
        if release_fails:
            assert release_error in logged


async def test_contention_rejects_without_starting_or_releasing_another_owner() -> None:
    # Given an existing owner for the same graph/thread.
    observer = FailureObserver()
    lock = BackendLock(observer)
    held = lock.acquire("echo", "t-1")
    assert held is not None
    graph = Graph(checkpointer=True)

    # When another Activity attempts that thread.
    try:
        result = await execute_langgraph_run_impl(
            DurableRunPayload("run-1", "echo", {}, thread_id="t-1"),
            registry={"echo": Reg(graph)},
            thread_lock=lock,
            observer=observer,
        )

        # Then contention is the only terminal event and ownership remains intact.
        assert result.activity_status == "rejected"
        assert observer.names == ["rejected"]
        assert observer.reasons == ["lock_contention"]
        assert observer.failures == []
        assert graph.calls == []
        assert lock.released == []
        assert lock.acquire("echo", "t-1") is None
    finally:
        lock.release("echo", "t-1", held)


@pytest.mark.parametrize("phase", ["acquire", "graph", "release", "graph-and-release"])
async def test_cancellation_propagates_with_one_failed_event(
    phase: Literal["acquire", "graph", "release", "graph-and-release"],
) -> None:
    # Given cancellation at each lifecycle stage, including a raising cleanup.
    observer = FailureObserver()
    lock = BackendLock(observer)
    cancellation = CancelledError()
    graph = Graph(checkpointer=True)
    match phase:
        case "acquire":
            lock.acquire_error = cancellation
        case "release":
            lock.release_error = cancellation
        case "graph":
            graph.error = cancellation
        case "graph-and-release":
            graph.error = cancellation
            lock.release_error = OSError("private cleanup diagnostic")
        case unreachable:
            assert_never(unreachable)

    # When cancellation interrupts the Activity.
    with pytest.raises(CancelledError) as raised:
        await execute_langgraph_run_impl(
            DurableRunPayload("run-1", "echo", {}, thread_id="t-1"),
            registry={"echo": Reg(graph)},
            thread_lock=lock,
            observer=observer,
        )

    # Then it propagates unchanged; cleanup precedes the single failed outcome.
    assert raised.value is cancellation
    assert observer.failures == [cancellation]
    assert observer.names == (["failed"] if phase == "acquire" else ["started", "failed"])
    assert lock.events_at_release == (None if phase == "acquire" else ["started"])
    assert lock.released == ([] if phase == "acquire" else [("echo", "t-1")])
