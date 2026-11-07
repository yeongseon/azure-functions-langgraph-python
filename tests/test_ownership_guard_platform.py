"""Platform ownership boundaries over real local locks/stores, not Azure certification."""

from collections.abc import Callable, Mapping
import json
from typing import Final, Literal

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY as KEY,
)
from azure_functions_langgraph.locks.guard import (
    LeaseLossReason,
    OwnershipGuard,
    ThreadLeaseLostError,
    resolve_ownership_guard,
)
from azure_functions_langgraph.platform.contracts import Thread, ThreadStatus
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from tests.ownership_guard_support import (
    GuardableLock,
    ProbeGraph,
    RaisingProviderLock,
    RecordingObserver,
    guard_in,
)
from tests.test_platform_routes import _get_fn, _post_request

# allow: SIZE_OK — the requested single-file boundary matrix shares one local harness.
THREAD_ID: Final = "thread-SECRET-7f3a"
RUN_ID: Final = "rid-7f3a"
AGENT: Final = "agent"
Endpoint = Literal["wait", "stream"]


class PlainLock(InProcessThreadLock):
    """Record real acquisitions/releases without implementing the guard capability."""

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


class RecordingStore(InMemoryThreadStore):
    """Record cleanup status, owner and context while retaining real store exclusion."""

    def __init__(self) -> None:
        super().__init__(id_factory=lambda: THREAD_ID)
        self.releases: list[tuple[ThreadStatus, str | None]] = []
        self.release_guards: list[OwnershipGuard | None] = []
        self.owner: str | None = None

    def try_acquire_run_lock(
        self, thread_id: str, *, assistant_id: str | None = None
    ) -> Thread | None:
        locked = super().try_acquire_run_lock(thread_id, assistant_id=assistant_id)
        if locked is not None:
            self.owner = locked.lock_token
        return locked

    def release_run_lock(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, object] | None = None,
        owner_token: str | None = None,
    ) -> Thread:
        self.releases.append((status, owner_token))
        self.release_guards.append(resolve_ownership_guard(None))
        return super().release_run_lock(
            thread_id, status=status, values=values, owner_token=owner_token
        )


class Harness:
    """Wire the actual registered synchronous handlers to per-test local resources."""

    def __init__(
        self,
        lock: GuardableLock | RaisingProviderLock | PlainLock,
        graph: ProbeGraph,
        max_bytes: int = 1024 * 1024,
    ) -> None:
        self.lock = lock
        self.graph = graph
        self.store = RecordingStore()
        self.observer = RecordingObserver()
        self.app = LangGraphApp(
            platform_compat=True,
            thread_lock=lock,
            observer=self.observer,
            max_stream_response_bytes=max_bytes,
        )
        self.app._thread_store = self.store
        self.app.register(graph=graph, name=AGENT)
        self.store.create()

    def request(self, body: Mapping[str, object]) -> func.HttpRequest:
        original = _post_request("/api/threads/state", dict(body), thread_id=THREAD_ID)
        return func.HttpRequest(
            method="POST",
            url=original.url,
            body=original.get_body(),
            headers={"Content-Type": "application/json", "X-Request-ID": RUN_ID},
            route_params={"thread_id": THREAD_ID},
        )

    def call(self, endpoint: str, body: Mapping[str, object] | None = None) -> func.HttpResponse:
        handler: Callable[[func.HttpRequest], func.HttpResponse] = _get_fn(
            self.app.function_app, f"aflg_platform_{endpoint}"
        )
        return handler(self.request(body if body is not None else {"assistant_id": AGENT}))

    def assert_released(self, status: ThreadStatus) -> None:
        assert len(self.lock.acquired) == 1
        assert self.lock.released == self.lock.acquired
        assert self.store.owner is not None
        assert self.store.owner != self.lock.acquired[0]
        assert self.store.releases == [(status, self.store.owner)]
        current = self.store.get(THREAD_ID)
        assert current is not None
        assert current.status == status
        assert current.lock_token is None
        assert self.store.release_guards == [None]

    def assert_sanitized(self, response: func.HttpResponse) -> None:
        wire = response.get_body().decode()
        for secret in (THREAD_ID, AGENT, self.lock.acquired[0], "ThreadLeaseLostError"):
            assert secret not in wire


