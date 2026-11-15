from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
import importlib
import itertools
import time
from typing import Any, Protocol, cast
from unittest.mock import DEFAULT, patch
import uuid

import pytest

from azure_functions_langgraph.platform.contracts import ThreadStatus
from azure_functions_langgraph.stores._errors import RunLockRenewalConflictError
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore
from tests.storage_service import fail_or_skip_service, require_tcp_service
from tests.table_status_index_support import CHECKS, Env, FaultTable
from tests.test_platform_routes import _get_fn, _patch_request
from tests.test_platform_run_release_ownership import (
    Harness,
    check_a_graph_failure_stays_the_primary_error_when_the_release_is_also_stale,
    check_a_stale_release_is_reported_as_failure_not_completion,
    check_an_ordinary_run_still_completes_and_publishes_its_result,
    check_another_owner_finishing_first_does_not_hide_a_stale_release,
    check_the_new_owner_can_still_finish_after_the_stale_release_was_refused,
)
from tests.test_platform_state_release_ownership import (
    StateHarness,
    check_a_stale_release_fails_the_state_update,
    check_a_state_update_graph_failure_stays_the_primary_error_when_the_release_is_also_stale,
    check_an_ordinary_state_update_still_completes,
    check_another_owner_finishing_first_does_not_hide_a_stale_state_update_release,
    check_the_new_owner_can_still_finish_after_a_stale_state_update_release_was_refused,
)
from tests.test_platform_state_stale_busy_read import (
    check_a_genuinely_busy_thread_still_answers_409_and_keeps_its_owner,
    check_a_stale_busy_read_is_refused_before_anything_changes,
    check_the_thread_can_be_updated_once_the_stale_read_is_gone,
)
from tests.test_platform_state_stale_terminal_read import (
    STALE_PAIRS,
    check_a_stale_terminal_read_does_not_replace_the_status_the_run_left,
)
from tests.test_table_renew_retry import _App, _race_the_renewal, _wait_for_one_renewal
from tests.test_thread_metadata_merge import App as MetadataApp

AZURITE_TABLE_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1"
)

pytestmark = pytest.mark.integration


class _TableServiceClientProtocol(Protocol):
    @classmethod
    def from_connection_string(cls, conn_str: str) -> _TableServiceClientProtocol: ...

    def list_tables(self) -> Iterable[object]: ...

    def create_table_if_not_exists(self, table_name: str) -> _TableClientProtocol: ...

    def delete_table(self, table_name: str) -> object: ...


class _TableClientProtocol(Protocol):
    table_name: str

    def update_entity(self, entity: dict[str, Any], mode: str, **kwargs: Any) -> object: ...

    def query_entities(self, *args: Any, **kwargs: Any) -> object: ...


@pytest.fixture
def azurite_table_client() -> Iterator[_TableClientProtocol]:
    require_tcp_service("127.0.0.1", 10002, "Azurite Table")
    try:
        tables_module = importlib.import_module("azure.data.tables")
        TableServiceClient = cast(
            type[_TableServiceClientProtocol],
            getattr(tables_module, "TableServiceClient"),
        )
        service_client = TableServiceClient.from_connection_string(
            conn_str=AZURITE_TABLE_CONNECTION_STRING
        )
        _ = list(service_client.list_tables())
    except Exception as exc:
        fail_or_skip_service(f"Azurite Table Storage is incompatible or unavailable: {exc}")

    table_name = f"aflgint{uuid.uuid4().hex[:18]}"
    table_client = service_client.create_table_if_not_exists(table_name=table_name)
    try:
        yield table_client
    finally:
        try:
            service_client.delete_table(table_name=table_name)
        except Exception:
            pass


def _new_store(table_client: _TableClientProtocol) -> AzureTableThreadStore:
    return AzureTableThreadStore.from_table_client(cast(Any, table_client))


