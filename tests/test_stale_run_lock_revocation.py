"""Stale-owner revocation for Platform run locks (#808).

``reset_stale_locks`` reclaims a ``busy`` thread whose worker went away. The old worker may
still come back and release with its old owner token. The reclaimed disposition must win:
reclaiming revokes the token, and a release or renewal on a thread that is no longer ``busy``
is refused even when a legacy row still carries the old token. Fake-backed only: the Azure
Table CAS semantics are modeled by ``MockTableClient`` and the Azurite lane covers the SDK.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from tests.test_stores_azure_table import MockTableClient, _new_store

PARTITION = "thread"
LOCK_FIELDS = ("lock_token", "lock_expires_at")


def _raw(table: MockTableClient, thread_id: str) -> dict[str, Any]:
    return table.entities[(PARTITION, thread_id)]


def _acquire_stale(store: Any, table: MockTableClient) -> tuple[str, str]:
    """A busy thread whose worker is gone: returns ``(thread_id, old_owner_token)``."""
    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id)
    assert locked is not None and locked.lock_token is not None
    _raw(table, thread.thread_id)["updated_at"] = datetime.now(UTC) - timedelta(seconds=900)
    return thread.thread_id, locked.lock_token


@pytest.mark.parametrize("reset_to", ["error", "idle"])
def test_reset_revokes_the_old_owner_so_a_late_release_cannot_undo_recovery(
    reset_to: str,
) -> None:
    # Given a stale busy thread that was reclaimed by the maintenance sweep.
    store, table = _new_store()
    thread_id, old_token = _acquire_stale(store, table)
    assert store.reset_stale_locks(older_than_seconds=300, status=reset_to) == 1
    recovered = dict(_raw(table, thread_id))
    attempts = table.update_attempts

    # When the old worker comes back and releases with its old token and a result.
    outcome = store.release_run_lock(
        thread_id, status="idle", values={"stale": "result"}, owner_token=old_token
    )

    # Then the recovery disposition stands: nothing was written, and the row has no lock.
    assert outcome.status == reset_to
    assert outcome.values is None
    assert outcome.lock_token is None
    assert table.update_attempts == attempts
    assert _raw(table, thread_id) == recovered
    assert all(field not in recovered for field in LOCK_FIELDS)


def test_reset_clears_the_lock_fields_atomically_with_the_new_status() -> None:
    # Given a stale busy thread carrying metadata, assistant binding and values.
    store, table = _new_store()
    thread = store.create(metadata={"keep": True})
    locked = store.try_acquire_run_lock(thread.thread_id, assistant_id="agent")
    assert locked is not None
    store.update(thread.thread_id, values={"progress": 1})
    _raw(table, thread.thread_id)["updated_at"] = datetime.now(UTC) - timedelta(seconds=900)

    # When the sweep reclaims it.
    assert store.reset_stale_locks(older_than_seconds=300) == 1

    # Then only the lock fields are gone; everything else is preserved in the same write.
    after = store.get(thread.thread_id)
    assert after is not None
    assert (after.status, after.assistant_id, after.metadata, after.values) == (
        "error",
        "agent",
        {"keep": True},
        {"progress": 1},
    )
    assert after.lock_token is None and after.lock_expires_at is None
    assert table.last_update_mode == "replace"


def test_a_new_owner_acquiring_right_after_reset_is_not_disturbed_by_the_old_release() -> None:
    # Given a reclaimed thread that a new run (B) acquires immediately.
    store, table = _new_store()
    thread_id, old_token = _acquire_stale(store, table)
    assert store.reset_stale_locks(older_than_seconds=300) == 1
    new_owner = store.try_acquire_run_lock(thread_id)
    assert new_owner is not None and new_owner.lock_token not in {None, old_token}
    attempts = table.update_attempts

    # When the old worker releases, and then renews, with its old token.
    refused = store.release_run_lock(
        thread_id, status="idle", values={"stale": 1}, owner_token=old_token
    )
    renewed = store.renew_run_lock(thread_id, old_token)

    # Then B's lock, status and values are untouched, and B can still finish normally.
    assert (refused.status, refused.lock_token) == ("busy", new_owner.lock_token)
    assert renewed is False
    assert table.update_attempts == attempts
    released = store.release_run_lock(
        thread_id, status="idle", values={"fresh": 1}, owner_token=new_owner.lock_token
    )
    assert (released.status, released.values, released.lock_token) == ("idle", {"fresh": 1}, None)


def test_a_legacy_reclaimed_row_that_still_carries_the_old_token_rejects_a_stale_release() -> None:
    # Given a row reclaimed by the earlier (merge-only) reset: not busy, token left behind.
    store, table = _new_store()
    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id)
    assert locked is not None and locked.lock_token is not None
    raw = _raw(table, thread.thread_id)
    raw["status"] = "error"
    assert raw["lock_token"] == locked.lock_token
    attempts = table.update_attempts

    # When the old worker releases and renews with that lingering token.
    outcome = store.release_run_lock(
        thread.thread_id, status="idle", values={"stale": 1}, owner_token=locked.lock_token
    )
    renewed = store.renew_run_lock(thread.thread_id, locked.lock_token)

    # Then both are refused because the thread is no longer busy, and nothing is written.
    assert outcome.status == "error" and outcome.values is None
    assert renewed is False
    assert table.update_attempts == attempts


def test_an_owner_release_without_a_token_still_works_on_a_busy_thread() -> None:
    # Given a busy thread released by an administrative caller that has no owner token.
    store, _ = _new_store()
    thread = store.create()
    assert store.try_acquire_run_lock(thread.thread_id) is not None

    # When it releases without an owner token (the documented best-effort path).
    outcome = store.release_run_lock(thread.thread_id, status="idle")

    # Then it releases as before.
    assert outcome.status == "idle" and outcome.lock_token is None


def test_reset_skips_a_thread_whose_owner_renewed_between_the_scan_and_the_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a stale-looking row whose worker renews just after the sweep's query.
    store, table = _new_store()
    thread_id, token = _acquire_stale(store, table)
    original_query = table.query_entities

    def query_then_renew(query_filter: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        rows = original_query(query_filter, **kwargs)
        assert store.renew_run_lock(thread_id, token) is True
        return rows

    monkeypatch.setattr(table, "query_entities", query_then_renew)

    # When the sweep continues with the row it had already selected.
    count = store.reset_stale_locks(older_than_seconds=300)

    # Then it re-verifies, sees a live owner, and leaves the lock alone.
    assert count == 0
    after = store.get(thread_id)
    assert after is not None and after.status == "busy" and after.lock_token == token


def test_reset_loses_the_race_to_a_renewal_between_its_read_and_its_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a renewal that lands after the reset read the row but before it wrote.
    store, table = _new_store()
    thread_id, token = _acquire_stale(store, table)
    original_update = table.update_entity
    renewed = {"done": False}

    def renew_then_update(entity: dict[str, Any], mode: str, **kwargs: Any) -> None:
        if not renewed["done"] and entity.get("status") in {"error", "idle"}:
            renewed["done"] = True
            assert store.renew_run_lock(thread_id, token) is True
        original_update(entity, mode, **kwargs)

    monkeypatch.setattr(table, "update_entity", renew_then_update)

    # When the reset's ETag-conditional write goes out.
    count = store.reset_stale_locks(older_than_seconds=300)

    # Then the ETag no longer matches: the reset is skipped and the owner keeps its lock.
    assert renewed["done"] is True and count == 0
    after = store.get(thread_id)
    assert after is not None and after.status == "busy" and after.lock_token == token


def test_reset_leaves_a_deleted_or_already_released_thread_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given two stale candidates: one is released and one is deleted after the query.
    store, table = _new_store()
    released_id, released_token = _acquire_stale(store, table)
    deleted_id, _ = _acquire_stale(store, table)
    original_query = table.query_entities

    def query_then_change(query_filter: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        rows = original_query(query_filter, **kwargs)
        store.release_run_lock(released_id, status="idle", owner_token=released_token)
        del table.entities[(PARTITION, deleted_id)]
        return rows

    monkeypatch.setattr(table, "query_entities", query_then_change)

    # When the sweep reaches them.
    count = store.reset_stale_locks(older_than_seconds=300)

    # Then neither is touched: the released one keeps its own disposition.
    assert count == 0
    released = store.get(released_id)
    assert released is not None and released.status == "idle"
    assert store.get(deleted_id) is None


def test_reset_takes_its_etag_from_the_full_reread_not_from_the_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a scan whose projected row is stale: it carries an ETag the row no longer has.
    store, table = _new_store()
    thread_id, _ = _acquire_stale(store, table)
    current_etag = _raw(table, thread_id)["etag"]
    original_query = table.query_entities

    def query_with_stale_etag(
        query_filter: str | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        rows = original_query(query_filter, **kwargs)
        for row in rows:
            cast(Any, row).metadata = {"etag": 'W/"stale-projection"'}
        return rows

    monkeypatch.setattr(table, "query_entities", query_with_stale_etag)

    # When the sweep reclaims it.
    count = store.reset_stale_locks(older_than_seconds=300)

    # Then the conditional write used the ETag of the fresh read, so the reset applied.
    assert count == 1
    assert table.last_update_kwargs["etag"] == current_etag
    after = store.get(thread_id)
    assert after is not None and after.status == "error"


# ---------------------------------------------------------------------------
# The in-memory store has no reset sweep, but it shares the release/renew contract.
# ---------------------------------------------------------------------------


def test_in_memory_release_and_renew_refuse_a_thread_that_is_no_longer_busy() -> None:
    # Given a thread whose busy state was ended behind its owner's back.
    store = InMemoryThreadStore()
    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id)
    assert locked is not None and locked.lock_token is not None
    store.update(thread.thread_id, status="error")

    # When the old owner releases and renews with its token.
    outcome = store.release_run_lock(
        thread.thread_id, status="idle", values={"stale": 1}, owner_token=locked.lock_token
    )
    renewed = store.renew_run_lock(thread.thread_id, locked.lock_token)

    # Then the disposition stands and neither call changed the thread.
    assert outcome.status == "error" and outcome.values is None
    assert renewed is False
    current = store.get(thread.thread_id)
    assert current is not None and current.status == "error" and current.values is None


def test_in_memory_normal_release_still_works() -> None:
    # Given an ordinary owner.
    store = InMemoryThreadStore()
    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id)
    assert locked is not None and locked.lock_token is not None
    assert store.renew_run_lock(thread.thread_id, locked.lock_token) is True

    # When it releases with its token and a result.
    outcome = cast(Any, store).release_run_lock(
        thread.thread_id, status="idle", values={"ok": 1}, owner_token=locked.lock_token
    )

    # Then the release is applied.
    assert (outcome.status, outcome.values, outcome.lock_token) == ("idle", {"ok": 1}, None)
