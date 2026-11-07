"""Native ownership boundaries, using real local locking (not Azure certification)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
from typing import Final, Literal, assert_never

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY,
    LeaseLossReason,
    ThreadLeaseLostError,
)
from tests.ownership_guard_support import (
    GuardableLock,
    ProbeGraph,
    RaisingProviderLock,
    RecordingObserver,
    guard_in,
)

# SIZE_OK: the brief requires this entire native boundary matrix in ONE new file.
# asyncio is intentional: cancellation must exercise the handlers' asyncio contract.
THREAD_ID: Final = "thread-SECRET-7f3a"
RUN_ID: Final = "rid-native-7f3a"
Endpoint = Literal["invoke", "stream"]


@dataclass(frozen=True, slots=True)
class Boundary:
    endpoint: Endpoint
    async_mode: bool


@pytest.fixture(params=[("invoke", False), ("stream", False), ("invoke", True), ("stream", True)])
def boundary(request: pytest.FixtureRequest) -> Boundary:
    return Boundary(*request.param)


class RecordingLock(InProcessThreadLock):
    """Record token lifetimes without implementing the optional capability."""

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


class Scenario:
    """Mutable request/app harness shared by all four native boundaries."""

    def __init__(self, lock: InProcessThreadLock, graph: ProbeGraph | None = None) -> None:
        self.probe = graph if graph is not None else ProbeGraph()
        self.observer = RecordingObserver()
        self.app = LangGraphApp(thread_lock=lock, observer=self.observer)
        self.app.register(graph=self.probe, name="agent")
        self.config: dict[str, object] = {"configurable": {"thread_id": THREAD_ID}}

    async def run(self, boundary: Boundary) -> func.HttpResponse:
        request = func.HttpRequest(
            method="POST",
            url=f"https://example.test/api/graphs/agent/{boundary.endpoint}",
            body=json.dumps({"input": {"value": "safe"}, "config": self.config}).encode(),
            headers={"X-Request-ID": RUN_ID},
        )
        original = json.loads(request.get_body())
        reg = self.app._registrations["agent"]
        match boundary.endpoint:
            case "invoke":
                if boundary.async_mode:
                    response = await self.app._handle_invoke_async(request, reg)
                else:
                    response = self.app._handle_invoke(request, reg)
            case "stream":
                if boundary.async_mode:
                    response = await self.app._handle_stream_async(request, reg)
                else:
                    response = self.app._handle_stream(request, reg)
            case unreachable:
                assert_never(unreachable)
        assert json.loads(request.get_body()) == original
        return response


def frames(response: func.HttpResponse) -> list[tuple[str, object]]:
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    result: list[tuple[str, object]] = []
    for frame in response.get_body().decode().strip().split("\n\n"):
        event, data = frame.split("\n")
        assert event.startswith("event: ")
        assert data.startswith("data: ")
        result.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return result


def assert_success(response: func.HttpResponse, endpoint: Endpoint) -> None:
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == RUN_ID
    match endpoint:
        case "invoke":
            assert response.mimetype == "application/json"
            assert json.loads(response.get_body()) == {"output": {"ok": True}}
        case "stream":
            assert frames(response) == [("data", {"n": n}) for n in (1, 2, 3)] + [("end", {})]
        case unreachable:
            assert_never(unreachable)


def assert_failure(response: func.HttpResponse, endpoint: Endpoint, data_count: int = 3) -> None:
    assert response.headers["X-Request-ID"] == RUN_ID
    match endpoint:
        case "invoke":
            assert response.status_code == 500
            assert response.mimetype == "application/json"
            assert json.loads(response.get_body()) == {
                "error": "error",
                "detail": "Graph execution failed",
                "request_id": RUN_ID,
            }
        case "stream":
            assert frames(response) == [("data", {"n": n}) for n in range(1, data_count + 1)] + [
                ("error", {"error": "stream processing failed", "request_id": RUN_ID}),
                ("end", {}),
            ]
        case unreachable:
            assert_never(unreachable)


async def test_hands_over_bound_guard_and_checks_after_graph(boundary: Boundary) -> None:
    # Given an ownership-capable lock and an intact caller-owned config.
    lock = GuardableLock()
    scenario = Scenario(lock)
    original_config = json.loads(json.dumps(scenario.config))

    def during_run(config: dict[str, object]) -> None:
        assert guard_in(config) is lock.guards[0]
        assert lock.guards[0].checks == 0
        assert lock.released == []

    scenario.probe.during_run = during_run
    # When the selected real native handler completes.
    response = await scenario.run(boundary)
    # Then precisely the acquired owner is handed over, checked and released once.
    assert_success(response, boundary.endpoint)
    assert len(scenario.probe.configs) == len(lock.guards) == len(lock.acquired) == 1
    assert guard_in(scenario.probe.configs[0]) is lock.guards[0]
    assert scenario.probe.configs[0]["configurable"]["thread_id"] == THREAD_ID
    assert lock.issued == [("agent", THREAD_ID, lock.acquired[0])]
    assert lock.guards[0].checks == 1
    assert lock.released == lock.acquired
    assert scenario.observer.events == ["started", "completed"]
    assert scenario.config == original_config
    assert OWNERSHIP_GUARD_CONFIG_KEY.encode() not in response.get_body()


@pytest.mark.parametrize(
    ("boundary", "reason"),
    [
        (Boundary(endpoint, mode), "lost")
        for endpoint in ("invoke", "stream")
        for mode in (False, True)
    ]
    + [(Boundary("invoke", False), reason) for reason in ("superseded", "released")],
)
async def test_terminal_loss_uses_existing_safe_failure(
    boundary: Boundary, reason: LeaseLossReason
) -> None:
    # Given a guard that reports loss on its first adapter check.
    lock = GuardableLock(trip_on=1, reason=reason)
    scenario = Scenario(lock)
    # When graph execution otherwise succeeds.
    response = await scenario.run(boundary)
    # Then only the observer receives the ownership diagnostics.
    assert_failure(response, boundary.endpoint)
    assert scenario.observer.events == ["started", "failed"]
    assert len(scenario.observer.failures) == 1
    error = scenario.observer.failures[0]
    assert isinstance(error, ThreadLeaseLostError)
    assert (error.reason, error.graph_name, error.thread_id) == (reason, "agent", THREAD_ID)
    assert lock.guards[0].checks == 1
    assert len(lock.acquired) == 1
    assert lock.released == lock.acquired
    for secret in (
        THREAD_ID,
        "agent",
        lock.acquired[0],
        "ThreadLeaseLostError",
        "locally detected",
    ):
        assert secret.encode() not in response.get_body()


@pytest.mark.parametrize("enabled", [False, None], ids=["disabled", "no-capability"])
async def test_unguarded_lock_preserves_exact_config(
    boundary: Boundary, enabled: None | bool
) -> None:
    # Given either no provider or a provider explicitly switched off.
    lock = RecordingLock() if enabled is None else GuardableLock(enabled=enabled)
    scenario = Scenario(lock)
    # When execution completes through a native boundary.
    response = await scenario.run(boundary)
    # Then no private keys or guards were added anywhere in the graph config.
    assert_success(response, boundary.endpoint)
    assert scenario.probe.configs == [{"configurable": {"thread_id": THREAD_ID}}]
    if isinstance(lock, GuardableLock):
        assert lock.guards == []
        assert lock.issued == []
    assert len(lock.acquired) == 1
    assert lock.released == lock.acquired
    assert scenario.observer.events == ["started", "completed"]


async def test_provider_failure_is_inside_token_cleanup(boundary: Boundary) -> None:
    # Given a provider that raises before the graph is called.
    lock = RaisingProviderLock()
    scenario = Scenario(lock)
    # When guard creation fails.
    response = await scenario.run(boundary)
    # Then ordinary failure reporting still releases the acquired token exactly once.
    assert_failure(response, boundary.endpoint, 0)
    assert scenario.probe.configs == []
    assert scenario.observer.events == ["started", "failed"]
    assert len(scenario.observer.failures) == 1
    assert type(scenario.observer.failures[0]) is RuntimeError
    assert len(lock.acquired) == 1
    assert lock.released == lock.acquired


@pytest.mark.parametrize("nested", [False, True], ids=["top-level", "configurable"])
async def test_client_guard_key_rejected_before_locking(boundary: Boundary, nested: bool) -> None:
    # Given a client trying to supply the reserved guard in either config location.
    lock = GuardableLock()
    scenario = Scenario(lock)
    if nested:
        scenario.config = {
            "configurable": {"thread_id": THREAD_ID, OWNERSHIP_GUARD_CONFIG_KEY: "evil"}
        }
    else:
        scenario.config[OWNERSHIP_GUARD_CONFIG_KEY] = "evil"
    # When native request admission validates the config.
    response = await scenario.run(boundary)
    # Then rejection happens before any acquisition, guard creation or graph call.
    assert response.status_code == 400
    assert json.loads(response.get_body()) == {
        "error": "error",
        "detail": "Internal configurable keys are not permitted"
        if nested
        else "Only config.configurable is client configurable",
        "request_id": RUN_ID,
    }
    assert scenario.probe.configs == []
    assert lock.acquired == lock.released == []
    assert lock.guards == []
    assert lock.issued == []
    assert scenario.observer.events == ["rejected:invalid_request"]


@pytest.mark.parametrize("async_mode", [False, True])
async def test_byte_cap_skips_terminal_check(async_mode: bool) -> None:
    # Given a trip-ready guard and a first frame too large for the buffer.
    lock = GuardableLock(trip_on=1)
    scenario = Scenario(lock, ProbeGraph(chunks=[{"payload": "x" * 200}]))
    scenario.app.max_stream_response_bytes = 64
    # When buffering stops early at the cap.
    response = await scenario.run(Boundary("stream", async_mode))
    # Then overflow, not lease loss, determines the failure and cleanup still runs.
    assert frames(response) == [
        (
            "error",
            {
                "error": "stream response exceeded max buffered size (64 bytes)",
                "request_id": RUN_ID,
            },
        ),
        ("end", {}),
    ]
    assert lock.guards[0].checks == 0
    assert scenario.observer.events == ["started", "failed"]
    assert len(scenario.observer.failures) == 1
    assert type(scenario.observer.failures[0]) is RuntimeError
    assert len(lock.acquired) == 1
    assert lock.released == lock.acquired


async def test_without_checkpointer_never_acquires_guard(boundary: Boundary) -> None:
    # Given a stateless graph even though the request carries a thread ID.
    lock = GuardableLock(trip_on=1)
    scenario = Scenario(lock, ProbeGraph(checkpointer=None))
    # When the native handler executes it.
    response = await scenario.run(boundary)
    # Then no lock capability is used and the graph config remains unchanged.
    assert_success(response, boundary.endpoint)
    assert scenario.probe.configs == [{"configurable": {"thread_id": THREAD_ID}}]
    assert lock.acquired == lock.released == []
    assert lock.guards == []
    assert lock.issued == []
    assert scenario.observer.events == ["started", "completed"]


async def test_graph_error_skips_terminal_check(boundary: Boundary) -> None:
    # Given a graph's own failure and a guard that would otherwise trip.
    error = ValueError("private graph failure")

    def fail(config: dict[str, object]) -> None:
        raise error

    lock = GuardableLock(trip_on=1)
    scenario = Scenario(lock, ProbeGraph(during_run=fail))
    # When the graph fails before normal completion.
    response = await scenario.run(boundary)
    # Then its exact exception wins, without a terminal guard check or duplicate failure.
    assert_failure(response, boundary.endpoint)
    assert scenario.observer.events == ["started", "failed"]
    assert scenario.observer.failures == [error]
    assert lock.guards[0].checks == 0
    assert len(lock.acquired) == 1
    assert lock.released == lock.acquired


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_cancellation_propagates_without_terminal_check(endpoint: Endpoint) -> None:
    # Given a graph blocked after receiving its guarded config, using event synchronization.
    class BlockingGraph(ProbeGraph):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.proceed = asyncio.Event()

        async def ainvoke(
            self, input: object, config: object = None, **kwargs: object
        ) -> dict[str, bool]:
            self.configs.append(config)
            self.started.set()
            await asyncio.wait_for(self.proceed.wait(), 2)
            return {"ok": True}

        async def astream(
            self,
            input: object,
            config: object = None,
            stream_mode: object = "values",
            **kwargs: object,
        ) -> AsyncIterator[dict[str, bool]]:
            await self.ainvoke(input, config)
            yield {"ok": True}

    graph = BlockingGraph()
    lock = GuardableLock(trip_on=1)
    scenario = Scenario(lock, graph)
    task = asyncio.create_task(scenario.run(Boundary(endpoint, True)))
    try:
        await asyncio.wait_for(graph.started.wait(), 2)
        # When cancellation interrupts the running graph.
        task.cancel()
        # Then cancellation escapes rather than becoming an ordinary run failure.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
    assert guard_in(graph.configs[0]) is lock.guards[0]
    assert lock.guards[0].checks == 0
    assert len(lock.acquired) == 1
    assert lock.released == lock.acquired
    assert scenario.observer.events == ["started"]
    assert scenario.observer.failures == []


async def test_contention_never_creates_guard(boundary: Boundary) -> None:
    # Given another owner already holds this exact graph/thread key.
    lock = GuardableLock()
    token = lock.acquire("agent", THREAD_ID)
    assert token is not None
    scenario = Scenario(lock)
    try:
        # When a second execution attempts admission.
        response = await scenario.run(boundary)
        # Then it neither creates a guard nor releases the existing owner's token.
        assert response.status_code == 409
        assert json.loads(response.get_body()) == {
            "error": "error",
            "detail": f"Thread {THREAD_ID!r} is currently in use by another request",
            "request_id": RUN_ID,
        }
        assert scenario.probe.configs == []
        assert lock.guards == []
        assert lock.issued == []
        assert lock.released == []
        assert lock.acquired == [token]
        assert scenario.observer.events == ["rejected:lock_contention"]
    finally:
        lock.release("agent", THREAD_ID, token)