def _frames(response: func.HttpResponse) -> list[tuple[str, object]]:
    return [
        (frame.splitlines()[0].removeprefix("event: "), json.loads(frame.splitlines()[1][6:]))
        for frame in response.get_body().decode().strip().split("\n\n")
    ]


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_run_hands_thread_token_guard_to_graph_when_capable(endpoint: Endpoint) -> None:
    # Given client config and independently owned store/thread locks.
    lock = GuardableLock()
    h = Harness(lock, ProbeGraph())
    body = {"assistant_id": AGENT, "config": {"configurable": {"user": "u1"}, "tags": ["t1"]}}
    # When the actual Platform handler completes normally.
    response = h.call(f"runs_{endpoint}", body)
    # Then only the thread-token guard accompanies the preserved client config.
    assert response.status_code == 200
    guard = lock.guards[0]
    assert guard_in(h.graph.configs[0]) is guard
    assert h.graph.configs == [
        {"configurable": {"thread_id": THREAD_ID, "user": "u1", KEY: guard}, "tags": ["t1"]}
    ]
    assert lock.issued == [(AGENT, THREAD_ID, lock.acquired[0])]
    assert guard.checks == 1
    h.assert_released("idle")
    assert h.observer.events == ["started", "completed"]


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("reason", ["lost", "superseded"])
def test_run_fails_when_terminal_guard_reports_loss(
    endpoint: Endpoint, reason: LeaseLossReason
) -> None:
    # Given loss detected only after normal graph completion.
    lock = GuardableLock(trip_on=1, reason=reason)
    h = Harness(lock, ProbeGraph())
    # When the boundary performs its terminal check.
    response = h.call(f"runs_{endpoint}")
    # Then the existing transport error replaces success without losing data frames.
    match endpoint:
        case "wait":
            assert response.status_code == 500
            assert json.loads(response.get_body()) == {
                "detail": "Graph execution failed",
                "request_id": RUN_ID,
            }
        case "stream":
            assert response.status_code == 200
            assert response.mimetype == "text/event-stream"
            assert _frames(response) == [
                ("metadata", {"run_id": RUN_ID}),
                ("values", {"n": 1}),
                ("values", {"n": 2}),
                ("values", {"n": 3}),
                ("error", {"error": "stream processing failed", "request_id": RUN_ID}),
                ("end", None),
            ]
    assert lock.guards[0].checks == 1
    h.assert_released("error")
    assert h.observer.events == ["started", "failed"]
    assert len(h.observer.failures) == 1
    error = h.observer.failures[0]
    assert isinstance(error, ThreadLeaseLostError)
    assert (error.reason, error.graph_name, error.thread_id) == (reason, AGENT, THREAD_ID)
    h.assert_sanitized(response)


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("capability_off", [False, True])
def test_run_preserves_legacy_config_when_unguarded(
    endpoint: Endpoint, capability_off: bool
) -> None:
    # Given a plain lock or an explicitly disabled guard provider.
    lock = GuardableLock(enabled=False) if capability_off else PlainLock()
    h = Harness(lock, ProbeGraph())
    # When config crosses the threaded run boundary.
    response = h.call(
        f"runs_{endpoint}",
        {
            "assistant_id": AGENT,
            "config": {"configurable": {"user": "u1", "thread_id": "client"}, "tags": ["t1"]},
        },
    )
    # Then no private key is introduced and the route still pins its own thread id.
    assert response.status_code == 200
    assert h.graph.configs == [
        {"configurable": {"thread_id": THREAD_ID, "user": "u1"}, "tags": ["t1"]}
    ]
    h.assert_released("idle")


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("capability", ["on", "off", "absent"])
def test_run_discards_client_guard_aliases(endpoint: Endpoint, capability: str) -> None:
    # Given both JSON aliases that pass Platform structural validation.
    lock = PlainLock() if capability == "absent" else GuardableLock(enabled=capability == "on")
    h = Harness(lock, ProbeGraph())
    body = {
        "assistant_id": AGENT,
        "config": {"configurable": {KEY: "evil", "user": "u1"}, KEY: "evil2", "tags": ["t1"]},
    }
    request = h.request(body)
    before = json.loads(request.get_body())
    handler = _get_fn(h.app.function_app, f"aflg_platform_runs_{endpoint}")
    # When the unfiltered Platform config reaches the shared guard helper.
    response = handler(request)
    # Then aliases cannot masquerade as ownership, even with no provider.
    assert response.status_code == 200
    config = h.graph.configs[0]
    assert KEY not in config
    if capability == "on":
        assert isinstance(lock, GuardableLock)
        assert guard_in(config) is lock.guards[0]
    else:
        assert KEY not in config["configurable"]
    assert config["configurable"]["user"] == "u1"
    assert config["tags"] == ["t1"]
    assert json.loads(request.get_body()) == before == body
    h.assert_released("idle")


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_run_releases_both_locks_when_guard_provider_raises(endpoint: Endpoint) -> None:
    # Given a provider failure before graph execution or stream metadata.
    h = Harness(RaisingProviderLock(), ProbeGraph())
    # When guard construction fails inside the protected setup.
    response = h.call(f"runs_{endpoint}")
    # Then even the stream route returns the exact pre-metadata JSON failure.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == {
        "detail": "Graph execution failed",
        "request_id": RUN_ID,
    }
    assert h.graph.configs == []
    assert h.observer.events == ["failed"]
    assert str(h.observer.failures[0]) == "guard provider failed"
    h.assert_released("error")
    h.assert_sanitized(response)


