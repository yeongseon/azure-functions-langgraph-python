from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import sys
import types
from typing import Any, cast

pytest = cast(Any, importlib.import_module("pytest"))
contracts_module = importlib.import_module("azure_functions_langgraph.platform.contracts")
Interrupt = getattr(contracts_module, "Interrupt")
platform_stores_module = importlib.import_module("azure_functions_langgraph.platform.stores")
ThreadStore = getattr(platform_stores_module, "ThreadStore")
azure_table_module = importlib.import_module("azure_functions_langgraph.stores.azure_table")
AzureTableThreadStore = getattr(azure_table_module, "AzureTableThreadStore")


class FakeResourceNotFoundError(Exception):
    pass


class MockTableClient:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        self.last_query_filter: str | None = None
        self.last_update_mode: str | None = None

    def create_entity(self, entity: dict[str, Any]) -> None:
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key in self.entities:
            raise ValueError(f"Entity already exists: {key}")
        self.entities[key] = deepcopy(entity)

    def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]:
        key = (partition_key, row_key)
        entity = self.entities.get(key)
        if entity is None:
            raise FakeResourceNotFoundError(row_key)
        return deepcopy(entity)

    def update_entity(self, entity: dict[str, Any], mode: str) -> None:
        self.last_update_mode = mode
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key not in self.entities:
            raise FakeResourceNotFoundError(key[1])
        if mode == "merge":
            merged = deepcopy(self.entities[key])
            merged.update(deepcopy(entity))
            self.entities[key] = merged
            return
        self.entities[key] = deepcopy(entity)

    def delete_entity(self, partition_key: str, row_key: str) -> None:
        key = (partition_key, row_key)
        if key not in self.entities:
            raise FakeResourceNotFoundError(row_key)
        del self.entities[key]

    def query_entities(
        self,
        query_filter: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del kwargs
        self.last_query_filter = query_filter
        entities = [deepcopy(entity) for entity in self.entities.values()]
        if query_filter is None:
            return entities

        # Parse PartitionKey + optional status filter
        parts = [p.strip() for p in query_filter.split(" and ")]
        filtered = entities
        for part in parts:
            if part.startswith("PartitionKey eq '") and part.endswith("'"):
                pk = part[len("PartitionKey eq '") : -1].replace("''", "'")
                filtered = [e for e in filtered if e.get("PartitionKey") == pk]
            elif part.startswith("status eq '") and part.endswith("'"):
                status = part[len("status eq '") : -1].replace("''", "'")
                filtered = [e for e in filtered if e.get("status") == status]
            else:
                raise ValueError(f"Unsupported query filter part: {part}")
        return filtered


def _new_store() -> tuple[Any, MockTableClient]:
    table_client = MockTableClient()
    store = AzureTableThreadStore(
        table_client=table_client,
        not_found_error=FakeResourceNotFoundError,
    )
    return store, table_client


def test_protocol_conformance() -> None:
    store, _ = _new_store()
    assert isinstance(store, ThreadStore)


def test_create_defaults_and_schema() -> None:
    store, table_client = _new_store()

    thread = store.create()

    assert thread.status == "idle"
    assert thread.metadata is None
    assert thread.values is None
    assert thread.assistant_id is None
    assert thread.interrupts == {}
    assert thread.created_at == thread.updated_at
    assert thread.created_at.tzinfo is not None

    entity = table_client.entities[("thread", thread.thread_id)]
    assert entity["PartitionKey"] == "thread"
    assert entity["RowKey"] == thread.thread_id
    assert "metadata_json" not in entity
    assert "values_json" not in entity
    assert "assistant_id" not in entity
    assert entity["interrupts_json"] == "{}"


def test_create_preserves_none_vs_empty_metadata() -> None:
    store, table_client = _new_store()

    none_metadata = store.create(metadata=None)
    empty_metadata = store.create(metadata={})

    none_entity = table_client.entities[("thread", none_metadata.thread_id)]
    empty_entity = table_client.entities[("thread", empty_metadata.thread_id)]

    assert none_metadata.metadata is None
    assert "metadata_json" not in none_entity

    assert empty_metadata.metadata == {}
    assert empty_entity["metadata_json"] == "{}"


def test_get_existing_and_missing() -> None:
    store, _ = _new_store()

    created = store.create(metadata={"env": "dev"})
    fetched = store.get(created.thread_id)

    assert fetched is not None
    assert fetched.thread_id == created.thread_id
    assert fetched.metadata == {"env": "dev"}
    assert store.get("missing") is None


def test_update_nonexistent_raises_keyerror() -> None:
    store, _ = _new_store()

    with pytest.raises(KeyError):
        store.update("missing", status="busy")


def test_update_partial_fields_and_merge_mode() -> None:
    store, table_client = _new_store()

    created = store.create(metadata={"env": "prod", "tier": "free"})
    updated = store.update(created.thread_id, status="busy")

    assert updated.status == "busy"
    assert updated.metadata == {"env": "prod", "tier": "free"}
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at
    assert table_client.last_update_mode == "merge"


def test_update_all_fields() -> None:
    store, table_client = _new_store()

    created = store.create()
    interrupts = {"node_a": [Interrupt(id="i-1", value={"kind": "pause"})]}
    updated = store.update(
        created.thread_id,
        metadata={},
        status="interrupted",
        values={"messages": [{"role": "user", "content": "hello"}]},
        interrupts=interrupts,
        assistant_id="assistant-1",
    )

    assert updated.metadata == {}
    assert updated.status == "interrupted"
    assert updated.values == {"messages": [{"role": "user", "content": "hello"}]}
    assert "node_a" in updated.interrupts
    assert updated.interrupts["node_a"][0].id == "i-1"
    assert updated.assistant_id == "assistant-1"

    entity = table_client.entities[("thread", created.thread_id)]
    assert entity["metadata_json"] == "{}"
    assert entity["assistant_id"] == "assistant-1"


def test_update_supports_all_status_values() -> None:
    store, _ = _new_store()

    for status in ("idle", "busy", "interrupted", "error"):
        thread = store.create()
        updated = store.update(thread.thread_id, status=status)
        assert updated.status == status


def test_delete_existing_and_missing() -> None:
    store, _ = _new_store()

    created = store.create()
    store.delete(created.thread_id)
    assert store.get(created.thread_id) is None

    with pytest.raises(KeyError):
        store.delete(created.thread_id)


def test_search_status_metadata_combined_limit_offset_ordering(monkeypatch: Any) -> None:
    store, table_client = _new_store()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = iter(base + timedelta(seconds=i) for i in range(16))
    monkeypatch.setattr(store, "_now", lambda: next(timestamps))

    t1 = store.create(metadata={"env": "prod", "tier": "free"})
    t2 = store.create(metadata={"env": "prod", "tier": "premium"})
    t3 = store.create(metadata={"env": "dev"})
    t4 = store.create(metadata={"env": "prod", "tier": "premium"})

    store.update(t1.thread_id, status="busy")
    store.update(t2.thread_id, status="busy")
    store.update(t4.thread_id, status="error")

    busy = store.search(status="busy", limit=10)
    assert [thread.thread_id for thread in busy] == [t2.thread_id, t1.thread_id]
    assert table_client.last_query_filter == "PartitionKey eq 'thread' and status eq 'busy'"

    prod = store.search(metadata={"env": "prod"}, limit=10)
    assert [thread.thread_id for thread in prod] == [t4.thread_id, t2.thread_id, t1.thread_id]

    prod_premium_busy = store.search(
        metadata={"env": "prod", "tier": "premium"},
        status="busy",
        limit=10,
    )
    assert [thread.thread_id for thread in prod_premium_busy] == [t2.thread_id]

    page = store.search(limit=2, offset=1)
    assert [thread.thread_id for thread in page] == [t3.thread_id, t2.thread_id]
    assert store.search(limit=10, offset=100) == []


def test_search_negative_limit_or_offset_raises() -> None:
    store, _ = _new_store()

    with pytest.raises(ValueError, match="limit must be non-negative"):
        store.search(limit=-1)

    with pytest.raises(ValueError, match="offset must be non-negative"):
        store.search(offset=-1)


def test_count_all_status_metadata_combined_and_consistent_with_search() -> None:
    store, _ = _new_store()

    t1 = store.create(metadata={"env": "prod", "tier": "free"})
    t2 = store.create(metadata={"env": "prod", "tier": "premium"})
    t3 = store.create(metadata={"env": "dev"})

    store.update(t1.thread_id, status="busy")
    store.update(t2.thread_id, status="busy")
    store.update(t3.thread_id, status="error")

    assert store.count() == 3
    assert store.count(status="busy") == 2
    assert store.count(metadata={"env": "prod"}) == 2
    assert store.count(metadata={"env": "prod", "tier": "premium"}, status="busy") == 1

    count = store.count(metadata={"env": "prod"}, status="busy")
    search = store.search(metadata={"env": "prod"}, status="busy", limit=100)
    assert count == len(search)


def test_returned_threads_are_deep_copy_isolated() -> None:
    store, _ = _new_store()

    created = store.create(metadata={"k": "v"})
    updated = store.update(
        created.thread_id,
        values={"messages": [{"role": "user", "content": "hello"}]},
        interrupts={"node": [Interrupt(id="i-1", value="pause")]},
    )

    assert updated.metadata is not None
    updated.metadata["k"] = "mutated"
    assert updated.values is not None
    updated.values["messages"].append({"role": "assistant", "content": "bye"})
    updated.interrupts["node"].append(Interrupt(id="i-2", value="extra"))

    fetched = store.get(created.thread_id)
    assert fetched is not None
    assert fetched.metadata == {"k": "v"}
    assert fetched.values == {"messages": [{"role": "user", "content": "hello"}]}
    assert len(fetched.interrupts["node"]) == 1

    search_result = store.search(limit=1)[0]
    search_result.interrupts["node"].append(Interrupt(id="i-3", value="x"))

    refetched = store.get(created.thread_id)
    assert refetched is not None
    assert len(refetched.interrupts["node"]) == 1


def test_from_connection_string_missing_dependency_raises_helpful_error(monkeypatch: Any) -> None:
    module = importlib.import_module("azure_functions_langgraph.stores.azure_table")
    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "azure.data.tables":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match="azure-data-tables"):
        AzureTableThreadStore.from_connection_string(
            connection_string="UseDevelopmentStorage=true",
            table_name="threads",
        )


