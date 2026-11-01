"""Table lease reconstruction preserves ownership without exposing it publicly."""

from datetime import UTC, datetime, timedelta, timezone
import json

import pytest

from tests.test_stores_azure_table import _new_store


@pytest.mark.parametrize(
    "expires_at",
    [
        datetime(2026, 1, 1, 12),
        datetime(2026, 1, 1, 12, tzinfo=UTC),
        datetime(2026, 1, 1, 14, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_table_round_trip_restores_internal_lease(expires_at: datetime) -> None:
    # Given: a stored lease, including SDK-returned naive or offset timestamps.
    store, table = _new_store()
    created = store.create()
    locked = store.try_acquire_run_lock(created.thread_id)
    assert locked is not None
    table.entities[("thread", created.thread_id)]["lock_expires_at"] = expires_at

    # When: the Table adapter reconstructs the persisted thread.
    restored = store.get(created.thread_id)

    # Then: ownership survives internally, but neither public dump exposes the lease.
    assert restored is not None
    assert restored.lock_token == locked.lock_token
    assert restored.lock_expires_at == datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert restored.lock_expires_at.tzinfo is UTC
    for payload in (restored.model_dump(), json.loads(restored.model_dump_json())):
        assert "lock_token" not in payload
        assert "lock_expires_at" not in payload


def test_table_round_trip_without_lease_restores_none() -> None:
    # Given: an ordinary thread which has never acquired a lease.
    store, _ = _new_store()
    created = store.create()

    # When: the thread is loaded from storage.
    restored = store.get(created.thread_id)

    # Then: absent internal fields retain their optional defaults.
    assert restored is not None
    assert restored.lock_token is None
    assert restored.lock_expires_at is None
