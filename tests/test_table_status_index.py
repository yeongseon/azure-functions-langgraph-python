"""Opt-in Table status index (#801): fake-backed scenarios, request accounting, edge cases.

The ``check_*`` scenarios in ``tests/table_status_index_support.py`` also run against Azurite in
the Table integration lane.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from azure_functions_langgraph.stores._table_status_index import StatusIndex
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore
from tests.table_status_index_support import (
    CHECKS,
    Env,
    FakeClock,
    FakeMatchConditions,
    FakeResourceModifiedError,
    FakeResourceNotFoundError,
    FakeTable,
    FakeTransactionError,
    FaultTable,
    SimulatedCrash,
    fake_env,
    ids,
    make_ready,
    seed,
)


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
def test_scenario_against_the_fake_table(check: Callable[[Env], None]) -> None:
    check(fake_env())


def _snapshot(fake: FakeTable) -> dict[tuple[str, str], dict[str, Any]]:
    return {key: dict(value) for key, value in fake.rows.items()}


def _count_requests(index: bool) -> dict[str, dict[str, int]]:
    env = fake_env()
    store = env.store if index else env.plain
    fake = env.fake
    assert fake is not None
    results: dict[str, dict[str, int]] = {}

    def measure(name: str, action: Callable[[], Any]) -> None:
        before = len(fake.requests)
        action()
        window = fake.requests[before:]
        results[name] = {
            "writes": sum(
                1
                for r in window
                if r
                in (
                    "create_entity",
                    "update_entity",
                    "upsert_entity",
                    "delete_entity",
                    "submit_transaction",
                )
            ),
            "gets": window.count("get_entity"),
        }

    measure("create", lambda: store.create())
    thread_id = store.create().thread_id
    lock_box: list[Any] = []
    measure("acquire (idle->busy)", lambda: lock_box.append(store.try_acquire_run_lock(thread_id)))
    measure(
        "release (busy->idle)",
        lambda: store.release_run_lock(
            thread_id, status="idle", owner_token=lock_box[0].lock_token
        ),
    )
    measure("update(status=error)", lambda: store.update(thread_id, status="error"))
    measure(
        "update(status=error) again (no change)", lambda: store.update(thread_id, status="error")
    )
    measure("update(metadata)", lambda: store.update(thread_id, metadata={"a": 1}))
    measure("merge_metadata", lambda: store.merge_metadata(thread_id, {"b": 2}))
    measure("delete", lambda: store.delete(thread_id))
    return results


def test_write_amplification_is_counted_per_operation() -> None:
    off = _count_requests(index=False)
    on = _count_requests(index=True)
    print("\nrequests per operation (writes/gets), index off -> on")  # recorded in the PR
    for name in off:
        before, after = off[name], on[name]
        print(f"  {name}: {before['writes']}/{before['gets']} -> {after['writes']}/{after['gets']}")
    assert off == {
        "create": {"writes": 1, "gets": 0},
        "acquire (idle->busy)": {"writes": 1, "gets": 1},
        "release (busy->idle)": {"writes": 1, "gets": 1},
        "update(status=error)": {"writes": 1, "gets": 1},
        "update(status=error) again (no change)": {"writes": 1, "gets": 1},
        "update(metadata)": {"writes": 1, "gets": 1},
        "merge_metadata": {"writes": 1, "gets": 1},
        "delete": {"writes": 1, "gets": 0},
    }
    # The index adds exactly one write where a terminal status is newly set, none elsewhere.
    assert on == {
        "create": {"writes": 2, "gets": 0},
        "acquire (idle->busy)": {"writes": 1, "gets": 1},
        "release (busy->idle)": {"writes": 2, "gets": 1},
        "update(status=error)": {"writes": 2, "gets": 1},
        "update(status=error) again (no change)": {"writes": 1, "gets": 1},
        "update(metadata)": {"writes": 1, "gets": 1},
        "merge_metadata": {"writes": 1, "gets": 1},
        "delete": {"writes": 1, "gets": 0},
    }


def test_the_default_store_sends_exactly_the_requests_it_did_before() -> None:
    env = fake_env()
    plain = env.plain
    thread = plain.create(metadata={"a": 1})
    lock = plain.try_acquire_run_lock(thread.thread_id)
    assert lock is not None
    plain.release_run_lock(thread.thread_id, status="error", owner_token=lock.lock_token)
    plain.search(status="error")
    plain.count()
    assert env.fake is not None
    assert env.fake.requests == [
        "create_entity",
        "get_entity",
        "update_entity",
        "get_entity",
        "update_entity",
        "query_page",
        "query_page",
    ]
    assert {pk for pk, _ in env.fake.rows} == {"thread"}


def test_read_amplification_index_vs_scan() -> None:
    env = fake_env()
    fake = env.fake
    assert fake is not None
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for position in range(3000):
        status = "idle" if position % 2 else "busy"
        seed(env, f"t{position:05d}", base + timedelta(seconds=position), status)
    make_ready(env)

    def measure(store: AzureTableThreadStore, **kwargs: Any) -> tuple[int, int, list[str]]:
        requests, read = len(fake.requests), fake.entities_read
        found = ids(store.search(**kwargs))
        return len(fake.requests) - requests, fake.entities_read - read, found

    scan = measure(env.plain, status="idle", limit=10)
    first = measure(env.store, status="idle", limit=10)
    deep = measure(env.store, status="idle", limit=10, offset=100)
    print(
        f"\nsearch(status=idle, limit=10) over 3000 threads (1500 idle): "
        f"scan {scan[:2]} requests/entities, index {first[:2]}, index offset=100 {deep[:2]}"
    )
    assert first[2] == scan[2]
    # The scan reads every idle thread (the status filter is server-side). The index path reads
    # the marker (uncached here: ttl 0), one hint page and one 14-thread verification query.
    assert (scan[0], scan[1]) == (2, 1500)
    assert (first[0], first[1]) == (3, 1 + 14 + 14)
    assert deep[2] == ids(env.plain.search(status="idle", limit=10, offset=100))
    # offset 100 verifies the 110 rows before the answer: marker + 1 hint page + 8 verifications.
    assert (deep[0], deep[1]) == (10, 1 + 112 + 112)


def test_stale_hints_inside_the_window_cost_more_reads_but_never_change_the_result() -> None:
    env = fake_env()
    for _ in range(30):
        env.store.create()
    # Move the 20 newest threads away: their idle hints stay (lazy delete) and are verified.
    newest = ids(env.plain.search(status="idle", limit=20))
    for thread_id in newest:
        env.store.update(thread_id, status="error")
    make_ready(env)
    assert env.fake is not None
    before = len(env.fake.requests)
    got = ids(env.store.search(status="idle", limit=5))
    assert got == ids(env.plain.search(status="idle", limit=5))
    assert len(env.fake.requests[before:]) > 2  # more index pages and verification batches


def test_limit_zero_and_empty_status_are_answered_without_reading_primary_rows() -> None:
    env = fake_env()
    make_ready(env)
    assert env.fake is not None
    env.reset_queries()
    assert env.store.search(status="idle", limit=0) == []
    assert env.store.search(status="error", limit=5) == []
    assert not any("PartitionKey eq 'thread'" in query for query in env.table.queries)


def test_negative_limit_and_offset_still_raise() -> None:
    env = fake_env()
    with pytest.raises(ValueError, match="limit"):
        env.store.search(status="idle", limit=-1)
    with pytest.raises(ValueError, match="offset"):
        env.store.search(status="idle", offset=-1)


def test_the_marker_is_cached_for_the_ttl_and_refreshed_after() -> None:
    clock = [0.0]
    fake = FakeTable()
    store = AzureTableThreadStore(
        table_client=fake,
        not_found_error=FakeResourceNotFoundError,
        modified_error=FakeResourceModifiedError,
        match_conditions=FakeMatchConditions,
        status_index=True,
        status_index_state_ttl_seconds=30,
    )
    index: StatusIndex = store._status_index  # type: ignore[assignment]
    index._monotonic = lambda: clock[0]
    store.create()
    make_ready_env = Env(
        fake,
        FaultTable(fake),
        store,
        store,
        lambda **_: store,
        lambda _: None,
        FakeResourceModifiedError,
        fake,
    )
    make_ready(make_ready_env)

    def reads() -> int:
        return fake.count("get_entity")

    store.search(status="idle")
    before = reads()
    store.search(status="idle")
    assert reads() == before  # within the TTL: no marker read
    clock[0] += 31
    store.search(status="idle")
    assert reads() == before + 1
    # another process disables the index: this one notices after the TTL, never before
    fake.rows[("idx-meta", "status-index")]["state"] = "disabled"
    assert ids(store.search(status="idle")) == ids(store.search(status="idle"))
    clock[0] += 31
    before = fake.count("query_page")
    store.search(status="idle")
    assert fake.count("query_page") == before + 1  # scan path only


def test_an_unknown_marker_value_is_not_ready() -> None:
    env = fake_env()
    env.store.create()
    env.inner.upsert_entity(
        {"PartitionKey": "idx-meta", "RowKey": "status-index", "state": "bogus"}, mode="replace"
    )
    assert env.store.status_index_state() == "absent"
    env.reset_queries()
    env.store.search(status="idle")
    assert not env.index_queried()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"status_index_offset_cap": -1}, "offset_cap"),
        ({"status_index_state_ttl_seconds": -1}, "ttl_seconds"),
    ],
)
def test_option_validation(options: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        AzureTableThreadStore(
            table_client=FakeTable(),
            not_found_error=FakeResourceNotFoundError,
            modified_error=FakeResourceModifiedError,
            match_conditions=FakeMatchConditions,
            status_index=True,
            **options,
        )


def test_a_table_client_without_the_index_methods_is_refused_only_when_opted_in() -> None:
    class Bare:
        def __getattr__(self, name: str) -> Any:
            raise AttributeError(name)

    kwargs: dict[str, Any] = {
        "table_client": Bare(),
        "not_found_error": FakeResourceNotFoundError,
        "modified_error": FakeResourceModifiedError,
        "match_conditions": FakeMatchConditions,
    }
    AzureTableThreadStore(**kwargs)  # off: nothing required
    with pytest.raises(ValueError, match="upsert_entity"):
        AzureTableThreadStore(**kwargs, status_index=True)


def test_from_table_client_and_from_connection_string_pass_the_options_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AzureTableThreadStore.from_table_client(
        FakeTable(),
        status_index=True,
        status_index_offset_cap=7,
    )
    assert store._status_index is not None and store._status_index._offset_cap == 7
    assert AzureTableThreadStore.from_table_client(FakeTable())._status_index is None

    captured: dict[str, Any] = {}

    class FakeTableClient:
        @classmethod
        def from_connection_string(cls, **kwargs: Any) -> FakeTable:
            captured.update(kwargs)
            return FakeTable()

    monkeypatch.setattr(
        AzureTableThreadStore,
        "_load_azure_sdk_symbols",
        staticmethod(
            lambda: (
                FakeTableClient,
                FakeResourceNotFoundError,
                FakeResourceModifiedError,
                FakeMatchConditions,
            )
        ),
    )
    built = AzureTableThreadStore.from_connection_string(
        "cs", "tbl", status_index=True, status_index_state_ttl_seconds=5
    )
    assert built._status_index is not None
    assert captured == {"conn_str": "cs", "table_name": "tbl"}
    assert AzureTableThreadStore.from_connection_string("cs", "tbl")._status_index is None


def test_repair_needs_a_service_timestamp_and_validates_grace() -> None:
    env = fake_env()
    with pytest.raises(ValueError, match="grace_seconds"):
        env.store.repair_status_index(-1)

    original = env.fake.get_entity  # type: ignore[union-attr]

    def no_timestamp(partition_key: str, row_key: str, **kwargs: Any) -> Any:
        entity = original(partition_key, row_key, **kwargs)
        entity.metadata["timestamp"] = None
        return entity

    env.fake.get_entity = no_timestamp  # type: ignore[union-attr,method-assign]
    with pytest.raises(RuntimeError, match="Timestamp"):
        env.store.repair_status_index(0)


def test_repair_never_deletes_a_row_without_a_timestamp_or_etag() -> None:
    env = fake_env()
    thread_id = env.store.create().thread_id
    env.store.delete(thread_id)  # leaves a stale hint
    real_query = env.fake.query_entities  # type: ignore[union-attr]

    def hide_metadata(query_filter: str | None = None, **kwargs: Any) -> Any:
        for row in real_query(query_filter, **kwargs):
            row.metadata.pop("etag", None)
            yield row

    env.fake.query_entities = hide_metadata  # type: ignore[union-attr,method-assign]
    assert env.store.repair_status_index(0) == 0
    assert len(env.index_rows("idle")) == 1


def test_repair_ignores_a_vanished_hint() -> None:
    env = fake_env()
    thread_id = env.store.create().thread_id
    env.store.delete(thread_id)
    row = env.index_rows("idle")[0]
    env.table.run("delete_entity", "idx:idle", lambda: env.inner.delete_entity("idx:idle", row))
    assert env.store.repair_status_index(0) == 0


def test_rebuild_and_audit_skip_rows_without_usable_status_or_created_at() -> None:
    env = fake_env()
    env.inner.create_entity({"PartitionKey": "thread", "RowKey": "odd", "status": "idle"})
    env.inner.create_entity(
        {"PartitionKey": "thread", "RowKey": "odd2", "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
    )
    assert env.store.rebuild_status_index().indexed == 0
    assert env.store.audit_status_index(mark_ready=True).marked_ready


def test_max_threads_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_threads"):
        fake_env().store.rebuild_status_index(max_threads=0)


def test_a_crash_between_marker_and_scan_leaves_the_marker_building() -> None:
    env = fake_env()
    env.store.create()
    env.table.crash("submit_transaction", None)
    with pytest.raises(SimulatedCrash):
        env.store.rebuild_status_index()
    assert env.store.status_index_state() == "building"


def test_the_fake_models_the_service_limits_the_design_relies_on() -> None:
    fake = FakeTable()
    with pytest.raises(ValueError, match="15"):
        fake.query_entities(" or ".join(f"RowKey eq 'k{i}'" for i in range(16)))
    with pytest.raises(FakeTransactionError):
        fake.submit_transaction([])
    with pytest.raises(FakeTransactionError):
        fake.submit_transaction(
            [
                ("upsert", {"PartitionKey": "a", "RowKey": "1"}),
                ("upsert", {"PartitionKey": "b", "RowKey": "1"}),
            ]
        )
    with pytest.raises(FakeTransactionError):
        fake.submit_transaction([("create", {"PartitionKey": "a", "RowKey": "1"})])
    fake.upsert_entity({"PartitionKey": "a", "RowKey": "1", "x": 1})
    with pytest.raises(FakeTransactionError):  # all-or-nothing: the delete of "2" fails the batch
        fake.submit_transaction(
            [
                ("upsert", {"PartitionKey": "a", "RowKey": "9"}, {"mode": "replace"}),
                ("delete", {"PartitionKey": "a", "RowKey": "2"}),
            ]
        )
    assert fake.partition("a") == ["1"]
    assert FakeClock().tick() > FakeClock().now
