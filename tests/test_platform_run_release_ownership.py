"""Platform runs must not report success when their own release was rejected as stale (#804).

``thread_store.release_run_lock`` deliberately does not throw on a stale owner token: it returns
the unchanged current thread. ``_release_thread_run_lock`` ignored that return value, so a run that
lost ownership AFTER its last renewal tick but BEFORE cleanup (a maintenance reset, or a newer run)
still answered 200, emitted ``on_run_completed`` and discarded its values silently.

Fake-backed: ownership is stolen from inside the graph call, which runs after the renewal worker
started (its 20 s tick never fires in a test) and before the handler's cleanup.

The scenarios are plain ``check_*`` functions over a ``Harness`` so that the Azurite lane
(``tests/integration/test_table_store_integration.py``) can run the very same assertions through a
real Table service; the tests below run them over the in-process stores.
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
from azure_functions_langgraph.platform.stores import (
    InMemoryThreadStore,
    ReleaseOutcomeProvider,
    RunLockReleaseOutcome,
)
from tests.conftest import FakeCompiledGraph
from tests.ownership_guard_support import RecordingObserver
from tests.test_platform_routes import _get_fn, _post_request
from tests.test_stores_azure_table import _new_store

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

    def __init__(self, kind: str, store: Any = None, table: Any = None) -> None:
        """``kind``: ``"memory"`` or ``"table"``; ``store``/``table`` plug in a real service."""
        self.kind = kind
        self.table: Any
        if store is not None:
            self.store: Any = store
            self.table = table
        elif kind == "memory":
            self.store = InMemoryThreadStore()
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

    def steal(
        self,
        *,
        new_owner: bool,
        reset_to: str = "error",
        new_owner_finishes: dict[str, Any] | None = None,
    ) -> None:
        """Ownership is lost after the last renewal tick: reclaimed, optionally re-acquired.

        With ``new_owner_finishes`` the new owner also runs to completion (releases to ``idle``
        with those values) before the stale run gets to release.
        """
        if self.table is None:
            self.store.release_run_lock(self.thread_id, status=reset_to)
        else:
            # A plain merge works on the in-process Table model and on a real service alike.
            self.table.update_entity(
                {
                    "PartitionKey": "thread",
                    "RowKey": self.thread_id,
                    "updated_at": datetime.now(UTC) - timedelta(seconds=900),
                },
                mode="merge",
            )
            assert self.store.reset_stale_locks(older_than_seconds=300, status=reset_to) == 1
        if new_owner:
            owner = self.store.try_acquire_run_lock(self.thread_id)
            assert owner is not None and owner.lock_token is not None
            self.new_owner_token = owner.lock_token
            if new_owner_finishes is not None:
                finished = self.store.release_run_lock(
                    self.thread_id,
                    status="idle",
                    values=new_owner_finishes,
                    owner_token=owner.lock_token,
                )
                assert (finished.status, finished.lock_token) == ("idle", None)

    def run(self, endpoint: str) -> func.HttpResponse:
        handler = _get_fn(self.app.function_app, f"aflg_platform_runs_{endpoint}")
        request = _post_request(
            f"/api/threads/{self.thread_id}/runs/{endpoint}",
            {"assistant_id": "agent", "input": {}},
            thread_id=self.thread_id,
        )
        return cast(func.HttpResponse, handler(request))


def check_a_stale_release_is_reported_as_failure_not_completion(
    h: Harness, endpoint: str, new_owner: bool
) -> None:
    # Given a run that loses the thread after its last renewal tick but before cleanup.
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


def check_another_owner_finishing_first_does_not_hide_a_stale_release(
    h: Harness, endpoint: str
) -> None:
    # Given a run that loses the thread after its last renewal tick, and a newer owner that
    # acquires the thread and runs to completion before the stale run releases.
    h.graph.hook = lambda: h.steal(new_owner=True, new_owner_finishes={"newer": "values"})

    # When the stale run finally releases with its old owner token. The thread the store hands
    # back now looks exactly like a normally finished one: idle, no lock token.
    response = h.run(endpoint)

    # Then the release was still not applied, so the run is a failure, not a completion.
    assert response.status_code == 500
    assert json.loads(response.get_body())["detail"] == "Run-lock release failed"
    assert h.observer.events == ["started", "failed"]
    assert isinstance(h.observer.failures[0], RunLockReleaseRejectedError)
    # And the newer owner's result stands; the stale run's output was not persisted over it.
    after = h.store.get(h.thread_id)
    assert after is not None
    assert (after.status, after.lock_token, after.values) == ("idle", None, {"newer": "values"})
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


def check_the_new_owner_can_still_finish_after_the_stale_release_was_refused(
    h: Harness, endpoint: str
) -> None:
    # Given a stale release that was refused while a newer owner holds the thread.
    h.graph.hook = lambda: h.steal(new_owner=True)
    assert h.run(endpoint).status_code == 500
    assert h.new_owner_token is not None

    # When the newer owner releases with its own token and a result.
    released = h.store.release_run_lock(
        h.thread_id, status="idle", values={"new": 1}, owner_token=h.new_owner_token
    )

    # Then its lock, status and values were never disturbed by the stale run.
    assert (released.status, released.values, released.lock_token) == ("idle", {"new": 1}, None)


def check_an_ordinary_run_still_completes_and_publishes_its_result(
    h: Harness, endpoint: str
) -> None:
    # Given a healthy run.

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


def check_a_graph_failure_stays_the_primary_error_when_the_release_is_also_stale(
    h: Harness, endpoint: str, new_owner: bool
) -> None:
    # Given a run whose graph fails after it already lost the thread.
    h.graph.hook = lambda: h.steal(new_owner=new_owner)
    h.graph.error = RuntimeError(SECRET)

    # When the handler cleans up. The store reports exactly that the release was not applied,
    # also when a reclaim left the thread at the very status the failed run asks for ("error").
    response = h.run(endpoint)

    # Then the graph error is the one failure reported (once) and it is sanitized. A refused
    # release replaces a buffered stream error frame with the sanitized JSON 500, as any other
    # release failure does.
    assert response.status_code == 500
    assert json.loads(response.get_body())["detail"] == "Graph execution failed"
    assert SECRET.encode() not in response.get_body()
    assert h.observer.events == ["started", "failed"]
    assert str(h.observer.failures[0]) == SECRET
    assert not isinstance(h.observer.failures[0], RunLockReleaseRejectedError)
    # And the other party's state is untouched and the outer lock is released once.
    after = h.store.get(h.thread_id)
    assert after is not None
    if new_owner:
        assert (after.status, after.lock_token) == ("busy", h.new_owner_token)
    else:
        assert (after.status, after.lock_token) == ("error", None)
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("new_owner", [False, True], ids=["reclaimed", "reacquired"])
def test_a_stale_release_is_reported_as_failure_not_completion(
    endpoint: str, store_kind: str, new_owner: bool
) -> None:
    check_a_stale_release_is_reported_as_failure_not_completion(
        Harness(store_kind), endpoint, new_owner
    )


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_the_new_owner_can_still_finish_after_the_stale_release_was_refused(
    endpoint: str, store_kind: str
) -> None:
    check_the_new_owner_can_still_finish_after_the_stale_release_was_refused(
        Harness(store_kind), endpoint
    )


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_an_ordinary_run_still_completes_and_publishes_its_result(
    endpoint: str, store_kind: str
) -> None:
    check_an_ordinary_run_still_completes_and_publishes_its_result(Harness(store_kind), endpoint)


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("new_owner", [False, True], ids=["reclaimed", "reacquired"])
def test_a_graph_failure_stays_the_primary_error_when_the_release_is_also_stale(
    endpoint: str, store_kind: str, new_owner: bool
) -> None:
    check_a_graph_failure_stays_the_primary_error_when_the_release_is_also_stale(
        Harness(store_kind), endpoint, new_owner
    )


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_another_owner_finishing_first_does_not_hide_a_stale_release(
    endpoint: str, store_kind: str
) -> None:
    check_another_owner_finishing_first_does_not_hide_a_stale_release(Harness(store_kind), endpoint)


class PlainStore:
    """A custom store that implements only the ``ThreadStore`` protocol: no release outcome.

    It forwards to a bundled store but exposes nothing else, so the run handlers have only the
    returned thread to judge a release by.
    """

    FORWARDED = (
        "create",
        "get",
        "update",
        "delete",
        "search",
        "count",
        "try_acquire_run_lock",
        "release_run_lock",
        "renew_run_lock",
        "reset_stale_locks",
    )

    def __init__(self, inner: Any) -> None:
        for name in self.FORWARDED:
            if hasattr(inner, name):
                setattr(self, name, getattr(inner, name))


def _plain_harness(kind: str) -> Harness:
    inner: Any
    table: Any = None
    if kind == "memory":
        inner = InMemoryThreadStore()
    else:
        inner, table = _new_store()
    plain = PlainStore(inner)
    assert not isinstance(plain, ReleaseOutcomeProvider)
    return Harness(kind, plain, table)


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("new_owner", [False, True], ids=["reclaimed", "reacquired"])
def test_a_store_without_the_outcome_still_rejects_what_the_returned_thread_exposes(
    endpoint: str, store_kind: str, new_owner: bool
) -> None:
    # A reclaim to "error" (the run asked for "idle") and a thread busy under a new owner both
    # differ from the requested status, so the fallback comparison still catches them.
    check_a_stale_release_is_reported_as_failure_not_completion(
        _plain_harness(store_kind), endpoint, new_owner
    )


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_a_store_without_the_outcome_still_completes_ordinary_runs(
    endpoint: str, store_kind: str
) -> None:
    check_an_ordinary_run_still_completes_and_publishes_its_result(
        _plain_harness(store_kind), endpoint
    )


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_a_store_without_the_outcome_cannot_see_a_finished_new_owner_documented_limit(
    endpoint: str,
) -> None:
    # Given the ordering above on a store that cannot say whether its release was applied.
    h = _plain_harness("memory")
    h.graph.hook = lambda: h.steal(new_owner=True, new_owner_finishes={"newer": "values"})

    # When the stale run releases: the returned thread is idle with no token, exactly what an
    # applied release returns, and nothing else is known.
    response = h.run(endpoint)

    # Then the run is reported as completed. This is the documented limit of the fallback; a
    # store that implements the outcome closes it (see the tests above).
    assert response.status_code == 200
    assert h.observer.events == ["started", "completed"]
    after = h.store.get(h.thread_id)
    assert after is not None and after.values == {"newer": "values"}


class _ScriptedOutcomeStore:
    """A store whose release outcome is scripted: it reports ``applied`` and returns ``thread``."""

    def __init__(self, inner: InMemoryThreadStore, *, applied: bool, thread_override: Any) -> None:
        self._inner = inner
        self._applied = applied
        self._override = thread_override
        for name in PlainStore.FORWARDED:
            if hasattr(inner, name):
                setattr(self, name, getattr(inner, name))

    def release_run_lock_outcome(self, thread_id: str, **kwargs: Any) -> RunLockReleaseOutcome:
        released = self._inner.release_run_lock(thread_id, **kwargs)
        thread = released if self._override is None else released.model_copy(update=self._override)
        return RunLockReleaseOutcome(thread, applied=self._applied)


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize(
    ("applied", "override"),
    [
        (False, None),
        (True, {"status": "busy"}),
        (True, {"lock_token": "left-behind"}),
        (True, {"status": "error"}),
    ],
    ids=[
        "reported-not-applied",
        "applied-but-busy",
        "applied-but-token-left",
        "applied-but-status",
    ],
)
def test_the_reported_outcome_and_the_returned_thread_must_both_say_success(
    endpoint: str, applied: bool, override: dict[str, Any] | None
) -> None:
    # Given a store that reports its own outcome: a refusal beside a clean-looking thread, or
    # an "applied" beside a thread that contradicts it.
    inner = InMemoryThreadStore()
    scripted = _ScriptedOutcomeStore(inner, applied=applied, thread_override=override)
    assert isinstance(scripted, ReleaseOutcomeProvider)
    h = Harness("memory", scripted)

    # When the run completes and releases.
    response = h.run(endpoint)

    # Then either signal alone is enough to fail the run.
    assert response.status_code == 500
    assert h.observer.events == ["started", "failed"]
    assert isinstance(h.observer.failures[0], RunLockReleaseRejectedError)


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