def test_init_without_not_found_error_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        AzureTableThreadStore(table_client=MockTableClient())

def test_from_connection_string_success_sets_not_found_error(monkeypatch: Any) -> None:
    table_client = MockTableClient()

    class FakeTableClient:
        called_with: tuple[str, str] | None = None

        @classmethod
        def from_connection_string(cls, conn_str: str, table_name: str) -> MockTableClient:
            cls.called_with = (conn_str, table_name)
            return table_client

    azure_data_tables = types.ModuleType("azure.data.tables")
    setattr(azure_data_tables, "TableClient", FakeTableClient)

    azure_core_exceptions = types.ModuleType("azure.core.exceptions")
    setattr(azure_core_exceptions, "ResourceNotFoundError", FakeResourceNotFoundError)

    monkeypatch.setitem(sys.modules, "azure.data.tables", azure_data_tables)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions)

    store = AzureTableThreadStore.from_connection_string(
        connection_string="UseDevelopmentStorage=true",
        table_name="threads",
    )

    assert FakeTableClient.called_with == ("UseDevelopmentStorage=true", "threads")
    assert store._not_found_error is FakeResourceNotFoundError
    assert store.get("missing") is None


def test_from_connection_string_missing_symbols_raise_helpful_errors(monkeypatch: Any) -> None:
    missing_table_client_module = types.ModuleType("azure.data.tables")
    azure_core_exceptions = types.ModuleType("azure.core.exceptions")
    setattr(azure_core_exceptions, "ResourceNotFoundError", FakeResourceNotFoundError)

    monkeypatch.setitem(sys.modules, "azure.data.tables", missing_table_client_module)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions)

    with pytest.raises(ImportError, match="TableClient"):
        AzureTableThreadStore.from_connection_string(
            connection_string="UseDevelopmentStorage=true",
            table_name="threads",
        )

    class FakeTableClientNoop:
        @classmethod
        def from_connection_string(
            cls,
            conn_str: str,
            table_name: str,
        ) -> MockTableClient:
            del conn_str, table_name
            return MockTableClient()

    azure_data_tables = types.ModuleType("azure.data.tables")
    setattr(azure_data_tables, "TableClient", FakeTableClientNoop)
    missing_not_found_module = types.ModuleType("azure.core.exceptions")

    monkeypatch.setitem(sys.modules, "azure.data.tables", azure_data_tables)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", missing_not_found_module)

    with pytest.raises(ImportError, match="ResourceNotFoundError"):
        AzureTableThreadStore.from_connection_string(
            connection_string="UseDevelopmentStorage=true",
            table_name="threads",
        )


