"""Support for the Table status-index tests (#801): a fake Table model, a fault-injecting wrapper
and the ``check_*`` scenarios.

The scenarios run twice: against :class:`FakeTable` in the coverage-gated unit suite
(``tests/test_table_status_index.py``) and against Azurite through the real SDK in the Table
integration lane (``tests/integration/test_table_store_integration.py``). The wrapper
(:class:`FaultTable`) injects a crash or a concurrent writer between two steps of a store
operation on either backend, which is how the crash/412 analysis of the design is encoded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import re
import time
from typing import Any, Literal

import pytest

from azure_functions_langgraph.platform.contracts import Thread
from azure_functions_langgraph.stores._table_status_index import (
    index_partition,
    index_row_key,
    thread_id_of,
)
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore


class FakeResourceNotFoundError(Exception):
    pass


class FakeResourceModifiedError(Exception):
    pass


class FakeTransactionError(Exception):
    pass


class FakeMatchConditions:
    IfNotModified = "if-not-modified"


class SimulatedCrash(RuntimeError):
    """A process or network failure injected between two steps of a store operation."""


class FakeEntity(dict[str, Any]):
    def __init__(self, data: dict[str, Any], metadata: dict[str, Any]) -> None:
        super().__init__(data)
        self.metadata = metadata


_TOKEN = re.compile(r"\s*(\(|\)|'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*)")


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise ValueError(f"unsupported filter text at {position}: {text!r}")
        tokens.append(match.group(1))
        position = match.end()
    return tokens


class _Filter:
    """``and`` / ``or`` / parentheses over ``<name> eq|gt '<string>'`` comparisons."""

    def __init__(self, text: str) -> None:
        self.tokens = _tokenize(text)
        self.position = 0
        self.comparisons = 0
        self.tree = self._or()
        if self.position != len(self.tokens):
            raise ValueError(f"trailing tokens in filter {text!r}")

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self) -> str:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def _or(self) -> Any:
        node = self._and()
        while self._peek() == "or":
            self._take()
            node = ("or", node, self._and())
        return node

    def _and(self) -> Any:
        node = self._factor()
        while self._peek() == "and":
            self._take()
            node = ("and", node, self._factor())
        return node

    def _factor(self) -> Any:
        if self._peek() == "(":
            self._take()
            node = self._or()
            assert self._take() == ")"
            return node
        name, operator, literal = self._take(), self._take(), self._take()
        if operator not in {"eq", "gt"} or not literal.startswith("'"):
            raise ValueError(f"unsupported comparison {name} {operator} {literal}")
        self.comparisons += 1
        return ("cmp", name, operator, literal[1:-1].replace("''", "'"))

    def matches(self, entity: dict[str, Any], node: Any = None) -> bool:
        node = self.tree if node is None else node
        if node[0] == "cmp":
            _, name, operator, value = node
            actual = entity.get(name)
            if actual is None:
                return False
            return actual == value if operator == "eq" else bool(str(actual) > value)
        left = self.matches(entity, node[1])
        right = self.matches(entity, node[2])
        return (left and right) if node[0] == "and" else (left or right)


@dataclass
class FakeClock:
    """The fake service's clock: every write stamps the row with a strictly later time."""

    now: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def tick(self) -> datetime:
        self.now += timedelta(milliseconds=1)
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeTable:
    """An in-memory Azure Table with the semantics the status index relies on.

    Rows sort by (PartitionKey, RowKey); every write changes the ETag and ``Timestamp``;
    ``$select`` hides ``Timestamp`` (as the real SDK does); a query pages lazily and counts one
    request per page; ``$filter`` is limited to 15 comparisons; entity group transactions are
    all-or-nothing, single-partition and at most 100 operations.
    """

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.stamps: dict[tuple[str, str], datetime] = {}
        self.etags: dict[tuple[str, str], str] = {}
        self.requests: list[str] = []
        self.entities_read = 0
        self._counter = 0

    # -- internals --------------------------------------------------------------------------

    def _stamp(self, key: tuple[str, str]) -> None:
        self._counter += 1
        self.etags[key] = f'W/"{self._counter}"'
        self.stamps[key] = self.clock.tick()

    def _entity(self, key: tuple[str, str], select: list[str] | None = None) -> FakeEntity:
        data = self.rows[key]
        metadata: dict[str, Any] = {"etag": self.etags[key], "timestamp": None}
        if select is None:
            metadata["timestamp"] = self.stamps[key]
        else:
            keep = {"PartitionKey", "RowKey", *select}
            data = {name: value for name, value in data.items() if name in keep}
        return FakeEntity(deepcopy(data), metadata)

    def _check_etag(self, key: tuple[str, str], etag: str | None, match_condition: Any) -> None:
        if match_condition == FakeMatchConditions.IfNotModified and etag != self.etags[key]:
            raise FakeResourceModifiedError(key[1])

    def _write(self, entity: dict[str, Any], mode: str) -> None:
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if mode == "merge" and key in self.rows:
            merged = deepcopy(self.rows[key])
            merged.update(deepcopy(entity))
            self.rows[key] = merged
        else:
            self.rows[key] = deepcopy(entity)
        self._stamp(key)

    # -- the TableClient surface ------------------------------------------------------------

    def create_entity(self, entity: dict[str, Any]) -> None:
        self.requests.append("create_entity")
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key in self.rows:
            raise ValueError(f"Entity already exists: {key}")
        self._write(entity, "replace")

    def get_entity(self, partition_key: str, row_key: str, **_: Any) -> FakeEntity:
        self.requests.append("get_entity")
        key = (partition_key, row_key)
        if key not in self.rows:
            raise FakeResourceNotFoundError(row_key)
        self.entities_read += 1
        return self._entity(key)

    def update_entity(
        self,
        entity: dict[str, Any],
        mode: str = "merge",
        *,
        etag: str | None = None,
        match_condition: Any = None,
    ) -> None:
        self.requests.append("update_entity")
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key not in self.rows:
            raise FakeResourceNotFoundError(key[1])
        self._check_etag(key, etag, match_condition)
        self._write(entity, mode)

    def upsert_entity(self, entity: dict[str, Any], mode: str = "merge", **_: Any) -> None:
        self.requests.append("upsert_entity")
        self._write(entity, mode)

    def delete_entity(
        self,
        partition_key: str,
        row_key: str,
        *,
        etag: str | None = None,
        match_condition: Any = None,
    ) -> None:
        self.requests.append("delete_entity")
        key = (partition_key, row_key)
        if key not in self.rows:
            raise FakeResourceNotFoundError(row_key)
        self._check_etag(key, etag, match_condition)
        del self.rows[key]
        del self.stamps[key]
        del self.etags[key]

    def submit_transaction(self, operations: list[tuple[Any, ...]]) -> list[Any]:
        self.requests.append("submit_transaction")
        if not 0 < len(operations) <= 100:
            raise FakeTransactionError("an entity group transaction holds 1 to 100 operations")
        partitions = {str(op[1]["PartitionKey"]) for op in operations}
        if len(partitions) != 1:
            raise FakeTransactionError("a transaction must stay in one partition")
        staged = deepcopy((self.rows, self.stamps, self.etags))
        try:
            for operation in operations:
                kind, entity = operation[0], operation[1]
                options = operation[2] if len(operation) > 2 else {}
                key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
                if kind == "upsert":
                    self._write(entity, options.get("mode", "merge"))
                elif kind == "delete":
                    if key not in self.rows:
                        raise FakeTransactionError(f"{key[1]} not found")
                    del self.rows[key], self.stamps[key], self.etags[key]
                else:
                    raise FakeTransactionError(f"unsupported operation {kind}")
        except FakeTransactionError:
            self.rows, self.stamps, self.etags = staged
            raise
        return [None] * len(operations)

    def query_entities(
        self,
        query_filter: str | None = None,
        *,
        select: list[str] | None = None,
        results_per_page: int | None = None,
        **_: Any,
    ) -> Iterator[FakeEntity]:
        parsed = _Filter(query_filter) if query_filter else None
        if parsed is not None and parsed.comparisons > 15:
            raise ValueError("$filter allows at most 15 discrete comparisons")
        size = min(results_per_page or 1000, 1000)

        def pages() -> Iterator[FakeEntity]:
            keys = sorted(
                key for key in self.rows if parsed is None or parsed.matches({**self.rows[key]})
            )
            for start in range(0, max(len(keys), 1), size):
                self.requests.append("query_page")
                chunk = keys[start : start + size]
                self.entities_read += len(chunk)
                yield from (self._entity(key, select) for key in chunk)

        return pages()

    # -- helpers for tests ------------------------------------------------------------------

    def count(self, *kinds: str) -> int:
        return sum(1 for request in self.requests if request in kinds)

    def partition(self, partition_key: str) -> list[str]:
        return sorted(row for pk, row in self.rows if pk == partition_key)


