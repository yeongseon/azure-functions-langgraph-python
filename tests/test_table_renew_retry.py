"""Renewal of a Table-backed run lock must survive unrelated writes to its row (#805).

A metadata-only write (``PATCH /threads/{id}``, a run's own metadata merge) changes the row's
ETag without changing its owner. ``renew_run_lock`` used to treat the resulting conditional-write
conflict as lost ownership, so the Platform renewal worker failed a perfectly healthy long run.
A renewal now re-reads the row after a conflict and re-checks the owner before writing again,
for at most three attempts. Fake-backed: the Table's compare-and-swap semantics are modeled by
``MockTableClient``, which is what lets every interleaving be forced deterministically and keeps
these tests in the coverage-gated suite. The same scenarios run against the real Table service,
through the real SDK and real PATCH requests, in the Azurite lane
(``tests/integration/test_table_store_integration.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any

import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.platform import _store_renewal
from azure_functions_langgraph.stores._errors import RunLockRenewalConflictError
from tests.conftest import FakeCompiledGraph
from tests.ownership_guard_support import RecordingObserver
from tests.test_platform_routes import _get_fn, _post_request
from tests.test_stores_azure_table import (
    FakeResourceModifiedError,
    FakeResourceNotFoundError,
    MockTableClient,
    _new_store,
)

PARTITION = "thread"
RENEWAL_KEYS = {"PartitionKey", "RowKey", "updated_at", "lock_expires_at"}


def _is_renewal_write(entity: dict[str, Any]) -> bool:
    return set(entity) == RENEWAL_KEYS


def _race_the_renewal(
    table: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    times: int,
    concurrent: Callable[[], object],
) -> list[int]:
    """Run ``concurrent`` right before each of the first ``times`` renewal writes.

    The write that follows then carries a stale ETag, exactly as it would if another request
    had written to the row between the renewal's read and its conditional write.
    """
    original = table.update_entity
    fired: list[int] = []

    def update_entity(entity: dict[str, Any], mode: str, **kwargs: Any) -> None:
        if _is_renewal_write(entity) and len(fired) < times:
            fired.append(1)
            concurrent()
        original(entity, mode, **kwargs)

    monkeypatch.setattr(table, "update_entity", update_entity)
    return fired


def _stale_lock(store: Any, table: MockTableClient) -> tuple[str, str]:
    """A busy thread whose lock timestamps are long past: ``(thread_id, owner_token)``."""
    thread = store.create(metadata={"original": True})
    locked = store.try_acquire_run_lock(thread.thread_id, assistant_id="agent")
    assert locked is not None and locked.lock_token is not None
    row = table.entities[(PARTITION, thread.thread_id)]
    row["updated_at"] = datetime.now(UTC) - timedelta(seconds=900)
    row["lock_expires_at"] = datetime.now(UTC) - timedelta(seconds=840)
    return thread.thread_id, locked.lock_token


@pytest.mark.parametrize("conflicts", [1, 2])
def test_a_renewal_survives_a_metadata_only_write_to_the_same_row(
    conflicts: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a live owner whose renewal races metadata-only writes (same owner token throughout).
    store, table = _new_store()
    thread_id, token = _stale_lock(store, table)
    fired = _race_the_renewal(
        table,
        monkeypatch,
        times=conflicts,
        concurrent=lambda: store.update(thread_id, metadata={"patched": True}),
    )

    # When the renewal's conditional write loses the race and is retried.
    renewed = store.renew_run_lock(thread_id, token)

    # Then the owner is not reported as lost, the concurrent write survives, and the lock
    # timestamps were refreshed by exactly the retried renewal.
    assert renewed is True
    assert len(fired) == conflicts
    row = table.entities[(PARTITION, thread_id)]
    assert row["lock_token"] == token and row["status"] == "busy"
    assert row["lock_expires_at"] > datetime.now(UTC) + timedelta(seconds=30)
    assert row["updated_at"] > datetime.now(UTC) - timedelta(seconds=30)
    persisted = store.get(thread_id)
    assert persisted is not None and persisted.metadata == {"patched": True}
    assert persisted.assistant_id == "agent"


def test_an_undisturbed_renewal_still_needs_one_attempt() -> None:
    # Given a live owner and nobody else writing.
    store, table = _new_store()
    thread_id, token = _stale_lock(store, table)
    attempts = table.update_attempts

    # When it renews.
    renewed = store.renew_run_lock(thread_id, token)

    # Then it is a single conditional write.
    assert renewed is True
    assert table.update_attempts - attempts == 1


@pytest.mark.parametrize("how", ["replaced", "released", "reclaimed"])
def test_a_conflicting_write_that_ends_the_ownership_is_reported_as_lost(
    how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a renewal that loses its write to a write that takes the thread away from its owner.
    store, table = _new_store()
    thread_id, token = _stale_lock(store, table)

    def end_the_ownership() -> None:
        if how == "replaced":
            store.update(thread_id, status="idle")
            assert store.try_acquire_run_lock(thread_id) is not None
        elif how == "released":
            store.release_run_lock(thread_id, status="idle", owner_token=token)
        else:
            assert store.reset_stale_locks(older_than_seconds=300) == 1

    fired = _race_the_renewal(table, monkeypatch, times=1, concurrent=end_the_ownership)
    expected_status = {"replaced": "busy", "released": "idle", "reclaimed": "error"}[how]

    # When the renewal re-reads the row after its conflict.
    renewed = store.renew_run_lock(thread_id, token)
    after = deepcopy(table.entities[(PARTITION, thread_id)])

    # Then it reports lost ownership and does not write over the thread's new state.
    assert renewed is False
    assert len(fired) == 1
    assert after["status"] == expected_status
    assert after.get("lock_token") != token
    if how == "replaced":
        assert after["lock_token"] is not None
    else:
        # A stale renewal that wrote anyway would have put lock_expires_at back on the row.
        assert "lock_token" not in after and "lock_expires_at" not in after


def test_a_renewal_never_overwrites_the_new_owners_lock_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an old owner whose renewal loses to a newer owner's acquisition.
    store, table = _new_store()
    thread_id, token = _stale_lock(store, table)
    snapshot: dict[str, Any] = {}

    def new_owner_takes_over() -> None:
        store.update(thread_id, status="idle")
        owner = store.try_acquire_run_lock(thread_id)
        assert owner is not None
        snapshot.update(deepcopy(table.entities[(PARTITION, thread_id)]))

    _race_the_renewal(table, monkeypatch, times=1, concurrent=new_owner_takes_over)

    # When the old owner retries.
    renewed = store.renew_run_lock(thread_id, token)

    # Then the newer owner's row is exactly as it left it.
    assert renewed is False
    current = dict(table.entities[(PARTITION, thread_id)])
    assert current == snapshot


def test_renewal_raises_after_bounded_conflicts_while_still_owning_the_lock() -> None:
    # Given an unchanged owner but persistent ETag conflicts on every write.
    store, table = _new_store()
    thread_id, token = _stale_lock(store, table)
    table.always_modified_error = True
    attempts = table.update_attempts

    # When the renewal exhausts its attempts.
    with pytest.raises(RunLockRenewalConflictError) as error:
        store.renew_run_lock(thread_id, token)

    # Then exhaustion is distinguishable from lost ownership, bounded at three attempts, and
    # the diagnostic cause is the last conflict.
    assert isinstance(error.value.__cause__, FakeResourceModifiedError)
    assert table.update_attempts - attempts == 3
    assert thread_id in str(error.value)
    row = table.entities[(PARTITION, thread_id)]
    assert row["lock_token"] == token and row["status"] == "busy"


@pytest.mark.parametrize("phase", ["read", "write"])
def test_storage_errors_are_not_mistaken_for_lost_ownership_or_retried(
    phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an owner whose renewal hits an unrelated storage failure on one specific call.
    store, table = _new_store()
    thread_id, token = _stale_lock(store, table)
    error: Exception = TimeoutError("private-read") if phase == "read" else OSError("private-write")
    calls: list[str] = []

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls.append(phase)
        raise error

    monkeypatch.setattr(table, "get_entity" if phase == "read" else "update_entity", failing)

    # When it renews.
    with pytest.raises(type(error)) as caught:
        store.renew_run_lock(thread_id, token)

    # Then that very error propagates after exactly one call: it is neither retried like a
    # conflict nor converted into False or into the conflict-exhaustion error.
    assert caught.value is error
    assert calls == [phase]


def test_a_deleted_thread_still_raises_instead_of_reporting_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a thread deleted between the renewal's read and its write.
    store, table = _new_store()
    thread_id, token = _stale_lock(store, table)
    _race_the_renewal(table, monkeypatch, times=1, concurrent=lambda: store.delete(thread_id))

    # When the renewal's write finds no row.
    with pytest.raises(FakeResourceNotFoundError):
        store.renew_run_lock(thread_id, token)

    # Then the missing row is surfaced, as before, not turned into a retry or a False.
    assert (PARTITION, thread_id) not in table.entities


# ---------------------------------------------------------------------------
# Through the Platform handlers: a healthy long run must not be failed by someone else's write.
# ---------------------------------------------------------------------------


class _App:
    """A Platform app over any ``AzureTableThreadStore``: the fake Table or a real service."""

    def __init__(self, endpoint: str, store: Any, table: Any) -> None:
        self.store, self.table = store, table
        self.lock = InProcessThreadLock()
        self.observer = RecordingObserver()
        self.graph = FakeCompiledGraph(checkpointer=None)
        self.app = LangGraphApp(platform_compat=True, thread_lock=self.lock, observer=self.observer)
        self.app._thread_store = self.store
        self.app.register(graph=self.graph, name="agent")
        self.thread_id = self.store.create().thread_id
        self.handler = _get_fn(self.app.function_app, f"aflg_platform_runs_{endpoint}")
        self.request = _post_request(
            f"/api/threads/{self.thread_id}/runs/{endpoint}",
            {"assistant_id": "agent", "input": {}},
            thread_id=self.thread_id,
        )


def _wait_for_one_renewal(app: _App, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Make the graph wait until the renewal worker has finished (or failed) one renewal."""
    outcomes: list[Any] = []
    done = Event()
    original = app.store.renew_run_lock

    def recording_renew(thread_id: str, owner_token: str) -> bool:
        try:
            outcome = original(thread_id, owner_token)
        except Exception as exc:
            outcomes.append(exc)
            done.set()
            raise
        outcomes.append(outcome)
        done.set()
        return bool(outcome)

    monkeypatch.setattr(app.store, "renew_run_lock", recording_renew)
    monkeypatch.setattr(_store_renewal, "RENEWAL_INTERVAL", 0.01)
    graph_invoke = app.graph.invoke
    graph_stream = app.graph.stream

    def invoke(*args: Any, **kwargs: Any) -> Any:
        assert done.wait(5)
        return graph_invoke(*args, **kwargs)

    def stream(*args: Any, **kwargs: Any) -> Any:
        assert done.wait(5)
        yield from graph_stream(*args, **kwargs)

    monkeypatch.setattr(app.graph, "invoke", invoke)
    monkeypatch.setattr(app.graph, "stream", stream)
    return outcomes


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_a_metadata_write_during_a_run_does_not_fail_the_run(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a run whose first renewal races a metadata-only write to the thread.
    app = _App(endpoint, *_new_store())
    outcomes = _wait_for_one_renewal(app, monkeypatch)
    fired = _race_the_renewal(
        app.table,
        monkeypatch,
        times=1,
        concurrent=lambda: app.store.update(app.thread_id, metadata={"patched": True}),
    )

    # When the run executes and completes.
    response = app.handler(app.request)

    # Then the renewal succeeded and the run is an ordinary success.
    assert len(fired) == 1
    assert outcomes[0] is True
    assert response.status_code == 200
    assert app.observer.events == ["started", "completed"]
    after = app.store.get(app.thread_id)
    assert after is not None and (after.status, after.lock_token) == ("idle", None)
    assert after.metadata == {"patched": True}
    token = app.lock.acquire("agent", app.thread_id)
    assert token is not None


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_a_run_is_still_failed_safely_when_renewal_conflicts_are_exhausted(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a run whose renewal writes conflict on every attempt (the owner is never replaced).
    app = _App(endpoint, *_new_store())
    outcomes = _wait_for_one_renewal(app, monkeypatch)
    original = app.table.update_entity

    def conflict_on_renewals(entity: dict[str, Any], mode: str, **kwargs: Any) -> None:
        if _is_renewal_write(entity):
            raise FakeResourceModifiedError("private-storage-canary")
        original(entity, mode, **kwargs)

    monkeypatch.setattr(app.table, "update_entity", conflict_on_renewals)

    # When the run executes.
    response = app.handler(app.request)

    # Then the caller gets the sanitized failure, never a success, with the exhaustion error
    # (not a bogus "lost ownership") as the one failed event, and the thread ends in error.
    assert isinstance(outcomes[0], RunLockRenewalConflictError)
    assert response.status_code == 500
    assert b"private-storage-canary" not in response.get_body()
    assert app.observer.events == ["started", "failed"]
    assert isinstance(app.observer.failures[0], RunLockRenewalConflictError)
    after = app.store.get(app.thread_id)
    assert after is not None and (after.status, after.lock_token) == ("error", None)
    token = app.lock.acquire("agent", app.thread_id)
    assert token is not None