def test_internal_helper_branches(monkeypatch: Any, caplog: Any) -> None:
    store, _ = _new_store()

    naive = datetime(2026, 1, 1)
    assert AzureTableThreadStore._normalize_datetime(naive).tzinfo is not None

    with pytest.raises(TypeError):
        AzureTableThreadStore._json_default(object())

    caplog.set_level("WARNING")
    large_entity = {"payload": "x" * (1024 * 1024)}
    store._warn_entity_size(large_entity, "thread-1")
    assert "close to" in caplog.text

    store._not_found_error = None
    with pytest.raises(RuntimeError, match="not_found_error is not configured"):
        store._not_found_exception()
    store._not_found_error = FakeResourceNotFoundError

    with_values_and_assistant = store._thread_to_entity(
        "thread-2",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata={"k": "v"},
        status="idle",
        values={"a": 1},
        assistant_id="assistant-1",
        interrupts={},
    )
    assert with_values_and_assistant["values_json"] == "{\"a\": 1}"
    assert with_values_and_assistant["assistant_id"] == "assistant-1"

    entity_without_interrupts = {
        "PartitionKey": "thread",
        "RowKey": "thread-3",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "status": "idle",
    }
    thread = store._entity_to_thread(entity_without_interrupts)
    assert thread.interrupts == {}

    no_metadata = store.create()
    assert store.search(metadata={"x": "y"}, limit=10, offset=0) == []
    store.delete(no_metadata.thread_id)