@pytest.mark.parametrize("max_bytes", [1, 100])
def test_stream_skips_terminal_check_when_buffer_overflows(max_bytes: int) -> None:
    # Given either metadata overflow or a data chunk exceeding the remaining cap.
    lock = GuardableLock(trip_on=1)
    h = Harness(lock, ProbeGraph(chunks=[{"large": "x" * 200}]), max_bytes)
    # When streaming exits early instead of exhausting the graph.
    response = h.call("runs_stream")
    # Then the existing overflow frame wins and the terminal guard is not checked.
    assert response.status_code == 200
    assert _frames(response) == [
        ("metadata", {"run_id": RUN_ID}),
        ("error", {"error": f"stream response exceeded max buffered size ({max_bytes} bytes)"}),
        ("end", None),
    ]
    assert len(h.graph.configs) == int(max_bytes > 1)
    assert lock.guards[0].checks == 0
    assert h.observer.events == ["started", "failed"]
    h.assert_released("error")
    h.assert_sanitized(response)


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_run_skips_terminal_check_when_graph_raises(endpoint: Endpoint) -> None:
    # Given graph failure after execution (and after stream data, for streaming).
    failure = RuntimeError("private-graph-canary")

    def fail(_config: object) -> None:
        raise failure

    lock = GuardableLock(trip_on=1)
    h = Harness(lock, ProbeGraph(during_run=fail))
    # When the graph's own exception crosses the boundary.
    response = h.call(f"runs_{endpoint}")
    # Then it is not replaced by a terminal ownership check.
    assert lock.guards[0].checks == 0
    assert h.observer.failures == [failure]
    assert h.observer.events == ["started", "failed"]
    match endpoint:
        case "wait":
            assert response.status_code == 500
            assert json.loads(response.get_body()) == {
                "detail": "Graph execution failed",
                "request_id": RUN_ID,
            }
        case "stream":
            assert response.status_code == 200
            assert _frames(response) == [
                ("metadata", {"run_id": RUN_ID}),
                ("values", {"n": 1}),
                ("values", {"n": 2}),
                ("values", {"n": 3}),
                ("error", {"error": "stream processing failed", "request_id": RUN_ID}),
                ("end", None),
            ]
    assert b"private-graph-canary" not in response.get_body()
    h.assert_released("error")
    h.assert_sanitized(response)


@pytest.mark.parametrize("capable", [False, True])
@pytest.mark.parametrize("prior_status", ["idle", "interrupted", "error"])
def test_state_update_scopes_guard_only_during_graph_call(
    capable: bool, prior_status: ThreadStatus
) -> None:
    # Given a bound assistant and a status that state updates must preserve.
    lock = GuardableLock(trip_on=1) if capable else PlainLock()
    h = Harness(lock, ProbeGraph())
    h.store.update(THREAD_ID, assistant_id=AGENT, status=prior_status)
    body = {
        "values": {"count": 1},
        "checkpoint": {"checkpoint_id": "cp-old", "checkpoint_ns": "ns"},
    }
    # When the actual state endpoint invokes the graph synchronously.
    response = h.call("threads_state_update", body)
    # Then checkpoint config and context binding agree only during update_state.
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {
        "checkpoint": {
            "thread_id": THREAD_ID,
            "checkpoint_ns": "",
            "checkpoint_id": "cp-1",
            "checkpoint_map": None,
        }
    }
    expected: dict[str, object] = {
        "thread_id": THREAD_ID,
        "checkpoint_id": "cp-old",
        "checkpoint_ns": "ns",
    }
    if capable:
        assert isinstance(lock, GuardableLock)
        guard = lock.guards[0]
        assert guard_in(h.graph.configs[0]) is guard
        assert h.graph.scoped_guards == [guard]
        assert h.graph.scoped_guards[0] is guard
        assert lock.issued == [(AGENT, THREAD_ID, lock.acquired[0])]
        assert guard.checks == 0
        expected[KEY] = guard
    else:
        assert h.graph.scoped_guards == [None]
    assert h.graph.configs == [{"configurable": expected}]
    assert resolve_ownership_guard(None) is None
    h.assert_released(prior_status)
    assert h.observer.events == []


