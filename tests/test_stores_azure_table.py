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


class FakeResourceModifiedError(Exception):
    pass


class FakeMatchConditions:
    IfNotModified = "if-not-modified"


class MockEntity(dict[str, Any]):
    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        etag = data.get("etag")
        self.metadata = {} if etag is None else {"etag": etag}


class MockTableClient:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        self.last_query_filter: str | None = None
        self.last_update_mode: str | None = None
        self.last_update_kwargs: dict[str, Any] = {}
        self.update_attempts = 0
        self.modified_errors_remaining = 0
        self.always_modified_error = False
        self.raise_not_found_on_update = False
        self._etag_counter = 0

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return f'W/"{self._etag_counter}"'

    def create_entity(self, entity: dict[str, Any]) -> None:
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key in self.entities:
            raise ValueError(f"Entity already exists: {key}")
        stored = deepcopy(entity)
        stored["etag"] = self._next_etag()
        self.entities[key] = stored

    def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]:
        key = (partition_key, row_key)
        entity = self.entities.get(key)
        if entity is None:
            raise FakeResourceNotFoundError(row_key)
        return MockEntity(deepcopy(entity))

    def update_entity(
        self,
        entity: dict[str, Any],
        mode: str,
        *,
        etag: str | None = None,
        match_condition: Any = None,
    ) -> None:
        self.last_update_mode = mode
        self.last_update_kwargs = {
            "etag": etag,
            "match_condition": match_condition,
        }
        self.update_attempts += 1
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if self.raise_not_found_on_update:
            raise FakeResourceNotFoundError(key[1])
        if key not in self.entities:
            raise FakeResourceNotFoundError(key[1])
        if self.always_modified_error:
            raise FakeResourceModifiedError(key[1])
        if self.modified_errors_remaining > 0:
            self.modified_errors_remaining -= 1
            raise FakeResourceModifiedError(key[1])
        stored = self.entities[key]
        if match_condition == FakeMatchConditions.IfNotModified and etag != stored.get("etag"):
            raise FakeResourceModifiedError(key[1])
        if mode == "merge":
            merged = deepcopy(stored)
            merged.update(deepcopy(entity))
            merged["etag"] = self._next_etag()
            self.entities[key] = merged
            return
        replaced = deepcopy(entity)
        replaced["etag"] = self._next_etag()
        self.entities[key] = replaced

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
        modified_error=FakeResourceModifiedError,
        match_conditions=FakeMatchConditions,
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


def test_try_acquire_success_consumes_etag() -> None:
    store, _ = _new_store()

    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id)

    assert locked is not None
    assert locked.status == "busy"
    assert store.try_acquire_run_lock(thread.thread_id) is None


def test_try_acquire_retries_on_modified_error() -> None:
    store, table_client = _new_store()

    thread = store.create()
    table_client.modified_errors_remaining = 1

    locked = store.try_acquire_run_lock(thread.thread_id)

    assert locked is not None
    assert locked.status == "busy"
    assert table_client.update_attempts == 2


def test_try_acquire_returns_none_after_retries_exhausted() -> None:
    store, table_client = _new_store()

    thread = store.create()
    table_client.always_modified_error = True

    locked = store.try_acquire_run_lock(thread.thread_id)

    assert locked is None
    assert table_client.update_attempts == 3


def test_try_acquire_raises_key_error_when_get_entity_404() -> None:
    store, _ = _new_store()

    with pytest.raises(KeyError):
        store.try_acquire_run_lock("missing")


def test_try_acquire_raises_key_error_when_update_404() -> None:
    store, table_client = _new_store()

    thread = store.create()
    table_client.raise_not_found_on_update = True

    with pytest.raises(KeyError):
        store.try_acquire_run_lock(thread.thread_id)


def test_try_acquire_assistant_mismatch_raises() -> None:
    store, _ = _new_store()

    thread = store.create()
    store.update(thread.thread_id, assistant_id="a")

    with pytest.raises(ValueError, match="cannot run with 'b'"):
        store.try_acquire_run_lock(thread.thread_id, assistant_id="b")


def test_release_lock_no_etag() -> None:
    store, table_client = _new_store()

    thread = store.create()
    store.release_run_lock(thread.thread_id, status="idle")

    assert table_client.last_update_mode == "merge"
    assert table_client.last_update_kwargs["etag"] is None
    assert table_client.last_update_kwargs["match_condition"] is None


