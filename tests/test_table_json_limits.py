from datetime import UTC, datetime
import json

from langchain_core.messages import HumanMessage
from pydantic import BaseModel
import pytest

from azure_functions_langgraph.platform.contracts import Interrupt
from azure_functions_langgraph.stores._table_json import (
    TableStorageLimitError,
    read_json,
    validate_entity,
    write_json,
)
from tests.test_stores_azure_table import FakeResourceModifiedError, _new_store


@pytest.mark.parametrize("text", ["x" * 32760, "x" * 32768, "한😀" * 12000])
@pytest.mark.parametrize("field", ["metadata", "values", "interrupts"])
def test_json_properties_round_trip_within_utf16_limit(text: str, field: str) -> None:
    # Given JSON at and beyond a single Azure string property's capacity.
    store, table = _new_store()
    thread = store.create()
    payload = {"node": [Interrupt(id="i", value=text)]} if field == "interrupts" else {"text": text}

    # When any supported JSON field is updated.
    updated = store.update(thread.thread_id, **{field: payload})

    # Then its data survives and every physical string property fits Azure's limit.
    assert getattr(updated, field) == payload
    entity = table.entities[("thread", thread.thread_id)]
    assert all(len(v.encode("utf-16-le")) <= 65536 for v in entity.values() if isinstance(v, str))
    assert getattr(store.get(thread.thread_id), field) == payload


def test_create_and_release_chunked_json_then_shrink_removes_obsolete_properties() -> None:
    # Given large metadata and final values spanning multiple properties.
    store, table = _new_store()
    metadata = {"text": "x" * 70000}
    thread = store.create(metadata=metadata)
    locked = store.try_acquire_run_lock(thread.thread_id)

    # When the run persists large final values.
    released = store.release_run_lock(
        thread.thread_id, status="idle", values=metadata, owner_token=locked.lock_token
    )

    # Then both fields round-trip without oversized properties.
    assert released.metadata == metadata
    assert released.values == metadata
    large_keys = set(table.entities[("thread", thread.thread_id)])
    store.update(thread.thread_id, metadata={}, values={}, interrupts={})
    small = table.entities[("thread", thread.thread_id)]
    assert len(small) < len(large_keys)
    assert all(value is not None for value in small.values())
    assert store.get(thread.thread_id).values == {}


@pytest.mark.parametrize("operation", ["create", "update", "release"])
def test_entity_overflow_is_rejected_before_write(operation: str) -> None:
    # Given data exceeding the entity-wide limit even after property splitting.
    store, table = _new_store()
    thread = store.create()
    payload = {"text": "x" * (1024 * 1024)}
    before = dict(table.entities)
    writes = table.update_attempts

    # When / Then persistence fails locally with actionable guidance, not an Azure request.
    with pytest.raises(ValueError, match="(?i)(limit|size).*(Blob|reduce|smaller)"):
        if operation == "create":
            store.create(metadata=payload)
        elif operation == "update":
            store.update(thread.thread_id, values=payload)
        else:
            store.release_run_lock(thread.thread_id, status="idle", values=payload)
    assert table.entities == before
    assert table.update_attempts == writes


def test_update_validates_combined_entity_not_just_patch() -> None:
    # Given two individually valid documents whose combined UTF-16 size exceeds 1 MiB.
    store, table = _new_store()
    thread = store.create(metadata={"text": "x" * 280000})
    writes = table.update_attempts

    # When / Then the second field cannot push the existing entity over the limit.
    with pytest.raises(ValueError, match="(?i)(limit|size)"):
        store.update(thread.thread_id, values={"text": "y" * 280000})
    assert table.update_attempts == writes


def test_legacy_entity_is_read_and_migrated_without_losing_other_fields() -> None:
    # Given a pre-versioning entity written by an older package.
    store, table = _new_store()
    thread = store.create()
    entity = table.entities[("thread", thread.thread_id)]
    entity.update(metadata_json='{"legacy":true}', values_json='{"old":1}')
    assert store.get(thread.thread_id).metadata == {"legacy": True}

    # When a legacy document grows beyond the property limit.
    updated = store.update(thread.thread_id, values={"text": "x" * 70000})

    # Then legacy and versioned data are both readable.
    assert updated.metadata == {"legacy": True}
    assert updated.values == {"text": "x" * 70000}
    assert store.search()[0].values == updated.values


def test_json_coercion_supports_datetime_messages_and_pydantic() -> None:
    class Detail(BaseModel):
        timestamp: datetime

    # Given application values accepted by the shared JSON boundary.
    store, _ = _new_store()
    now = datetime(2026, 10, 25, tzinfo=UTC)
    payload = {"at": now, "message": HumanMessage(content="hello"), "detail": Detail(timestamp=now)}

    # When those values are persisted.
    thread = store.create(metadata=payload)

    # Then they become predictable plain JSON, never opaque model repr strings.
    assert thread.metadata["at"] == str(now)
    assert thread.metadata["message"]["type"] == "human"
    assert thread.metadata["detail"] == {"timestamp": "2026-10-25T00:00:00Z"}
    json.dumps(thread.model_dump(mode="json"))


@pytest.mark.parametrize("length", [32766, 32767])
def test_exact_utf16_json_boundary(length: int) -> None:
    # Given a JSON string including its two quote characters.
    entity: dict[str, str] = {}

    # When the payload fits exactly or exceeds 64 KiB by two bytes.
    write_json(entity, "values_json", "x" * length)

    # Then only the overflow representation uses versioned chunks.
    assert ("values_json_format" in entity) == (length == 32767)
    assert read_json(entity, "values_json") == "x" * length


@pytest.mark.parametrize("count", [0, 253, "2"])
def test_invalid_chunk_counts_fail_closed(count: int | str) -> None:
    # Given a malformed stored representation.
    entity = {"values_json_format": "utf16-v1", "values_json_count": count}

    # When / Then reads reject it rather than returning partial data.
    with pytest.raises(ValueError, match="count"):
        read_json(entity, "values_json")


def test_unknown_format_fails_closed() -> None:
    with pytest.raises(ValueError, match="format"):
        read_json({"values_json_format": "future-v2"}, "values_json")


@pytest.mark.parametrize("value", ["x" * 32769, b"x" * 65537])
def test_oversized_non_json_property_rejected(value: str | bytes) -> None:
    with pytest.raises(TableStorageLimitError) as error:
        validate_entity({"custom": value})
    assert error.value.limit == 65536


def test_property_count_limit_includes_service_timestamp() -> None:
    with pytest.raises(TableStorageLimitError, match="count"):
        validate_entity({f"key_{i}": 1 for i in range(255)})


def test_capacity_reserves_space_below_one_mib() -> None:
    # Given a near-limit entity whose property data alone fits below 1 MiB.
    entity = {f"chunk_{i}": "x" * 32768 for i in range(15)}
    validate_entity({**entity, "binary": b"valid", "ignored": None})

    # When / Then an additional full chunk exceeds the conservative total budget.
    with pytest.raises(TableStorageLimitError, match="entity"):
        validate_entity({**entity, "chunk_15": "x" * 32768})


def test_invalid_property_type_rejected() -> None:
    with pytest.raises(TypeError, match="property type"):
        validate_entity({"invalid": []})


def test_update_retries_cas_and_preserves_concurrent_metadata() -> None:
    store, table = _new_store()
    thread = store.create(metadata={"original": True})
    table.modified_errors_remaining = 1
    assert store.update(thread.thread_id, values={"done": True}).metadata == {"original": True}
    table.always_modified_error = True
    with pytest.raises(FakeResourceModifiedError):
        store.update(thread.thread_id, values={})