def test_large_json_create_update_release_and_shrink(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # Given actual Azure SDK I/O against Azurite with multi-property Unicode JSON.
    store = _new_store(azurite_table_client)
    payload = {"text": "한😀" * 12000}
    thread = store.create(metadata=payload)
    locked = store.try_acquire_run_lock(thread.thread_id)
    assert locked is not None

    # When final values and subsequent shrinking updates replace the persisted documents.
    store.release_run_lock(
        thread.thread_id, status="idle", values=payload, owner_token=locked.lock_token
    )
    persisted = store.get(thread.thread_id)
    assert persisted is not None
    assert persisted.values == payload
    assert persisted.metadata == payload
    store.update(thread.thread_id, values={}, metadata={})

    # Then replacement really removes superseded chunks on the service, not just in a fake.
    rows = list(
        cast(
            Iterable[dict[str, Any]],
            azurite_table_client.query_entities(f"RowKey eq '{thread.thread_id}'"),
        )
    )
    assert len(rows) == 1
    assert not any(name.startswith("values_json_") for name in rows[0])
    shrunk = store.get(thread.thread_id)
    assert shrunk is not None
    assert shrunk.values == {}


def _set_updated_at(
    table_client: _TableClientProtocol,
    thread_id: str,
    value: datetime,
) -> None:
    table_client.update_entity(
        {
            "PartitionKey": "thread",
            "RowKey": thread_id,
            "updated_at": value,
        },
        mode="merge",
    )


def test_reset_stale_locks_resets_stale_and_skips_fresh(
    azurite_table_client: _TableClientProtocol,
) -> None:
    store = _new_store(azurite_table_client)

    stale = store.create(metadata={"kind": "stale"})
    fresh = store.create(metadata={"kind": "fresh"})
    _ = store.try_acquire_run_lock(stale.thread_id)
    _ = store.try_acquire_run_lock(fresh.thread_id)

    now = datetime.now(UTC)
    _set_updated_at(azurite_table_client, stale.thread_id, now - timedelta(seconds=900))
    _set_updated_at(azurite_table_client, fresh.thread_id, now - timedelta(seconds=30))

    reset_count = store.reset_stale_locks(older_than_seconds=300)

    stale_after = store.get(stale.thread_id)
    fresh_after = store.get(fresh.thread_id)
    assert reset_count == 1
    assert stale_after is not None
    assert fresh_after is not None
    assert stale_after.status == "error"
    assert fresh_after.status == "busy"


def test_reset_stale_locks_uses_projection_rowkey_and_updated_at(
    azurite_table_client: _TableClientProtocol,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _new_store(azurite_table_client)
    thread = store.create()
    _ = store.try_acquire_run_lock(thread.thread_id)
    _set_updated_at(
        azurite_table_client,
        thread.thread_id,
        datetime.now(UTC) - timedelta(seconds=900),
    )

    seen_select: list[object] = []
    original_query_entities = azurite_table_client.query_entities

    def wrapped_query_entities(*args: object, **kwargs: object) -> object:
        seen_select.append(kwargs.get("select"))
        return original_query_entities(*args, **kwargs)

    monkeypatch.setattr(azurite_table_client, "query_entities", wrapped_query_entities)

    reset_count = store.reset_stale_locks(older_than_seconds=300)

    assert reset_count == 1
    assert seen_select == [["RowKey", "updated_at"]]


def test_reset_stale_locks_etag_cas_conflict_does_not_stomp(
    azurite_table_client: _TableClientProtocol,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _new_store(azurite_table_client)
    thread = store.create()
    _ = store.try_acquire_run_lock(thread.thread_id)
    _set_updated_at(
        azurite_table_client,
        thread.thread_id,
        datetime.now(UTC) - timedelta(seconds=900),
    )

    core_module = importlib.import_module("azure.core")
    match_conditions = getattr(core_module, "MatchConditions")
    original_update_entity = azurite_table_client.update_entity
    conflict_injected = {"done": False}

    def wrapped_update_entity(*args: object, **kwargs: object) -> object:
        entity = args[0] if args else kwargs.get("entity")
        if (
            isinstance(entity, dict)
            and entity.get("PartitionKey") == "thread"
            and entity.get("RowKey") == thread.thread_id
            and entity.get("status") in {"idle", "error"}
            and kwargs.get("match_condition") == match_conditions.IfNotModified
            and not conflict_injected["done"]
        ):
            conflict_injected["done"] = True
            azurite_table_client.update_entity(
                {
                    "PartitionKey": "thread",
                    "RowKey": thread.thread_id,
                    "updated_at": datetime.now(UTC),
                },
                mode="merge",
            )
        return cast(Any, original_update_entity)(*args, **kwargs)

    monkeypatch.setattr(azurite_table_client, "update_entity", wrapped_update_entity)

    reset_count = store.reset_stale_locks(older_than_seconds=300)

    locked_after = store.get(thread.thread_id)
    assert conflict_injected["done"] is True
    assert reset_count == 0
    assert locked_after is not None
    assert locked_after.status == "busy"


def test_reset_stale_locks_delete_race_is_skipped(
    azurite_table_client: _TableClientProtocol,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a thread is deleted between query and CAS update, skip gracefully."""
    store = _new_store(azurite_table_client)
    thread = store.create()
    _ = store.try_acquire_run_lock(thread.thread_id)
    _set_updated_at(
        azurite_table_client,
        thread.thread_id,
        datetime.now(UTC) - timedelta(seconds=900),
    )

    original_update_entity = azurite_table_client.update_entity
    delete_injected = {"done": False}

    def wrapped_update_entity(*args: object, **kwargs: object) -> object:
        entity = args[0] if args else kwargs.get("entity")
        if (
            isinstance(entity, dict)
            and entity.get("PartitionKey") == "thread"
            and entity.get("RowKey") == thread.thread_id
            and entity.get("status") in {"idle", "error"}
            and not delete_injected["done"]
        ):
            delete_injected["done"] = True
            # Delete the entity before the CAS update lands
            azurite_table_client.update_entity(
                {"PartitionKey": "thread", "RowKey": thread.thread_id},
                mode="replace",
            )
            # Now delete it
            tables_module = importlib.import_module("azure.data.tables")
            table_client = cast(
                Any,
                getattr(tables_module, "TableServiceClient")
                .from_connection_string(conn_str=AZURITE_TABLE_CONNECTION_STRING)
                .get_table_client(azurite_table_client.table_name),
            )
            table_client.delete_entity(partition_key="thread", row_key=thread.thread_id)
        return cast(Any, original_update_entity)(*args, **kwargs)

    monkeypatch.setattr(azurite_table_client, "update_entity", wrapped_update_entity)

    reset_count = store.reset_stale_locks(older_than_seconds=300)

    assert delete_injected["done"] is True
    assert reset_count == 0
    # Thread no longer exists
    assert store.get(thread.thread_id) is None


def test_reset_stale_locks_projection_returns_usable_etag(
    azurite_table_client: _TableClientProtocol,
) -> None:
    """Projected query used by reset_stale_locks must expose a usable ETag.

    The Azure Tables SDK may surface the ETag either via
    ``entity.metadata["etag"]`` or as a top-level ``entity["etag"]`` key
    depending on the SDK version. ``reset_stale_locks`` accepts either
    shape, so this test asserts that at least one of them is populated
    when querying with a projection (``select=...``) against Azurite.
    """
    store = _new_store(azurite_table_client)
    thread = store.create()
    acquired = store.try_acquire_run_lock(thread.thread_id)
    assert acquired is not None, "expected to acquire run lock on fresh thread"

    entities = list(
        cast(
            Iterable[object],
            azurite_table_client.query_entities(
                query_filter="PartitionKey eq 'thread' and status eq 'busy'",
                select=["RowKey", "updated_at"],
            ),
        )
    )
    assert entities, "projected query should return the busy thread"

    entity = entities[0]
    metadata = getattr(entity, "metadata", None)
    etag: object = None
    if isinstance(metadata, Mapping):
        etag = metadata.get("etag")
    if etag is None and isinstance(entity, Mapping):
        etag = entity.get("etag")
    assert etag, (
        "projected query result must expose a usable ETag via "
        "entity.metadata['etag'] or entity['etag']"
    )


def test_release_removes_lease_fields_after_renewal_conflict(
    azurite_table_client: _TableClientProtocol,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a real Azure Tables ETag invalidated by a same-owner renewal.
    store = _new_store(azurite_table_client)
    thread = store.create(metadata={"preserved": True})
    locked = store.try_acquire_run_lock(thread.thread_id, assistant_id="agent")
    assert locked is not None and locked.lock_token is not None
    token = locked.lock_token
    update = azurite_table_client.update_entity
    renewed = False

    def renew_before_release(*args: object, **kwargs: object) -> object:
        nonlocal renewed
        if not renewed:
            renewed = True
            with patch.object(azurite_table_client, "update_entity", update):
                assert store.renew_run_lock(thread.thread_id, token)
                store.update(thread.thread_id, metadata={"concurrent": True})
        return DEFAULT

    with patch.object(
        azurite_table_client, "update_entity", side_effect=renew_before_release, wraps=update
    ):
        # When release retries the server's 412 response.
        released = store.release_run_lock(
            thread.thread_id, status="idle", values={"done": True}, owner_token=token
        )

    # Then the persisted entity has no obsolete lease fields and keeps concurrent data.
    persisted = store.get(thread.thread_id)
    assert persisted == released
    assert released.metadata == {"concurrent": True}
    assert released.values == {"done": True}
    assert released.assistant_id == "agent"
    rows = list(cast(Any, azurite_table_client.query_entities)("PartitionKey eq 'thread'"))
    assert "lock_token" not in rows[0]
    assert "lock_expires_at" not in rows[0]
    assert not store.renew_run_lock(thread.thread_id, token)


def test_reset_stale_locks_revokes_the_old_owner_against_the_real_table(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # Given a stale busy thread that the maintenance sweep reclaims (#808).
    store = _new_store(azurite_table_client)
    thread = store.create(metadata={"kind": "kept"})
    locked = store.try_acquire_run_lock(thread.thread_id, assistant_id="agent")
    assert locked is not None and locked.lock_token is not None
    old_token = locked.lock_token
    _set_updated_at(
        azurite_table_client, thread.thread_id, datetime.now(UTC) - timedelta(seconds=900)
    )
    assert store.reset_stale_locks(older_than_seconds=300) == 1

    # When the old worker comes back and releases and renews with its old token.
    outcome = store.release_run_lock(
        thread.thread_id, status="idle", values={"stale": True}, owner_token=old_token
    )
    renewed = store.renew_run_lock(thread.thread_id, old_token)

    # Then the real Table row has no lock properties, the recovery disposition stands,
    # and nothing else about the thread changed.
    rows = list(cast(Any, azurite_table_client.query_entities)("PartitionKey eq 'thread'"))
    assert "lock_token" not in rows[0] and "lock_expires_at" not in rows[0]
    assert (outcome.status, outcome.values, outcome.lock_token) == ("error", None, None)
    assert renewed is False
    after = store.get(thread.thread_id)
    assert after is not None
    assert (after.status, after.metadata, after.assistant_id) == (
        "error",
        {"kind": "kept"},
        "agent",
    )


def test_renewal_survives_a_concurrent_metadata_write_against_the_real_table(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # Given a live owner whose renewal read is invalidated by a metadata-only write (#805): the
    # real service answers the conditional write with 412 although the owner never changed.
    store = _new_store(azurite_table_client)
    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id, assistant_id="agent")
    assert locked is not None and locked.lock_token is not None
    token = locked.lock_token
    update = azurite_table_client.update_entity
    raced = False

    def write_metadata_before_the_renewal(
        entity: dict[str, Any], *args: object, **kwargs: object
    ) -> object:
        nonlocal raced
        is_renewal = set(entity) == {"PartitionKey", "RowKey", "updated_at", "lock_expires_at"}
        if is_renewal and not raced:
            raced = True
            with patch.object(azurite_table_client, "update_entity", update):
                store.update(thread.thread_id, metadata={"concurrent": True})
        return DEFAULT

    with patch.object(
        azurite_table_client,
        "update_entity",
        side_effect=write_metadata_before_the_renewal,
        wraps=update,
    ):
        # When the renewal's conditional write loses the race and is retried.
        renewed = store.renew_run_lock(thread.thread_id, token)

    # Then the real row still belongs to the same owner, with the concurrent write intact.
    assert raced is True
    assert renewed is True
    after = store.get(thread.thread_id)
    assert after is not None
    assert (after.status, after.lock_token) == ("busy", token)
    assert after.metadata == {"concurrent": True}
    assert after.assistant_id == "agent"


def test_a_renewal_that_loses_the_row_to_a_new_owner_returns_false_against_the_real_table(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # Given an old owner whose renewal conflicts with a newer owner's acquisition.
    store = _new_store(azurite_table_client)
    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id)
    assert locked is not None and locked.lock_token is not None
    old_token = locked.lock_token
    update = azurite_table_client.update_entity
    new_token: list[str] = []
    new_owner_row: list[Any] = []
    lock_fields = ("lock_token", "lock_expires_at", "updated_at", "status")

    def new_owner_takes_over(entity: dict[str, Any], *args: object, **kwargs: object) -> object:
        is_renewal = set(entity) == {"PartitionKey", "RowKey", "updated_at", "lock_expires_at"}
        if is_renewal and not new_token:
            with patch.object(azurite_table_client, "update_entity", update):
                store.update(thread.thread_id, status="idle")
                owner = store.try_acquire_run_lock(thread.thread_id)
            assert owner is not None and owner.lock_token is not None
            new_token.append(owner.lock_token)
            row = cast(Any, azurite_table_client).get_entity("thread", thread.thread_id)
            new_owner_row.append({field: row[field] for field in lock_fields})
        return DEFAULT

    with patch.object(
        azurite_table_client, "update_entity", side_effect=new_owner_takes_over, wraps=update
    ):
        # When the old owner's renewal re-reads the row after the 412.
        renewed = store.renew_run_lock(thread.thread_id, old_token)

    # Then it reports lost ownership and the newer owner's lock is untouched: token, status and
    # both lock timestamps are exactly what the new owner's acquisition wrote.
    assert renewed is False
    after = store.get(thread.thread_id)
    assert after is not None
    assert (after.status, after.lock_token) == ("busy", new_token[0])
    row = cast(Any, azurite_table_client).get_entity("thread", thread.thread_id)
    assert {field: row[field] for field in lock_fields} == new_owner_row[0]


def _real_metadata_patches(app: _App) -> Callable[[], None]:
    """A concurrent writer: each call is a real ``PATCH /threads/{id}`` through the handler."""
    handler = _get_fn(app.app.function_app, "aflg_platform_threads_update")
    counter = itertools.count(1)

    def patch_metadata() -> None:
        request = _patch_request(
            f"/api/threads/{app.thread_id}",
            {"metadata": {"patched": next(counter)}},
            thread_id=app.thread_id,
        )
        assert handler(request).status_code == 200

    return patch_metadata


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_a_real_metadata_patch_during_a_run_does_not_fail_the_run(
    endpoint: str,
    azurite_table_client: _TableClientProtocol,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a Platform run over the real Table whose first renewal loses its conditional write
    # to a real PATCH /threads/{id}: the owner token never changed (#805).
    app = _App(endpoint, _new_store(azurite_table_client), azurite_table_client)
    outcomes = _wait_for_one_renewal(app, monkeypatch)
    fired = _race_the_renewal(
        azurite_table_client, monkeypatch, times=1, concurrent=_real_metadata_patches(app)
    )

    # When the run executes and completes.
    response = app.handler(app.request)

    # Then the real 412 was retried after a re-read and the run is an ordinary success.
    assert len(fired) == 1
    assert outcomes[0] is True
    assert response.status_code == 200
    assert app.observer.events == ["started", "completed"]
    after = app.store.get(app.thread_id)
    assert after is not None and (after.status, after.lock_token) == ("idle", None)
    assert after.metadata == {"patched": 1}
    token = app.lock.acquire("agent", app.thread_id)
    assert token is not None


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_a_run_fails_safely_when_real_writes_exhaust_the_renewal_retries(
    endpoint: str,
    azurite_table_client: _TableClientProtocol,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a run whose renewal loses three conditional writes in a row to real PATCH writes.
    app = _App(endpoint, _new_store(azurite_table_client), azurite_table_client)
    outcomes = _wait_for_one_renewal(app, monkeypatch)
    fired = _race_the_renewal(
        azurite_table_client, monkeypatch, times=3, concurrent=_real_metadata_patches(app)
    )

    # When the run executes.
    response = app.handler(app.request)

    # Then exhaustion is reported as such (the owner was never shown to be replaced), the caller
    # gets the sanitized failure, and the thread ends in error with its lock cleared.
    assert len(fired) == 3
    assert isinstance(outcomes[0], RunLockRenewalConflictError)
    assert response.status_code == 500
    assert app.observer.events == ["started", "failed"]
    assert isinstance(app.observer.failures[0], RunLockRenewalConflictError)
    after = app.store.get(app.thread_id)
    assert after is not None and (after.status, after.lock_token) == ("error", None)
    assert after.metadata == {"patched": 3}
    token = app.lock.acquire("agent", app.thread_id)
    assert token is not None


def _real_run_harness(table_client: _TableClientProtocol) -> Harness:
    """A Platform app over the real Table service (in-process lock, recording observer)."""
    return Harness("table", _new_store(table_client), table_client)


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("new_owner", [False, True], ids=["reclaimed", "reacquired"])
def test_a_stale_run_release_over_the_real_table_is_a_failure_not_a_completion(
    endpoint: str, new_owner: bool, azurite_table_client: _TableClientProtocol
) -> None:
    # Given a real Platform run that loses the thread to a real `reset_stale_locks` (and, in the
    # second case, to a real re-acquisition) after its last renewal tick: #804 over the service.
    # When it releases through the real SDK with its old owner token, then it is a sanitized
    # failure with one failed event, and the other party's real row is untouched.
    check_a_stale_release_is_reported_as_failure_not_completion(
        _real_run_harness(azurite_table_client), endpoint, new_owner
    )


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_the_new_owner_can_still_finish_over_the_real_table(
    endpoint: str, azurite_table_client: _TableClientProtocol
) -> None:
    check_the_new_owner_can_still_finish_after_the_stale_release_was_refused(
        _real_run_harness(azurite_table_client), endpoint
    )


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_an_ordinary_run_completes_over_the_real_table(
    endpoint: str, azurite_table_client: _TableClientProtocol
) -> None:
    # The run-completion check compares the thread the store returns with what the run asked for;
    # on the real service a healthy release must still look like a success (no false positive).
    check_an_ordinary_run_still_completes_and_publishes_its_result(
        _real_run_harness(azurite_table_client), endpoint
    )


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("new_owner", [False, True], ids=["reclaimed", "reacquired"])
def test_a_graph_failure_stays_primary_over_the_real_table(
    endpoint: str, new_owner: bool, azurite_table_client: _TableClientProtocol
) -> None:
    check_a_graph_failure_stays_the_primary_error_when_the_release_is_also_stale(
        _real_run_harness(azurite_table_client), endpoint, new_owner
    )


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_another_owner_finishing_first_does_not_hide_a_stale_release_over_the_real_table(
    endpoint: str, azurite_table_client: _TableClientProtocol
) -> None:
    # The stale run's release hits a real row that another owner already finished at "idle",
    # which is exactly the thread an applied release would return. Only the store's own answer
    # (the replace was never sent) tells the run it must not report success.
    check_another_owner_finishing_first_does_not_hide_a_stale_release(
        _real_run_harness(azurite_table_client), endpoint
    )


def test_release_outcome_is_exact_against_the_real_table(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # Given a real row that ran A (reclaimed), then B (finished), and then A's stale release.
    store = _new_store(azurite_table_client)
    thread = store.create()
    held_a = store.try_acquire_run_lock(thread.thread_id)
    assert held_a is not None and held_a.lock_token is not None
    token_a = held_a.lock_token
    _set_updated_at(
        azurite_table_client, thread.thread_id, datetime.now(UTC) - timedelta(seconds=900)
    )
    assert store.reset_stale_locks(older_than_seconds=300) == 1
    held_b = store.try_acquire_run_lock(thread.thread_id)
    assert held_b is not None and held_b.lock_token is not None
    applied_b = store.release_run_lock_outcome(
        thread.thread_id, status="idle", values={"b": 1}, owner_token=held_b.lock_token
    )

    # When A's stale release arrives.
    stale_a = store.release_run_lock_outcome(
        thread.thread_id, status="idle", values={"a": 1}, owner_token=token_a
    )

    # Then B's release was applied and A's was not, although the thread A gets back is the same
    # idle, token-free row an applied release would have returned; nothing was written for A.
    assert applied_b.applied is True
    assert stale_a.applied is False
    assert (stale_a.thread.status, stale_a.thread.lock_token) == ("idle", None)
    assert stale_a.thread.values == {"b": 1}
    assert store.get(thread.thread_id) == stale_a.thread


def test_merge_metadata_retries_a_real_conflict_against_the_fresh_row(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # Given a metadata merge whose conditional write loses the race to another writer's new key:
    # the real service answers 412 and the store must merge again into what is stored now (#806).
    store = _new_store(azurite_table_client)
    thread = store.create(metadata={"base": 0})
    update = azurite_table_client.update_entity
    raced: list[int] = []

    def other_writer_first(entity: dict[str, Any], *args: object, **kwargs: object) -> object:
        if not raced:
            raced.append(1)
            with patch.object(azurite_table_client, "update_entity", update):
                store.update(thread.thread_id, metadata={"base": 0, "other-writer": True})
        return DEFAULT

    with patch.object(
        azurite_table_client, "update_entity", side_effect=other_writer_first, wraps=update
    ):
        # When the merge is applied.
        merged = store.merge_metadata(thread.thread_id, {"mine": 1})

    # Then the other writer's key survives next to ours, in the stored row too.
    assert raced == [1]
    assert merged.metadata == {"base": 0, "other-writer": True, "mine": 1}
    assert store.get(thread.thread_id) == merged


def test_two_concurrent_patches_of_different_keys_both_survive_against_the_real_table(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # Given request A that read the thread through the real service and has not written yet.
    app = MetadataApp(_new_store(azurite_table_client))
    concurrent = app.after_the_handlers_read("get", lambda: app.patch({"b": 2}))

    # When request B patches another key and finishes, and then A writes.
    assert app.patch({"a": 1}).status_code == 200

    # Then B ran inside A's window and was acknowledged, and neither change was lost.
    assert [response.status_code for response in concurrent] == [200]
    assert app.metadata() == {"base": 0, "a": 1, "b": 2}


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_a_patch_during_a_runs_metadata_merge_is_not_lost_against_the_real_table(
    endpoint: str, azurite_table_client: _TableClientProtocol
) -> None:
    # Given a run that read the thread and is about to take its lock and merge its metadata.
    app = MetadataApp(_new_store(azurite_table_client))
    concurrent = app.after_the_handlers_read(
        "try_acquire_run_lock", lambda: app.patch({"patched": True})
    )

    # When a PATCH lands in that window and the run then writes its own metadata.
    assert app.run(endpoint, {"from-run": 1}).status_code == 200

    # Then the PATCH ran inside the window and was acknowledged, and both changes survive.
    assert [response.status_code for response in concurrent] == [200]
    assert app.metadata() == {"base": 0, "patched": True, "from-run": 1}


def _real_state_harness(
    table_client: _TableClientProtocol, prior_status: ThreadStatus = "idle"
) -> StateHarness:
    """A Platform app over the real Table service for ``POST /threads/{id}/state``."""
    return StateHarness("table", _new_store(table_client), table_client, prior_status)


@pytest.mark.parametrize(
    ("prior_status", "reset_to", "new_owner"),
    [
        pytest.param("idle", "error", False, id="reclaimed"),
        pytest.param("error", "error", False, id="reclaimed-to-the-status-it-asked-for"),
        pytest.param("idle", "error", True, id="reacquired"),
    ],
)
def test_a_stale_state_update_release_over_the_real_table_is_a_failure_not_a_success(
    prior_status: ThreadStatus,
    reset_to: str,
    new_owner: bool,
    azurite_table_client: _TableClientProtocol,
) -> None:
    # Given a real state update that loses the thread to a real `reset_stale_locks` (and, in the
    # last case, to a real re-acquisition) while update_state runs: #836 over the service.
    # When it releases through the real SDK with its old owner token, then it is the sanitized
    # release failure and the other party's real row is untouched.
    check_a_stale_release_fails_the_state_update(
        _real_state_harness(azurite_table_client, prior_status),
        new_owner,
        "idle" if reset_to == "idle" else "error",
    )


def test_a_finished_new_owner_does_not_hide_a_stale_state_update_release_over_the_real_table(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # The stale update's release hits a real row that another owner already finished at "idle",
    # which is exactly the thread an applied release would return. Only the store's own answer
    # (the replace was never sent) tells the handler it must not report success.
    check_another_owner_finishing_first_does_not_hide_a_stale_state_update_release(
        _real_state_harness(azurite_table_client)
    )


def test_the_new_owner_can_still_finish_after_a_stale_state_update_release_over_the_real_table(
    azurite_table_client: _TableClientProtocol,
) -> None:
    check_the_new_owner_can_still_finish_after_a_stale_state_update_release_was_refused(
        _real_state_harness(azurite_table_client)
    )


@pytest.mark.parametrize("prior_status", ["idle", "interrupted"])
def test_an_ordinary_state_update_completes_over_the_real_table(
    prior_status: ThreadStatus, azurite_table_client: _TableClientProtocol
) -> None:
    # The check compares the thread the store returns with the status the update asked for; on
    # the real service a healthy release must still look like a success (no false positive).
    check_an_ordinary_state_update_still_completes(
        _real_state_harness(azurite_table_client, prior_status)
    )


@pytest.mark.parametrize("new_owner", [False, True], ids=["reclaimed", "reacquired"])
def test_a_state_update_graph_failure_stays_primary_over_the_real_table(
    new_owner: bool, azurite_table_client: _TableClientProtocol
) -> None:
    check_a_state_update_graph_failure_stays_the_primary_error_when_the_release_is_also_stale(
        _real_state_harness(azurite_table_client), new_owner
    )


@pytest.mark.parametrize("prior_status", ["idle", "interrupted", "error"])
def test_a_stale_busy_read_over_the_real_table_is_refused_before_anything_changes(
    prior_status: ThreadStatus, azurite_table_client: _TableClientProtocol
) -> None:
    # A real row that a finished run left terminal while the handler's reads saw it busy (#845):
    # the update is refused with the existing 409 before any lock is taken, and the real row keeps
    # the terminal status and carries no lock token.
    check_a_stale_busy_read_is_refused_before_anything_changes(
        _real_state_harness(azurite_table_client, prior_status)
    )


@pytest.mark.parametrize(("reported", "left"), STALE_PAIRS)
def test_a_stale_terminal_read_over_the_real_table_keeps_the_status_the_run_left(
    reported: ThreadStatus, left: ThreadStatus, azurite_table_client: _TableClientProtocol
) -> None:
    # A real row that a finished run left terminal while the handler's first read saw another
    # terminal status (#849): the update is applied and the real row keeps what the run left.
    check_a_stale_terminal_read_does_not_replace_the_status_the_run_left(
        _real_state_harness(azurite_table_client, left), reported
    )


@pytest.mark.parametrize("prior_status", ["idle", "interrupted", "error"])
def test_the_thread_can_be_updated_once_the_stale_read_is_gone_over_the_real_table(
    prior_status: ThreadStatus, azurite_table_client: _TableClientProtocol
) -> None:
    check_the_thread_can_be_updated_once_the_stale_read_is_gone(
        _real_state_harness(azurite_table_client, prior_status)
    )


def test_a_genuinely_busy_thread_still_answers_409_over_the_real_table(
    azurite_table_client: _TableClientProtocol,
) -> None:
    check_a_genuinely_busy_thread_still_answers_409_and_keeps_its_owner(
        _real_state_harness(azurite_table_client)
    )


# -- opt-in status index (#801): the shared scenarios and the unverified design assumptions ------


def _index_env(table_client: _TableClientProtocol) -> Env:
    """The scenarios' environment over the real table: the SDK client behind a fault wrapper."""
    core = importlib.import_module("azure.core.exceptions")
    wrapped = FaultTable(table_client)

    def new_store(client: Any = wrapped, **options: Any) -> AzureTableThreadStore:
        return AzureTableThreadStore.from_table_client(cast(Any, client), **options)

    return Env(
        inner=table_client,
        table=wrapped,
        store=new_store(status_index=True, status_index_state_ttl_seconds=0),
        plain=new_store(table_client),
        new_store=new_store,
        wait=time.sleep,
        modified_error=core.ResourceModifiedError,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
def test_status_index_scenario_against_azurite(
    azurite_table_client: _TableClientProtocol, check: Callable[[Env], None]
) -> None:
    check(_index_env(azurite_table_client))


def _raw(table_client: _TableClientProtocol) -> Any:
    return table_client


def test_u1_an_identical_upsert_changes_the_etag_and_timestamp_on_azurite(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # U1 (design proposal): does insert-or-replace of identical content change the ETag? The index
    # does not depend on the answer (every hint upsert carries a fresh ``u``), but it is recorded.
    table = _raw(azurite_table_client)
    key = {"PartitionKey": "idx:idle", "RowKey": "same"}
    table.upsert_entity(dict(key), mode="replace")
    first = table.get_entity("idx:idle", "same")
    time.sleep(0.02)
    table.upsert_entity(dict(key), mode="replace")
    second = table.get_entity("idx:idle", "same")
    assert first.metadata["etag"] != second.metadata["etag"]
    assert first.metadata["timestamp"] < second.metadata["timestamp"]
    table.upsert_entity({**key, "u": "1"}, mode="replace")
    before = table.get_entity("idx:idle", "same").metadata["etag"]
    table.upsert_entity({**key, "u": "1"}, mode="replace")
    assert table.get_entity("idx:idle", "same").metadata["etag"] != before


def test_u2_the_verification_filter_with_fourteen_row_keys_runs_on_azurite(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # U2: whether the 15-comparison limit counts the PartitionKey clause is NOT decidable here;
    # Azurite does not enforce the limit (see the PR), so 14 stays the conservative batch size.
    table = _raw(azurite_table_client)
    for position in range(20):
        table.upsert_entity({"PartitionKey": "thread", "RowKey": f"k{position:02d}"})
    clauses = " or ".join(f"RowKey eq 'k{position:02d}'" for position in range(14))
    rows = list(table.query_entities(f"PartitionKey eq 'thread' and ({clauses})"))
    assert sorted(row["RowKey"] for row in rows) == [f"k{position:02d}" for position in range(14)]


def test_u3_azurite_orders_filters_and_transacts_the_way_the_index_needs(
    azurite_table_client: _TableClientProtocol,
) -> None:
    table = _raw(azurite_table_client)
    keys = [f"{position:019d}~t{position}" for position in (5, 3, 9, 1, 7)]
    assert table.submit_transaction(
        [
            ("upsert", {"PartitionKey": "idx:error", "RowKey": key}, {"mode": "replace"})
            for key in keys
        ]
    )
    rows = [
        r["RowKey"] for r in table.query_entities("PartitionKey eq 'idx:error'", select=["RowKey"])
    ]
    assert rows == sorted(keys)  # ascending RowKey order
    after = [
        r["RowKey"]
        for r in table.query_entities(
            f"PartitionKey eq 'idx:error' and RowKey gt '{sorted(keys)[1]}'", select=["RowKey"]
        )
    ]
    assert after == sorted(keys)[2:]
    paged = table.query_entities(
        "PartitionKey eq 'idx:error'", results_per_page=2, select=["RowKey"]
    )
    assert [len(page) for page in (list(p) for p in paged.by_page())] == [2, 2, 1]
    stale = table.get_entity("idx:error", sorted(keys)[0])
    table.upsert_entity({"PartitionKey": "idx:error", "RowKey": sorted(keys)[0]}, mode="replace")
    core = importlib.import_module("azure.core")
    errors = importlib.import_module("azure.core.exceptions")
    with pytest.raises(errors.ResourceModifiedError):
        table.delete_entity(
            "idx:error",
            sorted(keys)[0],
            etag=stale.metadata["etag"],
            match_condition=core.MatchConditions.IfNotModified,
        )
    with pytest.raises(Exception):  # an all-or-nothing batch fails as a whole on a missing row
        table.submit_transaction(
            [
                ("upsert", {"PartitionKey": "idx:error", "RowKey": "x"}, {"mode": "replace"}),
                ("delete", {"PartitionKey": "idx:error", "RowKey": "missing"}, {}),
            ]
        )
    assert "x" not in [r["RowKey"] for r in table.query_entities("PartitionKey eq 'idx:error'")]


def test_u5_a_write_is_visible_to_the_next_read_on_azurite(
    azurite_table_client: _TableClientProtocol,
) -> None:
    # U5 on the emulator only: read-your-writes for point reads and queries after an upsert.
    store = _new_store(azurite_table_client)
    thread = store.create()
    assert store.get(thread.thread_id) is not None
    assert [t.thread_id for t in store.search(status="idle", limit=100)] == [thread.thread_id]