def test_release_lock_with_values_serializes_to_values_json() -> None:
    store, table_client = _new_store()

    thread = store.create()
    released = store.release_run_lock(
        thread.thread_id,
        status="error",
        values={"messages": [{"role": "assistant", "content": "done"}]},
    )

    assert released.status == "error"
    entity = table_client.entities[("thread", thread.thread_id)]
    assert entity["values_json"] == ('{"messages": [{"role": "assistant", "content": "done"}]}')


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
    setattr(azure_core_exceptions, "ResourceModifiedError", FakeResourceModifiedError)

    azure_core = types.ModuleType("azure.core")
    setattr(azure_core, "MatchConditions", FakeMatchConditions)

    monkeypatch.setitem(sys.modules, "azure.data.tables", azure_data_tables)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core)

    store = AzureTableThreadStore.from_connection_string(
        connection_string="UseDevelopmentStorage=true",
        table_name="threads",
    )

    assert FakeTableClient.called_with == ("UseDevelopmentStorage=true", "threads")
    assert store._not_found_error is FakeResourceNotFoundError
    assert store._modified_error is FakeResourceModifiedError
    assert store._match_conditions is FakeMatchConditions
    assert store.get("missing") is None


def test_from_connection_string_missing_symbols_raise_helpful_errors(monkeypatch: Any) -> None:
    missing_table_client_module = types.ModuleType("azure.data.tables")
    azure_core_exceptions = types.ModuleType("azure.core.exceptions")
    setattr(azure_core_exceptions, "ResourceNotFoundError", FakeResourceNotFoundError)
    setattr(azure_core_exceptions, "ResourceModifiedError", FakeResourceModifiedError)
    azure_core = types.ModuleType("azure.core")
    setattr(azure_core, "MatchConditions", FakeMatchConditions)

    monkeypatch.setitem(sys.modules, "azure.data.tables", missing_table_client_module)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core)

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
    monkeypatch.setitem(sys.modules, "azure.core", azure_core)

    with pytest.raises(ImportError, match="ResourceNotFoundError"):
        AzureTableThreadStore.from_connection_string(
            connection_string="UseDevelopmentStorage=true",
            table_name="threads",
        )

    missing_modified_module = types.ModuleType("azure.core.exceptions")
    setattr(missing_modified_module, "ResourceNotFoundError", FakeResourceNotFoundError)

    monkeypatch.setitem(sys.modules, "azure.core.exceptions", missing_modified_module)

    with pytest.raises(ImportError, match="ResourceModifiedError"):
        AzureTableThreadStore.from_connection_string(
            connection_string="UseDevelopmentStorage=true",
            table_name="threads",
        )

    azure_core_exceptions_with_both = types.ModuleType("azure.core.exceptions")
    setattr(azure_core_exceptions_with_both, "ResourceNotFoundError", FakeResourceNotFoundError)
    setattr(azure_core_exceptions_with_both, "ResourceModifiedError", FakeResourceModifiedError)
    missing_match_conditions_module = types.ModuleType("azure.core")

    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions_with_both)
    monkeypatch.setitem(sys.modules, "azure.core", missing_match_conditions_module)

    with pytest.raises(ImportError, match="MatchConditions"):
        AzureTableThreadStore.from_connection_string(
            connection_string="UseDevelopmentStorage=true",
            table_name="threads",
        )


def _install_fake_azure_sdk(monkeypatch: Any) -> None:
    azure_data_tables = types.ModuleType("azure.data.tables")
    setattr(azure_data_tables, "TableClient", object)

    azure_core_exceptions = types.ModuleType("azure.core.exceptions")
    setattr(azure_core_exceptions, "ResourceNotFoundError", FakeResourceNotFoundError)
    setattr(azure_core_exceptions, "ResourceModifiedError", FakeResourceModifiedError)

    azure_core = types.ModuleType("azure.core")
    setattr(azure_core, "MatchConditions", FakeMatchConditions)

    monkeypatch.setitem(sys.modules, "azure.data.tables", azure_data_tables)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core)


def test_from_table_client_wires_sdk_symbols(monkeypatch: Any) -> None:
    _install_fake_azure_sdk(monkeypatch)
    table_client = MockTableClient()

    store = AzureTableThreadStore.from_table_client(table_client)

    assert store._table_client is table_client
    assert store._not_found_error is FakeResourceNotFoundError
    assert store._modified_error is FakeResourceModifiedError
    assert store._match_conditions is FakeMatchConditions


def test_from_table_client_supports_full_lifecycle(monkeypatch: Any) -> None:
    _install_fake_azure_sdk(monkeypatch)
    table_client = MockTableClient()

    store = AzureTableThreadStore.from_table_client(table_client)

    thread = store.create(metadata={"k": "v"})
    fetched = store.get(thread.thread_id)
    assert fetched is not None
    assert fetched.metadata == {"k": "v"}

    acquired = store.try_acquire_run_lock(thread.thread_id)
    assert acquired is not None
    store.release_run_lock(thread.thread_id, status="idle")

    listed = store.search(limit=10, offset=0)
    assert any(t.thread_id == thread.thread_id for t in listed)

    store.delete(thread.thread_id)
    assert store.get(thread.thread_id) is None


