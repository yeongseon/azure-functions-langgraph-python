from unittest.mock import DEFAULT, patch

import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from tests.conftest import FakeCompiledGraph
from tests.test_platform_routes import _get_fn, _post_request
from tests.test_stores_azure_table import (
    FakeResourceModifiedError,
    FakeResourceNotFoundError,
    _new_store,
)


@pytest.mark.parametrize("conflicts", [0, 1, 2])
def test_release_retries_when_renewal_changes_etag(conflicts: int) -> None:
    # Given a renewal/metadata write landing immediately before release CAS.
    store, table = _new_store()
    thread = store.create(metadata={"original": True})
    locked = store.try_acquire_run_lock(thread.thread_id, assistant_id="agent")
    key = ("thread", thread.thread_id)
    update = table.update_entity
    remaining = conflicts

    def before_update(*args: object, **kwargs: object) -> object:
        nonlocal remaining
        with patch.object(table, "update_entity", update):
            if remaining:
                remaining -= 1
                assert store.renew_run_lock(thread.thread_id, locked.lock_token)
                store.update(thread.thread_id, metadata={"concurrent": True})
        return DEFAULT

    with patch.object(table, "update_entity", side_effect=before_update, wraps=update):
        # When the original owner releases with final values.
        released = store.release_run_lock(
            thread.thread_id, status="idle", values={"done": True}, owner_token=locked.lock_token
        )

    # Then the release preserves concurrent metadata and removes stored lease properties.
    assert released.status == "idle"
    assert released.values == {"done": True}
    assert released.metadata == ({"concurrent": True} if conflicts else {"original": True})
    assert "lock_token" not in table.entities[key]
    assert "lock_expires_at" not in table.entities[key]
    assert not store.renew_run_lock(thread.thread_id, locked.lock_token)


def test_release_rechecks_owner_when_new_run_wins_cas(caplog: pytest.LogCaptureFixture) -> None:
    # Given a stale release whose snapshot predates a newer owner's acquisition.
    store, table = _new_store()
    thread = store.create()
    first = store.try_acquire_run_lock(thread.thread_id)
    update = table.update_entity

    def reacquire(*args: object, **kwargs: object) -> object:
        with patch.object(table, "update_entity", update):
            store.update(thread.thread_id, status="idle")
            store.try_acquire_run_lock(thread.thread_id)
        return DEFAULT

    with patch.object(table, "update_entity", side_effect=reacquire, wraps=update):
        # When the old owner attempts release.
        released = store.release_run_lock(
            thread.thread_id, status="error", values={"stale": True}, owner_token=first.lock_token
        )

    # Then neither the new lock nor its values are overwritten.
    stored = table.entities[("thread", thread.thread_id)]
    assert released.status == "busy"
    assert stored["lock_token"] != first.lock_token
    assert "values_json" not in stored
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_release_raises_when_conflicts_exhausted() -> None:
    # Given an unchanged owner but persistent ETag conflicts.
    store, table = _new_store()
    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id)
    table.always_modified_error = True
    attempts = table.update_attempts

    # When / Then exhaustion is distinguishable from a successful or superseded release.
    with pytest.raises(RuntimeError, match="release") as error:
        store.release_run_lock(thread.thread_id, status="idle", owner_token=locked.lock_token)
    assert isinstance(error.value.__cause__, FakeResourceModifiedError)
    assert table.update_attempts - attempts == 3
    assert table.entities[("thread", thread.thread_id)]["status"] == "busy"


@pytest.mark.parametrize("phase", ["initial_read", "retry_read", "write"])
def test_release_missing_thread_raises_keyerror(phase: str) -> None:
    # Given a missing thread, or deletion at a release I/O boundary.
    store, table = _new_store()
    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id)
    if phase == "initial_read":
        store.delete(thread.thread_id)

    def deleted(*args: object, **kwargs: object) -> None:
        if phase == "retry_read":
            store.delete(thread.thread_id)
            raise FakeResourceModifiedError()
        raise FakeResourceNotFoundError()

    with patch.object(table, "update_entity", side_effect=deleted):
        # When / Then all missing-thread paths retain the ThreadStore contract.
        with pytest.raises(KeyError):
            store.release_run_lock(thread.thread_id, status="idle", owner_token=locked.lock_token)


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_platform_reports_release_exhaustion(endpoint: str) -> None:
    # Given a real Table store whose release writes alone always conflict.
    store, table = _new_store()
    lock = InProcessThreadLock()
    app = LangGraphApp(platform_compat=True, thread_lock=lock)
    app._thread_store = store
    app.register(graph=FakeCompiledGraph(), name="agent")
    thread = store.create()
    handler = _get_fn(app.function_app, f"aflg_platform_runs_{endpoint}")
    request = _post_request(
        f"/api/threads/{thread.thread_id}/runs/{endpoint}",
        {"assistant_id": "agent", "input": {}},
        thread_id=thread.thread_id,
    )
    update = table.update_entity

    def conflict_on_release(*args: object, **kwargs: object) -> object:
        if table.entities[("thread", thread.thread_id)]["status"] == "busy":
            raise FakeResourceModifiedError("private-storage-canary")
        return DEFAULT

    with patch.object(table, "update_entity", side_effect=conflict_on_release, wraps=update):
        # When graph execution completes but persisted release fails.
        response = handler(request)

    # Then callers do not receive a false success, and local exclusion is released.
    assert response.status_code == 500
    assert b"private-storage-canary" not in response.get_body()
    token = lock.acquire("agent", thread.thread_id)
    assert token is not None
    lock.release("agent", thread.thread_id, token)
