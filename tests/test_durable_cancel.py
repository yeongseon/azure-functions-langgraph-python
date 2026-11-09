"""Cancellation contract of the Durable control plane (#809).

``POST runs/{run_id}/cancel`` used to terminate the orchestration and immediately answer
``202`` with ``{"status": "cancelled"}``. Durable termination is asynchronous and does not stop
an Activity that is already running, so that answer claimed more than anything had observed. It
now answers only what is known: ``cancel_requested`` once the termination call returned, ``200``
with the real status for a run that already finished, and a sanitized ``502`` when the backend
fails. Everything here runs on in-process fakes (no Azure host, no Durable task hub).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import threading
from typing import Any

import pytest

from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
from azure_functions_langgraph.durable._lifecycle import (
    CANCEL_REQUESTED,
    cancel_run_impl,
    create_run_impl,
    get_run_impl,
)
from azure_functions_langgraph.durable._runtime import cancel_run_http
from azure_functions_langgraph.locks import InProcessThreadLock
from tests.test_durable import (
    FakeDurableClient,
    FakeReg,
    FakeStatus,
    RecordingObserver,
    _Enum,
    _payload,
    _req,
)

SECRET = "postgresql://user:private-backend-canary@db.internal/app"
ACK = {"run_id": "r1", "status": "cancel_requested"}


class ScriptedClient(FakeDurableClient):
    """A fake Durable client whose orchestration can change state between and during calls."""

    def __init__(
        self,
        runtime: Any = "Running",
        output: Any = None,
        *,
        on_terminate: Callable[[ScriptedClient], None] | None = None,
        fail_status: Exception | None = None,
        fail_terminate: Exception | None = None,
    ) -> None:
        super().__init__(FakeStatus(runtime, output))
        self._on_terminate = on_terminate
        self._fail_status = fail_status
        self._fail_terminate = fail_terminate

    def set_status(self, runtime: Any, output: Any = None) -> None:
        self._status = FakeStatus(runtime, output)

    async def get_status(self, instance_id: str) -> FakeStatus | None:
        if self._fail_status is not None:
            raise self._fail_status
        return self._status

    async def terminate(self, instance_id: str, reason: str) -> None:
        self.terminated.append((instance_id, reason))
        if self._fail_terminate is not None:
            raise self._fail_terminate
        if self._on_terminate is not None:
            self._on_terminate(self)


def _outcome(status: str, output: Any = None) -> dict[str, Any]:
    return {"activity_status": status, "result": output, "error": None}


class TestRequestingTermination:
    @pytest.mark.parametrize(
        "runtime",
        [
            "Pending",
            "Running",
            "Suspended",
            "ContinuedAsNew",
            _Enum("Running"),
            "  Running  ",
            "SomeFutureStatus",
        ],
        ids=["pending", "running", "suspended", "continued", "enum", "whitespace", "unknown"],
    )
    async def test_a_nonterminal_run_gets_an_acknowledgement_not_a_verdict(
        self, runtime: Any
    ) -> None:
        # Given a run whose orchestration is still in flight (or in a state we do not know).
        client = ScriptedClient(runtime)

        # When cancellation is requested.
        code, body = await cancel_run_impl(client, "r1")

        # Then the termination was requested and the answer says exactly that: it is not
        # "cancelled", and it is not one of the statuses GET returns.
        assert code == 202
        assert body == ACK == {"run_id": "r1", "status": CANCEL_REQUESTED}
        assert client.terminated == [("r1", "cancelled by client")]

    async def test_an_unknown_run_is_404_and_nothing_is_requested(self) -> None:
        for client in (FakeDurableClient(status=None), FakeDurableClient(FakeStatus(None))):
            code, body = await cancel_run_impl(client, "r1")
            assert (code, body) == (404, {"run_id": "r1", "error": "run not found"})
            assert client.terminated == []


class TestFinishedRuns:
    @pytest.mark.parametrize(
        ("runtime", "output", "expected"),
        [
            ("Completed", _outcome("success", {"y": 1}), "success"),
            ("Completed", _outcome("error"), "error"),
            ("Completed", _outcome("rejected"), "error"),
            ("Completed", None, "success"),
            ("Failed", None, "error"),
            ("Terminated", None, "cancelled"),
            ("Canceled", None, "cancelled"),
            ("Cancelled", None, "cancelled"),
            (_Enum("Completed"), _outcome("success"), "success"),
            (_Enum("Terminated"), None, "cancelled"),
            ("  Terminated ", None, "cancelled"),
        ],
        ids=[
            "success",
            "activity-error",
            "activity-rejected",
            "completed-no-output",
            "failed",
            "terminated",
            "canceled",
            "cancelled",
            "enum-completed",
            "enum-terminated",
            "whitespace",
        ],
    )
    async def test_a_finished_run_reports_its_real_status_and_requests_nothing(
        self, runtime: Any, output: Any, expected: str
    ) -> None:
        # Given a run that already finished (a run that succeeded must never be called cancelled).
        client = ScriptedClient(runtime, output)

        # When cancellation is requested.
        code, body = await cancel_run_impl(client, "r1")

        # Then it is 200 with the status GET would return, no termination was requested, and no
        # backend output leaks into the response.
        assert code == 200
        assert body == {"run_id": "r1", "status": expected}
        assert client.terminated == []
        get_code, get_body = await get_run_impl(client, "r1")
        assert (get_code, get_body["status"]) == (200, expected)

    async def test_backend_output_is_never_copied_into_the_cancel_response(self) -> None:
        client = ScriptedClient("Completed", _outcome("success", {"secret": SECRET}))

        _, body = await cancel_run_impl(client, "r1")

        assert SECRET not in json.dumps(body)


class TestRepeatedAndQueuedCancellation:
    async def test_repeating_the_request_is_safe_before_and_after_the_termination_applies(
        self,
    ) -> None:
        # Given a run whose termination Durable has accepted but not applied yet.
        client = ScriptedClient("Running")
        assert await cancel_run_impl(client, "r1") == (202, ACK)

        # When the client asks again before anything changed (a lost response, a retry loop).
        assert await cancel_run_impl(client, "r1") == (202, ACK)
        assert len(client.terminated) == 2

        # And again after Durable applied the termination.
        client.set_status("Terminated")
        code, body = await cancel_run_impl(client, "r1")

        # Then the third answer is the truth and no further termination was requested.
        assert (code, body) == (200, {"run_id": "r1", "status": "cancelled"})
        assert len(client.terminated) == 2

    async def test_a_queued_termination_shows_up_on_get_only_when_it_is_applied(self) -> None:
        # Given an accepted termination that Durable applies a few polls later.
        client = ScriptedClient("Running")
        assert (await cancel_run_impl(client, "r1"))[0] == 202

        # When the client polls: the acknowledgement did not change the lifecycle status.
        for _ in range(3):
            code, body = await get_run_impl(client, "r1")
            assert (code, body["status"]) == (200, "running")

        # And Durable finally applies it.
        client.set_status("Terminated")
        code, body = await get_run_impl(client, "r1")

        # Then GET reports the terminal status the acknowledgement only promised to ask for.
        assert (code, body["status"]) == (200, "cancelled")

    async def test_a_run_that_finishes_inside_the_race_window_is_acknowledged_and_ends_as_it_ended(
        self,
    ) -> None:
        # Given a run that completes between the status check and the termination call: the
        # real SDK returns normally for an instance that already finished (HTTP 410).
        client = ScriptedClient(
            "Running",
            on_terminate=lambda c: c.set_status("Completed", _outcome("success", {"y": 1})),
        )

        # When cancellation is requested.
        code, body = await cancel_run_impl(client, "r1")

        # Then the request is still only acknowledged (it may or may not have mattered) and GET
        # tells the truth: the run succeeded.
        assert (code, body) == (202, ACK)
        get_code, get_body = await get_run_impl(client, "r1")
        assert (get_code, get_body["status"]) == (200, "success")
        # And a retry sees a finished run and requests nothing more.
        assert await cancel_run_impl(client, "r1") == (200, {"run_id": "r1", "status": "success"})
        assert len(client.terminated) == 1


class TestBackendFailures:
    async def test_a_failing_status_lookup_is_a_sanitized_502_and_requests_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = ScriptedClient(fail_status=RuntimeError(SECRET))

        with caplog.at_level(logging.ERROR):
            code, body = await cancel_run_impl(client, "r1")

        assert code == 502
        assert body == {
            "run_id": "r1",
            "error": {
                "code": "cancel_failed",
                "message": "Unable to confirm the cancellation request.",
            },
        }
        assert SECRET not in json.dumps(body)
        assert client.terminated == []
        assert SECRET in caplog.text  # the cause stays available to operators

    async def test_a_failing_termination_is_a_sanitized_502_after_the_attempt(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = ScriptedClient(fail_terminate=OSError(SECRET))

        with caplog.at_level(logging.ERROR):
            code, body = await cancel_run_impl(client, "r1")

        # The attempt was made (its outcome is unknown), and the client learns only that.
        assert code == 502
        assert body["error"]["code"] == "cancel_failed"
        assert SECRET not in json.dumps(body)
        assert client.terminated == [("r1", "cancelled by client")]
        assert SECRET in caplog.text

    async def test_the_http_adapter_returns_the_same_contract(self) -> None:
        running = await cancel_run_http(
            ScriptedClient("Running"), _req(route_params={"run_id": "r1"})
        )
        finished = await cancel_run_http(
            ScriptedClient("Completed", _outcome("success")), _req(route_params={"run_id": "r1"})
        )
        failing = await cancel_run_http(
            ScriptedClient(fail_status=RuntimeError(SECRET)), _req(route_params={"run_id": "r1"})
        )

        assert (running.status_code, json.loads(running.get_body())) == (202, ACK)
        assert (finished.status_code, json.loads(finished.get_body())) == (
            200,
            {"run_id": "r1", "status": "success"},
        )
        assert failing.status_code == 502
        assert SECRET.encode() not in failing.get_body()


class BlockingGraph:
    """A checkpointed synchronous graph that blocks inside ``invoke`` until released."""

    checkpointer = object()

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def invoke(self, input: Any, config: Any = None) -> Any:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10), "the test never released the graph"
        return {"done": True}


class TestTerminatedOrchestrationVersusRunningActivity:
    """The status the API reports is the orchestration's, not the Activity's."""

    async def test_a_terminated_orchestration_still_holds_the_thread_while_its_worker_runs(
        self,
    ) -> None:
        # Given a checkpointed graph blocked inside a running Activity.
        graph = BlockingGraph()
        lock = InProcessThreadLock()
        registry = {"echo": FakeReg(graph, "echo")}
        run_a = asyncio.create_task(
            execute_langgraph_run_impl(
                _payload(run_id="run-a", thread_id="t1"), registry=registry, thread_lock=lock
            )
        )
        try:
            assert await asyncio.to_thread(graph.entered.wait, 5)

            # When the run is cancelled and Durable applies the termination to the orchestration.
            client = ScriptedClient("Running")
            assert await cancel_run_impl(client, "run-a") == (
                202,
                {"run_id": "run-a", "status": "cancel_requested"},
            )
            client.set_status("Terminated")

            # Then the API reports the orchestration as cancelled while the Activity is still
            # inside the graph...
            assert (await get_run_impl(client, "run-a"))[1]["status"] == "cancelled"
            assert not run_a.done() and graph.calls == 1

            # ...and a new run for the same checkpointed thread is accepted by Durable but its
            # Activity cannot take the thread: it is rejected, and GET reports that as an error.
            code, _ = await create_run_impl(
                client, "orchestrator", {"thread_id": "t1", "input": {}}, graph_name="echo"
            )
            assert code == 202
            observer = RecordingObserver()
            rejected = await execute_langgraph_run_impl(
                _payload(run_id="run-b", thread_id="t1"),
                registry=registry,
                thread_lock=lock,
                observer=observer,
            )
            assert rejected.activity_status == "rejected"
            assert rejected.error is not None and rejected.error["type"] == "ThreadContentionError"
            assert observer.events == ["rejected"]
            finished_b = FakeDurableClient(FakeStatus("Completed", rejected.to_dict()))
            assert (await get_run_impl(finished_b, "run-b"))[1]["status"] == "error"
        finally:
            graph.release.set()

        # When the blocked worker finally returns, its result is produced (Durable discards it
        # for a terminated orchestration, but the graph ran to the end)...
        result_a = await run_a
        assert result_a.activity_status == "success" and graph.calls == 1

        # ...and only then can the thread be used again.
        graph_c = BlockingGraph()
        graph_c.release.set()
        again = await execute_langgraph_run_impl(
            _payload(run_id="run-c", thread_id="t1"),
            registry={"echo": FakeReg(graph_c, "echo")},
            thread_lock=lock,
        )
        assert again.activity_status == "success"

    async def test_cancelling_the_activity_task_keeps_the_thread_until_the_graph_stops(
        self,
    ) -> None:
        # Given the same blocked Activity, and a host that cancels its task (for example on
        # shutdown): the protection added in #652 applies, not anything Durable does.
        graph = BlockingGraph()
        lock = InProcessThreadLock()
        registry = {"echo": FakeReg(graph, "echo")}
        run_a = asyncio.create_task(
            execute_langgraph_run_impl(
                _payload(run_id="run-a", thread_id="t1"), registry=registry, thread_lock=lock
            )
        )
        try:
            assert await asyncio.to_thread(graph.entered.wait, 5)

            # When the task is cancelled while the synchronous graph is still running.
            run_a.cancel()
            await asyncio.sleep(0.05)

            # Then it has not finished and still holds the thread, so a new Activity is rejected.
            assert not run_a.done()
            rejected = await execute_langgraph_run_impl(
                _payload(run_id="run-b", thread_id="t1"), registry=registry, thread_lock=lock
            )
            assert rejected.activity_status == "rejected"
        finally:
            graph.release.set()

        # When the graph stops, the cancellation is delivered and the thread is free again.
        with pytest.raises(asyncio.CancelledError):
            await run_a
        token = lock.acquire("echo", "t1")
        assert token is not None
        lock.release("echo", "t1", token)