@pytest.mark.parametrize("provider_raises", [False, True])
def test_state_update_resets_binding_when_guard_setup_or_write_fails(provider_raises: bool) -> None:
    # Given loss checked inside update_state, or a provider that fails before it.
    lock = RaisingProviderLock() if provider_raises else GuardableLock(trip_on=1)
    graph = ProbeGraph(during_run=lambda cfg: guard_in(cfg).check())
    h = Harness(lock, graph)
    h.store.update(THREAD_ID, assistant_id=AGENT)
    # When ownership cannot protect the state write.
    response = h.call("threads_state_update", {"values": {"count": 1}})
    # Then both errors follow the existing generic path without leaking the binding.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == {"detail": "Internal error updating thread state"}
    if provider_raises:
        assert graph.configs == []
        assert graph.scoped_guards == []
    else:
        assert isinstance(lock, GuardableLock)
        assert len(graph.configs) == 1
        assert graph.scoped_guards[0] is lock.guards[0]
        assert lock.guards[0].checks == 1
    assert resolve_ownership_guard(None) is None
    h.assert_released("error")
    h.assert_sanitized(response)
    assert h.observer.events == []


@pytest.mark.parametrize("endpoint", ["runs_wait", "runs_stream", "threads_state_update"])
def test_boundary_does_not_issue_guard_when_lock_is_contended(endpoint: str) -> None:
    # Given another execution holding the same real thread-lock key.
    lock = GuardableLock()
    h = Harness(lock, ProbeGraph())
    h.store.update(THREAD_ID, assistant_id=AGENT)
    owner = lock.acquire(AGENT, THREAD_ID)
    assert owner is not None
    try:
        # When a second execution attempts the boundary.
        response = h.call(
            endpoint, {"assistant_id": AGENT} if endpoint.startswith("runs") else {"values": {}}
        )
        # Then rejection precedes guard construction and leaves the other owner alone.
        assert response.status_code == 409
        suffix = (
            "Concurrent runs are not supported (multitask_strategy=reject)."
            if endpoint.startswith("runs")
            else "State updates are not supported while a run is active."
        )
        assert json.loads(response.get_body()) == {
            "detail": f"Thread {THREAD_ID!r} is already busy. {suffix}"
        }
        assert lock.guards == []
        assert lock.acquired == [owner]
        assert lock.released == []
        assert h.graph.configs == []
        assert h.store.releases == []
    finally:
        lock.release(AGENT, THREAD_ID, owner)


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_threadless_run_never_acquires_guard_or_thread_lock(endpoint: Endpoint) -> None:
    # Given a threadless-capable graph and a provider that would fail on checking.
    lock = GuardableLock(trip_on=1)
    h = Harness(lock, ProbeGraph(checkpointer=None))
    # When the dedicated threadless route executes.
    response = h.call(
        f"runs_{endpoint}_threadless",
        {"assistant_id": AGENT, "config": {"configurable": {"user": "u1"}, "tags": ["t1"]}},
    )
    # Then no ownership feature is added to the legacy threadless configuration.
    assert response.status_code == 200
    assert lock.acquired == lock.released == []
    assert lock.guards == []
    assert h.store.releases == []
    assert h.graph.configs == [{"configurable": {"user": "u1"}, "tags": ["t1"]}]
    assert h.observer.events == ["started", "completed"]


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_guard_injection_does_not_modify_client_request(endpoint: Endpoint) -> None:
    # Given a serialized client body with no ownership key at either level.
    lock = GuardableLock()
    h = Harness(lock, ProbeGraph())
    body = {"assistant_id": AGENT, "config": {"configurable": {"user": "u1"}, "tags": ["t1"]}}
    request = h.request(body)
    handler = _get_fn(h.app.function_app, f"aflg_platform_runs_{endpoint}")
    before = request.get_body()
    # When the handler injects its private in-process guard.
    response = handler(request)
    # Then re-parsing the same request remains JSON-safe and guard-free.
    assert response.status_code == 200
    assert guard_in(h.graph.configs[0]) is lock.guards[0]
    assert request.get_body() == before
    assert request.get_json() == body
    assert KEY.encode() not in request.get_body()
    h.assert_released("idle")
