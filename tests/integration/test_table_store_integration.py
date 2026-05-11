from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
import importlib
from typing import Any, Protocol, cast
import uuid

import pytest

from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

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
        pytest.skip(f"Azurite Table Storage not available: {exc}")

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

    now = datetime.now(timezone.utc)
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
        datetime.now(timezone.utc) - timedelta(seconds=900),
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
        datetime.now(timezone.utc) - timedelta(seconds=900),
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
                    "updated_at": datetime.now(timezone.utc),
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
        datetime.now(timezone.utc) - timedelta(seconds=900),
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
            table_client.delete_entity(
                partition_key="thread", row_key=thread.thread_id
            )
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