def test_search_excludes_non_thread_partition_rows() -> None:
    """Regression: search/count must only return rows with PartitionKey='thread'."""
    store, table_client = _new_store()

    # Create a thread via the store (PartitionKey='thread')
    thread = store.create(metadata={"env": "prod"})

    # Manually inject a foreign-partition entity into the table
    foreign_entity = {
        "PartitionKey": "other_partition",
        "RowKey": "foreign-1",
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
        "status": "idle",
        "interrupts_json": "{}",
    }
    table_client.entities[("other_partition", "foreign-1")] = deepcopy(foreign_entity)

    # search/count should only see our thread, not the foreign entity
    results = store.search(limit=100)
    assert len(results) == 1
    assert results[0].thread_id == thread.thread_id
    assert store.count() == 1


def test_update_entity_race_raises_keyerror() -> None:
    """Regression: if entity is deleted between read and update_entity, raise KeyError."""
    store, table_client = _new_store()
    thread = store.create()

    # Monkey-patch update_entity to simulate race: entity disappears after get
    original_update = table_client.update_entity

    def racing_update(entity: dict[str, Any], mode: str) -> None:
        # Delete the entity before the real update
        pk = str(entity["PartitionKey"])
        rk = str(entity["RowKey"])
        key = (pk, rk)
        if key in table_client.entities:
            del table_client.entities[key]
        original_update(entity, mode)

    table_client.update_entity = racing_update  # type: ignore[method-assign]

    with pytest.raises(KeyError):
        store.update(thread.thread_id, status="busy")


def test_from_connection_string_does_not_leak_class_state(monkeypatch: Any) -> None:
    """Regression: from_connection_string() must not change class-level _not_found_error."""
    table_client = MockTableClient()

    class FakeTableClient:
        @classmethod
        def from_connection_string(cls, conn_str: str, table_name: str) -> MockTableClient:
            del conn_str, table_name
            return table_client

    azure_data_tables = types.ModuleType("azure.data.tables")
    setattr(azure_data_tables, "TableClient", FakeTableClient)

    azure_core_exceptions = types.ModuleType("azure.core.exceptions")
    setattr(azure_core_exceptions, "ResourceNotFoundError", FakeResourceNotFoundError)

    monkeypatch.setitem(sys.modules, "azure.data.tables", azure_data_tables)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions)

    # Create store via from_connection_string
    store = AzureTableThreadStore.from_connection_string(
        connection_string="UseDevelopmentStorage=true",
        table_name="threads",
    )

    # Instance should work
    assert store.get("missing") is None

    # Constructing a new instance without not_found_error should still fail
    with pytest.raises(TypeError):
        AzureTableThreadStore(table_client=MockTableClient())