def test_from_table_client_supports_update(monkeypatch: Any) -> None:
    _install_fake_azure_sdk(monkeypatch)
    table_client = MockTableClient()

    store = AzureTableThreadStore.from_table_client(table_client)

    thread = store.create(metadata={"k": "v1"})
    updated = store.update(thread.thread_id, metadata={"k": "v2"})
    assert updated is not None
    assert updated.metadata == {"k": "v2"}


def test_from_table_client_retries_on_cas_modified_error(monkeypatch: Any) -> None:
    _install_fake_azure_sdk(monkeypatch)
    table_client = MockTableClient()

    store = AzureTableThreadStore.from_table_client(table_client)

    thread = store.create()
    table_client.modified_errors_remaining = 1

    locked = store.try_acquire_run_lock(thread.thread_id)

    assert locked is not None
    assert locked.status == "busy"
    assert table_client.update_attempts == 2


def test_from_table_client_returns_none_after_cas_retries_exhausted(
    monkeypatch: Any,
) -> None:
    _install_fake_azure_sdk(monkeypatch)
    table_client = MockTableClient()

    store = AzureTableThreadStore.from_table_client(table_client)

    thread = store.create()
    table_client.always_modified_error = True

    locked = store.try_acquire_run_lock(thread.thread_id)

    assert locked is None


def test_from_table_client_missing_dependency_raises_helpful_error(monkeypatch: Any) -> None:
    module = importlib.import_module("azure_functions_langgraph.stores.azure_table")
    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "azure.core.exceptions":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match="azure-core"):
        AzureTableThreadStore.from_table_client(MockTableClient())


def test_from_table_client_does_not_import_table_client(monkeypatch: Any) -> None:
    """Regression: from_table_client must not import azure.data.tables.

    Application code building its own TableClient (e.g. with
    DefaultAzureCredential) should be able to use this factory even if
    azure.data.tables is not importable through azure_table_module's
    importlib (the caller already supplied a working TableClient).
    """
    _install_fake_azure_sdk(monkeypatch)
    module = importlib.import_module("azure_functions_langgraph.stores.azure_table")
    real_import_module = importlib.import_module
    forbidden = {"azure.data.tables"}
    seen: list[str] = []

    def fake_import_module(name: str) -> Any:
        seen.append(name)
        if name in forbidden:
            raise AssertionError(
                f"from_table_client must not import {name}; caller already supplied client"
            )
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    store = AzureTableThreadStore.from_table_client(MockTableClient())
    assert store is not None
    assert "azure.data.tables" not in seen


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
    assert with_values_and_assistant["values_json"] == '{"a": 1}'
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

    def racing_update(
        entity: dict[str, Any],
        mode: str,
        *,
        etag: str | None = None,
        match_condition: Any = None,
    ) -> None:
        # Delete the entity before the real update
        pk = str(entity["PartitionKey"])
        rk = str(entity["RowKey"])
        key = (pk, rk)
        if key in table_client.entities:
            del table_client.entities[key]
        original_update(entity, mode, etag=etag, match_condition=match_condition)

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
    setattr(azure_core_exceptions, "ResourceModifiedError", FakeResourceModifiedError)

    azure_core = types.ModuleType("azure.core")
    setattr(azure_core, "MatchConditions", FakeMatchConditions)

    monkeypatch.setitem(sys.modules, "azure.data.tables", azure_data_tables)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core)

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


# ── reset_stale_locks tests ──────────────────────────────────────────────


def test_reset_stale_locks_resets_stale_skips_recent(monkeypatch: Any) -> None:
    """Stale busy thread older than threshold is reset; recent busy thread is not."""
    store, table_client = _new_store()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = iter(
        [
            base,  # create t1
            base + timedelta(seconds=100),  # create t2
            base + timedelta(seconds=10),  # acquire t1: sets updated_at=10
            base + timedelta(seconds=500),  # acquire t2: sets updated_at=500
            base + timedelta(seconds=700),  # reset_stale_locks: cutoff = 700 - 300 = 400
            base + timedelta(seconds=700),  # reset_stale_locks: patch updated_at for t1
        ]
    )
    monkeypatch.setattr(store, "_now", lambda: next(timestamps))

    t1 = store.create()
    t2 = store.create()
    store.try_acquire_run_lock(t1.thread_id)
    store.try_acquire_run_lock(t2.thread_id)

    # cutoff = 700 - 300 = 400.  t1 updated_at=10 < 400 → stale.  t2 updated_at=500 >= 400 → recent.
    count = store.reset_stale_locks(older_than_seconds=300)

    assert count == 1
    assert store.get(t1.thread_id).status == "error"
    assert store.get(t2.thread_id).status == "busy"


