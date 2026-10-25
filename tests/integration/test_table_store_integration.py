from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
import importlib
from typing import Any, Protocol, cast
import uuid

import pytest

from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore
from tests.storage_service import fail_or_skip_service, require_tcp_service

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
    from unittest.mock import DEFAULT, patch

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