WRITES = ("create_entity", "update_entity", "upsert_entity", "delete_entity", "submit_transaction")


@dataclass
class _Fault:
    op: str
    partition: str | None
    when: Literal["before", "after"]
    action: Callable[[], None] | None
    remaining: int


class FaultTable:
    """Wraps any table client (fake or real) to inject a crash or a concurrent writer.

    ``crash`` raises :class:`SimulatedCrash` before the matching call (the step never ran) or
    after it (the step ran but the caller saw an error, the F9 case). ``run`` calls a function
    right before the matching call, which is how a concurrent writer is interleaved.
    ``queries`` records every ``$filter`` so a test can prove which partitions a search read.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.faults: list[_Fault] = []
        self.queries: list[str] = []

    def crash(
        self,
        op: str,
        partition: str | None = None,
        *,
        when: Literal["before", "after"] = "before",
        times: int = 1,
    ) -> None:
        self.faults.append(_Fault(op, partition, when, None, times))

    def run(self, op: str, partition: str | None, action: Callable[[], None]) -> None:
        self.faults.append(_Fault(op, partition, "before", action, 1))

    def _fire(self, op: str, partition: str | None, when: str) -> None:
        for fault in list(self.faults):
            if fault.op != op or fault.when != when or fault.remaining <= 0:
                continue
            if fault.partition is not None and fault.partition != partition:
                continue
            fault.remaining -= 1
            if fault.action is None:
                raise SimulatedCrash(f"{when} {op} on {partition}")
            fault.action()

    def _call(self, op: str, partition: str | None, *args: Any, **kwargs: Any) -> Any:
        self._fire(op, partition, "before")
        result = getattr(self.inner, op)(*args, **kwargs)
        self._fire(op, partition, "after")
        return result

    def create_entity(self, entity: dict[str, Any], **kwargs: Any) -> Any:
        return self._call("create_entity", entity["PartitionKey"], entity, **kwargs)

    def get_entity(self, partition_key: str, row_key: str, **kwargs: Any) -> Any:
        return self.inner.get_entity(partition_key=partition_key, row_key=row_key, **kwargs)

    def update_entity(self, entity: dict[str, Any], mode: str = "merge", **kwargs: Any) -> Any:
        return self._call("update_entity", entity["PartitionKey"], entity, mode, **kwargs)

    def upsert_entity(self, entity: dict[str, Any], mode: str = "merge", **kwargs: Any) -> Any:
        return self._call("upsert_entity", entity["PartitionKey"], entity, mode=mode, **kwargs)

    def delete_entity(self, partition_key: str, row_key: str, **kwargs: Any) -> Any:
        return self._call("delete_entity", partition_key, partition_key, row_key, **kwargs)

    def submit_transaction(self, operations: list[tuple[Any, ...]]) -> Any:
        return self._call("submit_transaction", operations[0][1]["PartitionKey"], operations)

    def query_entities(self, query_filter: str | None = None, **kwargs: Any) -> Any:
        self.queries.append(query_filter or "")
        return self.inner.query_entities(query_filter, **kwargs)


# ---------------------------------------------------------------------------------------------
# Environment shared by the scenarios
# ---------------------------------------------------------------------------------------------


@dataclass
class Env:
    """One table, an index-enabled store, a plain scan-path store and the fault wrapper."""

    inner: Any
    table: FaultTable
    store: AzureTableThreadStore
    plain: AzureTableThreadStore
    new_store: Callable[..., AzureTableThreadStore]
    wait: Callable[[float], None]
    modified_error: type[BaseException]
    fake: FakeTable | None = None
    notes: list[str] = field(default_factory=list)

    def index_rows(self, status: str) -> list[str]:
        rows = self.inner.query_entities(
            f"PartitionKey eq '{index_partition(status)}'", select=["RowKey"]
        )
        return sorted(str(row["RowKey"]) for row in rows)

    def all_index_rows(self) -> dict[str, list[str]]:
        return {s: self.index_rows(s) for s in ("idle", "interrupted", "error")}

    def reset_queries(self) -> None:
        self.table.queries.clear()

    def index_queried(self) -> bool:
        return any("idx:" in query for query in self.table.queries)


def fake_env(*, status_index_offset_cap: int = 200) -> Env:
    fake = FakeTable()
    table = FaultTable(fake)

    def new_store(wrapped: Any = table, **options: Any) -> AzureTableThreadStore:
        return AzureTableThreadStore(
            table_client=wrapped,
            not_found_error=FakeResourceNotFoundError,
            modified_error=FakeResourceModifiedError,
            match_conditions=FakeMatchConditions,
            **options,
        )

    return Env(
        inner=fake,
        table=table,
        store=new_store(
            status_index=True,
            status_index_offset_cap=status_index_offset_cap,
            status_index_state_ttl_seconds=0,
        ),
        plain=new_store(fake),
        new_store=new_store,
        wait=lambda seconds: fake.clock.advance(seconds),
        modified_error=FakeResourceModifiedError,
        fake=fake,
    )


def make_ready(env: Env) -> None:
    env.store.rebuild_status_index()
    report = env.store.audit_status_index(mark_ready=True)
    assert report.marked_ready, report
    assert env.store.status_index_state() == "ready"


def ids(threads: list[Thread]) -> list[str]:
    return [thread.thread_id for thread in threads]


def assert_equivalent(env: Env, *, offsets: tuple[int, ...] = (0, 1, 3, 50)) -> None:
    """Index-path results equal the scan path for every status, limit and offset."""
    for status in ("idle", "interrupted", "error", "busy"):
        for limit in (0, 1, 2, 5, 100):
            for offset in offsets:
                got = ids(env.store.search(status=status, limit=limit, offset=offset))
                want = ids(env.plain.search(status=status, limit=limit, offset=offset))
                assert got == want, (status, limit, offset)


def seed(env: Env, thread_id: str, created_at: datetime, status: str, **extra: Any) -> None:
    """Write a primary row directly (no hint), as an index-unaware writer or old data would."""
    now = datetime.now(UTC)
    env.inner.create_entity(
        {
            "PartitionKey": "thread",
            "RowKey": thread_id,
            "created_at": created_at,
            "updated_at": now,
            "status": status,
            "interrupts_json": "{}",
            **extra,
        }
    )


def populate(env: Env) -> dict[str, str]:
    """A mix of threads in every status, created through the store (so hinted)."""
    made: dict[str, str] = {}
    for position in range(4):
        made[f"idle{position}"] = env.store.create(metadata={"n": position}).thread_id
    made["err"] = env.store.create().thread_id
    env.store.update(made["err"], status="error")
    made["int"] = env.store.create().thread_id
    env.store.update(made["int"], status="interrupted")
    made["busy"] = env.store.create().thread_id
    assert env.store.try_acquire_run_lock(made["busy"]) is not None
    made["released"] = env.store.create().thread_id
    lock = env.store.try_acquire_run_lock(made["released"])
    assert lock is not None
    env.store.release_run_lock(made["released"], status="error", owner_token=lock.lock_token)
    return made


# ---------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------


def check_default_off_writes_no_index_and_reads_no_index(env: Env) -> None:
    plain = env.new_store()
    thread = plain.create()
    plain.update(thread.thread_id, status="error")
    lock = plain.try_acquire_run_lock(thread.thread_id)
    assert lock is not None
    plain.release_run_lock(thread.thread_id, status="idle", owner_token=lock.lock_token)
    plain.search(status="idle")
    plain.delete(thread.thread_id)
    assert env.all_index_rows() == {"idle": [], "interrupted": [], "error": []}
    assert not any("idx" in query for query in env.table.queries)
    with pytest.raises(RuntimeError, match="status_index=True"):
        plain.audit_status_index()


def check_ready_index_matches_scan(env: Env) -> None:
    populate(env)
    make_ready(env)
    env.reset_queries()
    assert_equivalent(env)
    assert env.index_queried()


def check_created_at_ties_keep_thread_id_order(env: Env) -> None:
    moment = datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)
    for thread_id in ("c", "a", "b"):
        seed(env, thread_id, moment, "idle")
    seed(env, "z", moment - timedelta(seconds=1), "idle")
    seed(env, "y", moment + timedelta(seconds=1), "idle")
    make_ready(env)
    assert ids(env.store.search(status="idle", limit=10)) == ["y", "a", "b", "c", "z"]
    assert_equivalent(env)


def check_not_ready_states_scan_and_never_read_the_index(env: Env) -> None:
    populate(env)
    for prepare in ("absent", "building", "disabled"):
        if prepare == "building":
            env.store.rebuild_status_index()
        if prepare == "disabled":
            env.store.disable_status_index()
        assert env.store.status_index_state() == prepare
        env.reset_queries()
        assert_equivalent(env)
        assert not env.index_queried(), prepare


def check_offset_cap_falls_back_to_scan(env: Env) -> None:
    capped = env.new_store(
        status_index=True, status_index_offset_cap=2, status_index_state_ttl_seconds=0
    )
    populate(env)
    make_ready(env)
    env.reset_queries()
    assert ids(capped.search(status="idle", limit=2, offset=2)) == ids(
        env.plain.search(status="idle", limit=2, offset=2)
    )
    assert env.index_queried()
    env.reset_queries()
    assert ids(capped.search(status="idle", limit=2, offset=3)) == ids(
        env.plain.search(status="idle", limit=2, offset=3)
    )
    assert not env.index_queried()


def check_unindexed_queries_keep_scanning(env: Env) -> None:
    populate(env)
    make_ready(env)
    env.reset_queries()
    env.store.search(limit=5)
    env.store.search(metadata={"n": 1}, status="idle")
    env.store.search(status="busy")
    env.store.count(status="idle")
    env.store.count()
    assert not env.index_queried()
    assert env.store.count(status="idle") == env.plain.count(status="idle")
    assert ids(env.store.search(metadata={"n": 1}, status="idle")) == ids(
        env.plain.search(metadata={"n": 1}, status="idle")
    )


def check_a_crash_between_the_hint_and_the_create_leaves_only_a_harmless_orphan(env: Env) -> None:
    keep = env.store.create().thread_id
    make_ready(env)
    env.table.crash("create_entity", "thread")
    with pytest.raises(SimulatedCrash):
        env.store.create()
    assert len(env.index_rows("idle")) == 2  # the orphan hint
    assert ids(env.store.search(status="idle")) == [keep]
    assert_equivalent(env)
    env.wait(0.05)
    assert env.store.repair_status_index(0) == 1
    assert len(env.index_rows("idle")) == 1


def check_a_crash_before_the_hint_creates_nothing(env: Env) -> None:
    env.table.crash("upsert_entity", "idx:idle")
    with pytest.raises(SimulatedCrash):
        env.store.create()
    assert env.plain.count() == 0
    assert env.all_index_rows()["idle"] == []


def check_a_crash_after_the_hint_before_the_cas_keeps_the_primary_status(env: Env) -> None:
    thread_id = env.store.create().thread_id
    make_ready(env)
    env.table.crash("update_entity", "thread")
    with pytest.raises(SimulatedCrash):
        env.store.update(thread_id, status="error")
    assert len(env.index_rows("error")) == 1  # write-ahead hint, now stale
    assert ids(env.store.search(status="error")) == []
    assert ids(env.store.search(status="idle")) == [thread_id]
    assert_equivalent(env)
    env.store.update(thread_id, status="error")  # the retry succeeds, the hint is reused
    assert ids(env.store.search(status="error")) == [thread_id]
    assert len(env.index_rows("error")) == 1


def check_a_crash_after_the_cas_leaves_the_old_hint_stale_not_wrong(env: Env) -> None:
    thread_id = env.store.create().thread_id
    make_ready(env)
    env.table.crash("update_entity", "thread", when="after")
    with pytest.raises(SimulatedCrash):
        env.store.update(thread_id, status="error")
    # The write landed although the caller saw an error: the hint for the new status exists
    # (written first) and the old one is stale and filtered out on read.
    assert ids(env.store.search(status="error")) == [thread_id]
    assert ids(env.store.search(status="idle")) == []
    assert len(env.index_rows("idle")) == 1
    assert_equivalent(env)


def check_a_lost_etag_race_retries_and_the_final_status_is_hinted(env: Env) -> None:
    thread_id = env.store.create().thread_id
    make_ready(env)

    def other_writer() -> None:
        env.plain.update(thread_id, metadata={"racer": True})

    env.table.run("update_entity", "thread", other_writer)
    assert env.store.update(thread_id, status="interrupted").status == "interrupted"
    assert ids(env.store.search(status="interrupted")) == [thread_id]
    assert env.store.get(thread_id).metadata == {"racer": True}  # type: ignore[union-attr]
    assert_equivalent(env)


def check_concurrent_writers_to_different_targets_leave_one_winner(env: Env) -> None:
    thread_id = env.store.create().thread_id
    make_ready(env)

    def writer_b() -> None:
        env.store.update(thread_id, status="interrupted")  # a complete, index-aware writer

    env.table.run("update_entity", "thread", writer_b)
    env.store.update(thread_id, status="error")  # A: 412, re-read, applies its own status
    final = env.store.get(thread_id)
    assert final is not None and final.status == "error"
    rows = env.all_index_rows()
    assert len(rows["error"]) == 1 and len(rows["interrupted"]) == 1  # both hints, one stale
    assert ids(env.store.search(status="error")) == [thread_id]
    assert ids(env.store.search(status="interrupted")) == []
    assert_equivalent(env)


def check_lock_acquire_and_release_keep_the_index_consistent(env: Env) -> None:
    thread_id = env.store.create().thread_id
    make_ready(env)
    lock = env.store.try_acquire_run_lock(thread_id)
    assert lock is not None
    assert ids(env.store.search(status="idle")) == []  # the idle hint is stale while busy
    assert ids(env.store.search(status="busy")) == [thread_id]
    env.store.release_run_lock(thread_id, status="interrupted", owner_token=lock.lock_token)
    assert ids(env.store.search(status="interrupted")) == [thread_id]
    assert_equivalent(env)
    again = env.store.try_acquire_run_lock(thread_id)
    assert again is not None
    env.store.release_run_lock(thread_id, status="idle", owner_token=again.lock_token)
    assert ids(env.store.search(status="idle")) == [thread_id]
    assert_equivalent(env)


def check_reset_stale_locks_hints_the_recovery_status(env: Env) -> None:
    thread_id = env.store.create().thread_id
    assert env.store.try_acquire_run_lock(thread_id) is not None
    make_ready(env)
    time.sleep(0.02)
    assert env.store.reset_stale_locks(0, status="error") == 1
    assert ids(env.store.search(status="error")) == [thread_id]
    assert_equivalent(env)


def check_delete_leaves_a_stale_hint_that_repair_removes(env: Env) -> None:
    keep = env.store.create().thread_id
    gone = env.store.create().thread_id
    make_ready(env)
    env.store.delete(gone)
    assert ids(env.store.search(status="idle")) == [keep]
    assert len(env.index_rows("idle")) == 2
    assert_equivalent(env)
    env.wait(0.05)
    assert env.store.repair_status_index(0) == 1
    assert [thread_id_of(row) for row in env.index_rows("idle")] == [keep]
    assert env.store.audit_status_index().clean


def check_metadata_updates_write_no_hint(env: Env) -> None:
    thread_id = env.store.create().thread_id
    before = env.all_index_rows()
    env.store.update(thread_id, metadata={"a": 1})
    env.store.merge_metadata(thread_id, {"b": 2})
    assert env.all_index_rows() == before


def check_an_index_unaware_writer_is_detected_and_blocks_readiness(env: Env) -> None:
    kept = env.store.create().thread_id
    make_ready(env)
    old_writer = env.new_store()  # an old-version instance: creates without a hint
    unhinted = old_writer.create().thread_id
    # Readers still trust the marker, so the search misses the thread: the documented limit.
    assert ids(env.store.search(status="idle")) == [kept]
    report = env.store.audit_status_index(mark_ready=True)
    assert report.missing == (unhinted,) and not report.clean and not report.marked_ready
    assert env.store.status_index_state() == "building"  # demoted: readers scan again
    assert ids(env.store.search(status="idle", limit=10)) == ids(
        env.plain.search(status="idle", limit=10)
    )
    assert unhinted in ids(env.store.search(status="idle", limit=10))
    result = env.store.rebuild_status_index()
    assert result.indexed >= 2
    assert env.store.audit_status_index(mark_ready=True).marked_ready
    assert unhinted in ids(env.store.search(status="idle", limit=10))


def check_mark_ready_requires_a_rebuild_first(env: Env) -> None:
    env.store.create()
    assert env.store.status_index_state() == "absent"
    with pytest.raises(ValueError, match="rebuild_status_index"):
        env.store.audit_status_index(mark_ready=True)
    assert env.store.audit_status_index().clean  # a plain audit never changes the marker
    assert env.store.status_index_state() == "absent"


def check_rebuild_is_idempotent_and_resumable(env: Env) -> None:
    for position in range(7):
        seed(env, f"t{position}", datetime(2026, 1, 1 + position, tzinfo=UTC), "idle")
    seed(env, "e0", datetime(2026, 2, 1, tzinfo=UTC), "error")
    seed(env, "b0", datetime(2026, 2, 2, tzinfo=UTC), "busy")
    first = env.store.rebuild_status_index(max_threads=3)
    assert (first.scanned, first.complete) == (3, False)
    rows_after_slice = sum(len(rows) for rows in env.all_index_rows().values())
    assert rows_after_slice == first.indexed
    rest = env.store.rebuild_status_index(resume_after=first.last_row_key)
    assert rest.complete and first.scanned + rest.scanned == 9
    assert first.indexed + rest.indexed == 8  # busy is not indexed
    snapshot = env.all_index_rows()
    again = env.store.rebuild_status_index()  # a full re-run changes nothing observable
    assert again.indexed == 8 and env.all_index_rows() == snapshot
    assert env.store.audit_status_index(mark_ready=True).marked_ready
    assert_equivalent(env)


def check_a_failed_backfill_batch_is_healed_by_rerunning(env: Env) -> None:
    for position in range(5):
        seed(env, f"t{position}", datetime(2026, 1, 1 + position, tzinfo=UTC), "idle")
    env.table.crash("submit_transaction", "idx:idle")
    with pytest.raises(SimulatedCrash):
        env.store.rebuild_status_index()
    assert env.store.status_index_state() == "building"
    assert not env.store.audit_status_index().clean  # readiness is refused
    env.store.rebuild_status_index()
    assert env.store.audit_status_index(mark_ready=True).marked_ready
    assert_equivalent(env)


def check_a_large_backfill_uses_bounded_transactions(env: Env) -> None:
    for position in range(205):
        seed(
            env,
            f"t{position:04d}",
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=position),
            "idle",
        )
    result = env.store.rebuild_status_index()
    assert result.indexed == 205
    assert len(env.index_rows("idle")) == 205
    assert env.store.audit_status_index(mark_ready=True).marked_ready


def check_repair_skips_hints_inside_the_grace_period(env: Env) -> None:
    keep = env.store.create().thread_id
    gone = env.store.create().thread_id
    env.store.delete(gone)
    assert env.store.repair_status_index(3600) == 0
    assert len(env.index_rows("idle")) == 2
    assert env.store.audit_status_index().stale == 1
    assert ids(env.store.search(status="idle")) == [keep]  # no ready marker: the scan answers


def check_repair_loses_to_a_concurrent_writer(env: Env) -> None:
    thread_id = env.store.create().thread_id
    env.store.update(thread_id, status="error")
    # The idle hint is stale. Between repair's read and its conditional delete a writer moves
    # the thread back to idle and re-upserts the hint: the delete must lose.
    env.wait(0.05)

    def writer() -> None:
        env.store.update(thread_id, status="idle")

    env.table.run("delete_entity", "idx:idle", writer)
    # Only the now-stale "error" hint is removed; the idle delete lost its ETag race.
    assert env.store.repair_status_index(0) == 1
    assert len(env.index_rows("idle")) == 1 and env.index_rows("error") == []
    make_ready(env)
    assert ids(env.store.search(status="idle")) == [thread_id]


def check_repair_removes_a_hint_whose_thread_changed_status(env: Env) -> None:
    thread_id = env.store.create().thread_id
    env.store.update(thread_id, status="error")
    env.wait(0.05)
    assert env.store.repair_status_index(0) == 1
    assert env.index_rows("idle") == []
    assert len(env.index_rows("error")) == 1  # the current hint stays


def check_disable_and_rollback(env: Env) -> None:
    populate(env)
    make_ready(env)
    assert env.store.disable_status_index() == 0
    assert env.store.status_index_state() == "disabled"
    env.reset_queries()
    assert_equivalent(env)
    assert not env.index_queried()
    removed = env.store.disable_status_index(delete_rows=True)
    assert removed > 0
    assert env.all_index_rows() == {"idle": [], "interrupted": [], "error": []}
    with pytest.raises(ValueError, match="rebuild_status_index"):
        env.store.audit_status_index(mark_ready=True)
    make_ready_again = env.store.rebuild_status_index()
    assert make_ready_again.indexed > 0
    assert env.store.audit_status_index(mark_ready=True).marked_ready
    assert_equivalent(env)


def check_delete_rows_survives_a_vanished_row(env: Env) -> None:
    for position in range(3):
        env.store.create()
    first = env.index_rows("idle")[0]

    def vanish() -> None:
        env.inner.delete_entity("idx:idle", first)

    env.table.run("submit_transaction", "idx:idle", vanish)
    # The SDK's delete_entity does not raise for a row that is already gone, so the fake and the
    # real table may count it or not: what matters is that the batch failure does not abort.
    assert env.store.disable_status_index(delete_rows=True) in (2, 3)
    assert env.index_rows("idle") == []


def check_a_ready_index_demotes_on_rebuild_and_reads_scan(env: Env) -> None:
    populate(env)
    make_ready(env)
    env.store.rebuild_status_index(max_threads=1)
    assert env.store.status_index_state() == "building"
    env.reset_queries()
    assert_equivalent(env)
    assert not env.index_queried()


def check_every_hint_key_orders_like_created_at_descending(env: Env) -> None:
    base = datetime(2026, 5, 5, tzinfo=UTC)
    keys = [index_row_key(base + timedelta(microseconds=n), "x") for n in (0, 1, 999999, 1000000)]
    assert keys == sorted(keys, reverse=True)
    assert all(len(key.partition("~")[0]) == 19 for key in keys)
    assert index_row_key(datetime(1970, 1, 1), "t") == index_row_key(
        datetime(1970, 1, 1, tzinfo=UTC), "t"
    )
    assert index_row_key(datetime(1, 1, 1, tzinfo=UTC), "t") > index_row_key(
        datetime(9999, 12, 31, tzinfo=UTC), "t"
    )
    assert thread_id_of(keys[0]) == "x"


def check_a_hint_with_the_wrong_created_at_is_never_trusted(env: Env) -> None:
    moment = datetime(2026, 4, 1, tzinfo=UTC)
    seed(env, "real", moment, "idle")
    make_ready(env)
    forged = index_row_key(moment + timedelta(days=1), "real")
    env.inner.upsert_entity({"PartitionKey": "idx:idle", "RowKey": forged}, mode="replace")
    assert ids(env.store.search(status="idle", limit=10)) == ["real"]
    assert ids(env.store.search(status="idle", limit=10)) == ids(
        env.plain.search(status="idle", limit=10)
    )


def check_quotes_in_ids_are_escaped(env: Env) -> None:
    seed(env, "it's", datetime(2026, 4, 1, tzinfo=UTC), "idle")
    seed(env, "plain", datetime(2026, 4, 2, tzinfo=UTC), "idle")
    make_ready(env)
    assert ids(env.store.search(status="idle", limit=10)) == ["plain", "it's"]
    assert env.store.audit_status_index().clean


CHECKS: list[Callable[[Env], None]] = [
    check_default_off_writes_no_index_and_reads_no_index,
    check_ready_index_matches_scan,
    check_created_at_ties_keep_thread_id_order,
    check_not_ready_states_scan_and_never_read_the_index,
    check_offset_cap_falls_back_to_scan,
    check_unindexed_queries_keep_scanning,
    check_a_crash_between_the_hint_and_the_create_leaves_only_a_harmless_orphan,
    check_a_crash_before_the_hint_creates_nothing,
    check_a_crash_after_the_hint_before_the_cas_keeps_the_primary_status,
    check_a_crash_after_the_cas_leaves_the_old_hint_stale_not_wrong,
    check_a_lost_etag_race_retries_and_the_final_status_is_hinted,
    check_concurrent_writers_to_different_targets_leave_one_winner,
    check_lock_acquire_and_release_keep_the_index_consistent,
    check_reset_stale_locks_hints_the_recovery_status,
    check_delete_leaves_a_stale_hint_that_repair_removes,
    check_metadata_updates_write_no_hint,
    check_an_index_unaware_writer_is_detected_and_blocks_readiness,
    check_mark_ready_requires_a_rebuild_first,
    check_rebuild_is_idempotent_and_resumable,
    check_a_failed_backfill_batch_is_healed_by_rerunning,
    check_a_large_backfill_uses_bounded_transactions,
    check_repair_skips_hints_inside_the_grace_period,
    check_repair_loses_to_a_concurrent_writer,
    check_repair_removes_a_hint_whose_thread_changed_status,
    check_disable_and_rollback,
    check_delete_rows_survives_a_vanished_row,
    check_a_ready_index_demotes_on_rebuild_and_reads_scan,
    check_every_hint_key_orders_like_created_at_descending,
    check_a_hint_with_the_wrong_created_at_is_never_trusted,
    check_quotes_in_ids_are_escaped,
]