def test_reset_stale_locks_etag_mismatch_skips(monkeypatch: Any) -> None:
    """Concurrent re-acquire during reset: ETag mismatch → skipped, not stomped."""
    store, table_client = _new_store()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = iter(
        [
            base,  # create
            base + timedelta(seconds=10),  # acquire lock
            base + timedelta(seconds=700),  # reset_stale_locks cutoff calc
            base + timedelta(seconds=700),  # reset_stale_locks: patch updated_at (before CAS fails)
        ]
    )
    monkeypatch.setattr(store, "_now", lambda: next(timestamps))

    t1 = store.create()
    store.try_acquire_run_lock(t1.thread_id)

    # Force all CAS updates to fail with ETag mismatch
    table_client.always_modified_error = True

    count = store.reset_stale_locks(older_than_seconds=300)

    assert count == 0
    # Thread status unchanged (still busy)
    table_client.always_modified_error = False  # allow get_entity to work
    assert store.get(t1.thread_id).status == "busy"


def test_reset_stale_locks_empty_store_returns_zero() -> None:
    """No threads at all → returns 0, no errors."""
    store, _ = _new_store()

    count = store.reset_stale_locks(older_than_seconds=600)

    assert count == 0


def test_reset_stale_locks_idle_threads_not_touched(monkeypatch: Any) -> None:
    """Idle threads are not affected by reset_stale_locks."""
    store, _ = _new_store()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = iter(
        [
            base,  # create
            base + timedelta(seconds=700),  # reset cutoff
        ]
    )
    monkeypatch.setattr(store, "_now", lambda: next(timestamps))

    t1 = store.create()  # idle status, old timestamp

    count = store.reset_stale_locks(older_than_seconds=300)

    assert count == 0
    assert store.get(t1.thread_id).status == "idle"


def test_reset_stale_locks_negative_older_than_raises() -> None:
    """Negative older_than_seconds → ValueError."""
    store, _ = _new_store()

    with pytest.raises(ValueError, match="non-negative"):
        store.reset_stale_locks(older_than_seconds=-1)


def test_reset_stale_locks_invalid_status_raises() -> None:
    """Invalid status param → ValueError."""
    store, _ = _new_store()

    with pytest.raises(ValueError, match="must be 'idle' or 'error'"):
        store.reset_stale_locks(older_than_seconds=600, status="busy")

    with pytest.raises(ValueError, match="must be 'idle' or 'error'"):
        store.reset_stale_locks(older_than_seconds=600, status="interrupted")


def test_reset_stale_locks_status_idle(monkeypatch: Any) -> None:
    """reset_stale_locks with status='idle' sets thread to idle."""
    store, _ = _new_store()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = iter(
        [
            base,  # create
            base + timedelta(seconds=10),  # acquire
            base + timedelta(seconds=700),  # reset cutoff
            base + timedelta(seconds=700),  # patch updated_at
        ]
    )
    monkeypatch.setattr(store, "_now", lambda: next(timestamps))

    t1 = store.create()
    store.try_acquire_run_lock(t1.thread_id)

    count = store.reset_stale_locks(older_than_seconds=300, status="idle")

    assert count == 1
    assert store.get(t1.thread_id).status == "idle"


def test_reset_stale_locks_zero_threshold(monkeypatch: Any) -> None:
    """older_than_seconds=0 resets any busy thread (cutoff == now)."""
    store, _ = _new_store()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = iter(
        [
            base,  # create
            base + timedelta(seconds=10),  # acquire: sets updated_at=10
            base + timedelta(seconds=10),  # reset: cutoff=10-0=10, updated_at=10 >= 10
        ]
    )
    monkeypatch.setattr(store, "_now", lambda: next(timestamps))

    t1 = store.create()
    store.try_acquire_run_lock(t1.thread_id)

    # cutoff equals updated_at exactly → not stale (normalized >= cutoff)
    count = store.reset_stale_locks(older_than_seconds=0)

    assert count == 0


def test_reset_stale_locks_deleted_thread_skipped(monkeypatch: Any) -> None:
    """Thread deleted between query and update is skipped, not raised."""
    store, table_client = _new_store()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = iter(
        [
            base,  # create
            base + timedelta(seconds=10),  # acquire
            base + timedelta(seconds=700),  # reset cutoff
            base + timedelta(seconds=700),  # patch updated_at
        ]
    )
    monkeypatch.setattr(store, "_now", lambda: next(timestamps))

    t1 = store.create()
    store.try_acquire_run_lock(t1.thread_id)

    # Simulate deletion between query_entities and update_entity
    table_client.raise_not_found_on_update = True

    count = store.reset_stale_locks(older_than_seconds=300)

    assert count == 0
