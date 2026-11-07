"""Platform runs must not report success when their own release was rejected as stale (#804).

``thread_store.release_run_lock`` deliberately does not throw on a stale owner token: it returns
the unchanged current thread. ``_release_thread_run_lock`` ignored that return value, so a run that
lost ownership AFTER its last renewal tick but BEFORE cleanup (a maintenance reset, or a newer run)
still answered 200, emitted ``on_run_completed`` and discarded its values silently.

Fake-backed: ownership is stolen from inside the graph call, which runs after the renewal worker
started (its 20 s tick never fires in a test) and before the handler's cleanup.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
from typing import Any, cast

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.platform._store_renewal import RunLockReleaseRejectedError
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from tests.conftest import FakeCompiledGraph
from tests.ownership_guard_support import RecordingObserver
from tests.test_platform_routes import _get_fn, _post_request
from tests.test_stores_azure_table import MockTableClient, _new_store

SECRET = "private-graph-canary"


class RecordingLock(InProcessThreadLock):
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


class HookedGraph(FakeCompiledGraph):
    """Runs ``hook`` while the run is executing, then behaves like the normal fake graph."""

    def __init__(self) -> None:
        super().__init__(checkpointer=None)
        self.hook: Callable[[], None] | None = None
        self.error: Exception | None = None

    def _during(self) -> None:
        if self.hook is not None:
            self.hook()
        if self.error is not None:
            raise self.error

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        self._during()
        return super().invoke(input, config)

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
        **kwargs: Any,
    ) -> Any:
        self._during()
        yield from super().stream(input, config, stream_mode)


class Harness:
    """A Platform app over a chosen store, with ownership stolen from inside the graph call."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.table: MockTableClient | None
        if kind == "memory":
            self.store: Any = InMemoryThreadStore()
            self.table = None
        else:
            self.store, self.table = _new_store()
        self.lock = RecordingLock()
        self.observer = RecordingObserver()
        self.graph = HookedGraph()
        self.app = LangGraphApp(platform_compat=True, thread_lock=self.lock, observer=self.observer)
        self.app._thread_store = self.store
        self.app.register(graph=self.graph, name="agent")
        self.thread_id = self.store.create().thread_id
        self.new_owner_token: str | None = None

    def steal(self, *, new_owner: bool, reset_to: str = "error") -> None:
        """Ownership is lost after the last renewal tick: reclaimed, optionally re-acquired."""
        if self.table is None:
            self.store.release_run_lock(self.thread_id, status=reset_to)
        else:
            self.table.entities[("thread", self.thread_id)]["updated_at"] = datetime.now(
                UTC
            ) - timedelta(seconds=900)
            assert self.store.reset_stale_locks(older_than_seconds=300, status=reset_to) == 1
        if new_owner:
            owner = self.store.try_acquire_run_lock(self.thread_id)
            assert owner is not None and owner.lock_token is not None
            self.new_owner_token = owner.lock_token

    def run(self, endpoint: str) -> func.HttpResponse:
        handler = _get_fn(self.app.function_app, f"aflg_platform_runs_{endpoint}")
        request = _post_request(
            f"/api/threads/{self.thread_id}/runs/{endpoint}",
            {"assistant_id": "agent", "input": {}},
            thread_id=self.thread_id,
        )
        return cast(func.HttpResponse, handler(request))


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("new_owner", [False, True], ids=["reclaimed", "reacquired"])
def test_a_stale_release_is_reported_as_failure_not_completion(
    endpoint: str, store_kind: str, new_owner: bool
) -> None:
    # Given a run that loses the thread after its last renewal tick but before cleanup.
    h = Harness(store_kind)
    h.graph.hook = lambda: h.steal(new_owner=new_owner)

    # When the graph returns normally and the handler releases with its old owner token.
    response = h.run(endpoint)

    # Then the caller gets a sanitized failure, not 200 and not a successful stream.
    assert response.status_code == 500
    body = json.loads(response.get_body())
    assert body["detail"] == "Run-lock release failed"
    assert b"event: end" not in response.get_body()
    # And exactly one failed terminal event is emitted, never a completion.
    assert h.observer.events == ["started", "failed"]
    assert isinstance(h.observer.failures[0], RunLockReleaseRejectedError)
    # And the other party's state is preserved: the reclaimed disposition or the new owner.
    after = h.store.get(h.thread_id)
    assert after is not None and after.values is None
    if new_owner:
        assert (after.status, after.lock_token) == ("busy", h.new_owner_token)
    else:
        assert (after.status, after.lock_token) == ("error", None)
    # And the outer thread lock is released exactly once.
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_the_new_owner_can_still_finish_after_the_stale_release_was_refused(
    endpoint: str, store_kind: str
) -> None:
    # Given a stale release that was refused while a newer owner holds the thread.
    h = Harness(store_kind)
    h.graph.hook = lambda: h.steal(new_owner=True)
    assert h.run(endpoint).status_code == 500
    assert h.new_owner_token is not None

    # When the newer owner releases with its own token and a result.
    released = h.store.release_run_lock(
        h.thread_id, status="idle", values={"new": 1}, owner_token=h.new_owner_token
    )

    # Then its lock, status and values were never disturbed by the stale run.
    assert (released.status, released.values, released.lock_token) == ("idle", {"new": 1}, None)


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_an_ordinary_run_still_completes_and_publishes_its_result(
    endpoint: str, store_kind: str
) -> None:
    # Given a healthy run.
    h = Harness(store_kind)

    # When it completes and releases normally.
    response = h.run(endpoint)

    # Then it is a success with one completed event, the thread is idle and nothing is blocked.
    assert response.status_code == 200
    assert h.observer.events == ["started", "completed"]
    after = h.store.get(h.thread_id)
    assert after is not None and (after.status, after.lock_token) == ("idle", None)
    if endpoint == "wait":
        assert after.values == json.loads(response.get_body())
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_a_graph_failure_stays_the_primary_error_when_the_release_is_also_stale(
    endpoint: str, store_kind: str
) -> None:
    # Given a run whose graph fails after it already lost the thread.
    h = Harness(store_kind)
    h.graph.hook = lambda: h.steal(new_owner=False)
    h.graph.error = RuntimeError(SECRET)

    # When the handler cleans up.
    response = h.run(endpoint)

    # Then the graph error is what is reported (once), sanitized, and the stale release
    # neither adds a second terminal event nor overrides the reclaimed disposition.
    if endpoint == "wait":
        assert response.status_code == 500
        assert json.loads(response.get_body())["detail"] == "Graph execution failed"
    else:
        assert response.status_code == 200
        assert b"event: error" in response.get_body()
    assert SECRET.encode() not in response.get_body()
    assert h.observer.events == ["started", "failed"]
    assert str(h.observer.failures[0]) == SECRET
    after = h.store.get(h.thread_id)
    assert after is not None and (after.status, after.lock_token) == ("error", None)
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_a_release_that_leaves_the_lock_token_in_place_is_not_a_completion(endpoint: str) -> None:
    # Given a store whose release reports the requested terminal status but did not clear
    # the owner token (the same condition the state-update helper already rejects).
    h = Harness("memory")
    real_release = h.store.release_run_lock

    def release_without_clearing(*args: Any, **kwargs: Any) -> Any:
        released = real_release(*args, **kwargs)
        return released.model_copy(update={"lock_token": "left-behind"})

    h.store.release_run_lock = release_without_clearing

    # When the run completes and the handler releases.
    response = h.run(endpoint)

    # Then it is a failure, not a success.
    assert response.status_code == 500
    assert h.observer.events == ["started", "failed"]
    assert isinstance(h.observer.failures[0], RunLockReleaseRejectedError)
