"""Opt-in time-ordered status index for :class:`AzureTableThreadStore` (#801).

Design (see ``docs/adapter-reference.md``): index rows are *hints* that live in the same table
under ``PartitionKey="idx:<status>"`` with ``RowKey="<inverted created_at>~<thread_id>"``, so the
service's ascending RowKey order is ``created_at`` descending with a thread-id ascending
tie-break, exactly the order of the scan path. A reader verifies every hint against the primary
row and returns a thread only if its primary row has the requested status, so a stale hint can
never produce a wrong result. Writers upsert the hint *before* the primary compare-and-swap, so
while every writer is index-aware a thread is never missing from its status partition.

Only terminal statuses (``idle``, ``interrupted``, ``error``) are indexed; ``busy`` keeps the
scan. Nothing here runs unless the store was constructed with ``status_index=True``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import itertools
import logging
import time
from typing import Any, Literal, cast
import uuid

logger = logging.getLogger(__name__)

StatusIndexState = Literal["absent", "building", "ready", "disabled"]

INDEXED_STATUSES: tuple[str, ...] = ("idle", "interrupted", "error")

_PRIMARY_PARTITION = "thread"
_INDEX_PREFIX = "idx:"
_META_PARTITION = "idx-meta"
_MARKER_ROW = "status-index"
_CLOCK_ROW = "clock"
# Azure Table $filter allows at most 15 discrete comparisons; one is the PartitionKey clause.
_VERIFY_BATCH = 14
_TRANSACTION_LIMIT = 100
_MAX_PAGE = 1000
# 9999-12-31T23:59:59.999999Z in microseconds since the epoch; keeps the inverted value
# non-negative for every datetime, so the zero-padded 19-digit RowKey sorts numerically.
_MAX_MICROS = 253402300799999999
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

DEFAULT_OFFSET_CAP = 200
DEFAULT_STATE_TTL_SECONDS = 30.0
DEFAULT_REPAIR_GRACE_SECONDS = 600.0


def _quote(value: str) -> str:
    return value.replace("'", "''")


def index_partition(status: str) -> str:
    return f"{_INDEX_PREFIX}{status}"


def index_row_key(created_at: datetime, thread_id: str) -> str:
    """``RowKey`` of the hint: inverted ``created_at`` in microseconds, ``~``, the thread id."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    micros = (created_at.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)
    return f"{_MAX_MICROS - micros:019d}~{thread_id}"


def thread_id_of(index_row: str) -> str:
    return index_row.partition("~")[2]


@dataclass(frozen=True)
class StatusIndexBuildResult:
    """Outcome of :meth:`AzureTableThreadStore.rebuild_status_index`.

    ``last_row_key`` is the last primary row examined; pass it as ``resume_after`` to continue
    a run that stopped early (``complete`` is false). Re-running from the start is also safe.
    """

    scanned: int
    indexed: int
    last_row_key: str | None
    complete: bool


@dataclass(frozen=True)
class StatusIndexAuditReport:
    """Outcome of :meth:`AzureTableThreadStore.audit_status_index`.

    ``missing`` lists threads whose primary status is indexed but that have no hint: this is a
    completeness violation (an index-unaware writer, or a backfill not yet run). ``stale`` counts
    hints without a matching primary row; they are harmless and removed by repair.
    """

    scanned: int
    expected_hints: int
    missing: tuple[str, ...]
    stale: int
    marked_ready: bool

    @property
    def clean(self) -> bool:
        return not self.missing


class StatusIndex:
    """The index bound to one table; all Azure Table access of the feature goes through here."""

    def __init__(
        self,
        table_client: Any,
        *,
        not_found_error: type[BaseException],
        modified_error: type[BaseException],
        match_conditions: Any,
        offset_cap: int,
        state_ttl_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        for method in ("upsert_entity", "submit_transaction", "delete_entity"):
            if not callable(getattr(table_client, method, None)):
                raise ValueError(
                    f"status_index=True requires a table client with {method}(); "
                    "azure.data.tables.TableClient provides it"
                )
        if offset_cap < 0:
            raise ValueError(f"status_index_offset_cap must be non-negative, got {offset_cap}")
        if state_ttl_seconds < 0:
            raise ValueError(
                f"status_index_state_ttl_seconds must be non-negative, got {state_ttl_seconds}"
            )
        self._table = table_client
        self._not_found = not_found_error
        self._modified = modified_error
        self._match_conditions = match_conditions
        self._offset_cap = offset_cap
        self._ttl = state_ttl_seconds
        self._monotonic = monotonic
        self._cached_state: StatusIndexState | None = None
        self._cached_at = 0.0

    # -- write path -------------------------------------------------------------------------

    def upsert_hint(self, status: str, thread_id: str, created_at: datetime) -> None:
        """Idempotently write the hint for ``status`` (a no-op for non-indexed statuses).

        Every upsert carries a fresh ``u`` value so the row's ETag and ``Timestamp`` change on
        every call whatever the service does with identical content; a repair that deleted the
        hint conditionally on the ETag it read therefore always loses to a later upsert.
        """
        if status not in INDEXED_STATUSES:
            return
        self._table.upsert_entity(
            {
                "PartitionKey": index_partition(status),
                "RowKey": index_row_key(created_at, thread_id),
                "u": uuid.uuid4().hex,
            },
            mode="replace",
        )

    # -- readiness marker -------------------------------------------------------------------

    def state(self, *, refresh: bool = False) -> StatusIndexState:
        now = self._monotonic()
        if not refresh and self._cached_state is not None and now - self._cached_at < self._ttl:
            return self._cached_state
        try:
            entity = self._table.get_entity(partition_key=_META_PARTITION, row_key=_MARKER_ROW)
            raw = entity.get("state")
            state: StatusIndexState = (
                cast(StatusIndexState, raw)
                if raw in ("building", "ready", "disabled")
                else "absent"
            )
        except self._not_found:
            state = "absent"
        self._cached_state = state
        self._cached_at = now
        return state

    def set_state(self, state: StatusIndexState) -> None:
        self._table.upsert_entity(
            {"PartitionKey": _META_PARTITION, "RowKey": _MARKER_ROW, "state": state},
            mode="replace",
        )
        self._cached_state = state
        self._cached_at = self._monotonic()

    # -- read path --------------------------------------------------------------------------

    def search(self, status: str, limit: int, offset: int) -> list[dict[str, Any]] | None:
        """Verified primary rows for ``search(status=..., limit, offset)``; ``None`` = use the scan.

        The caller guarantees ``limit >= 0``, ``offset >= 0`` and no metadata filter.
        """
        if status not in INDEXED_STATUSES:
            return None
        if offset > self._offset_cap:
            logger.debug("status index skipped for offset %d (cap %d)", offset, self._offset_cap)
            return None
        state = self.state()
        if state != "ready":
            logger.debug("status index skipped, state is %s", state)
            return None
        if limit == 0:
            return []

        need = offset + limit
        pk = _quote(index_partition(status))
        hints = self._table.query_entities(
            query_filter=f"PartitionKey eq '{pk}'",
            select=["RowKey"],
            # A whole number of verification batches per page, so no page is fetched for a few rows.
            results_per_page=min(
                _MAX_PAGE - _MAX_PAGE % _VERIFY_BATCH, -(-need // _VERIFY_BATCH) * _VERIFY_BATCH
            ),
        )
        iterator: Iterator[Any] = iter(hints)
        verified: list[dict[str, Any]] = []
        while len(verified) < need:
            batch = [str(row["RowKey"]) for row in itertools.islice(iterator, _VERIFY_BATCH)]
            if not batch:
                break
            verified.extend(self._verified(status, batch))
        return verified[offset:need]

    def _fetch_primaries(
        self, thread_ids: list[str], select: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        clauses = " or ".join(f"RowKey eq '{_quote(thread_id)}'" for thread_id in thread_ids)
        query_filter = f"PartitionKey eq '{_PRIMARY_PARTITION}' and ({clauses})"
        if select is None:
            rows = self._table.query_entities(query_filter=query_filter)
        else:
            rows = self._table.query_entities(query_filter=query_filter, select=select)
        return {str(row["RowKey"]): row for row in rows}

    def _hint_is_current(self, status: str, hint_row: str, primary: dict[str, Any] | None) -> bool:
        if primary is None or primary.get("status") != status:
            return False
        created_at = primary.get("created_at")
        if not isinstance(created_at, datetime):
            return False
        return index_row_key(created_at, thread_id_of(hint_row)) == hint_row

    def _verified(self, status: str, hint_rows: list[str]) -> list[dict[str, Any]]:
        by_id = self._fetch_primaries([thread_id_of(row) for row in hint_rows])
        result: list[dict[str, Any]] = []
        for hint_row in hint_rows:
            primary = by_id.get(thread_id_of(hint_row))
            if primary is not None and self._hint_is_current(status, hint_row, primary):
                result.append(primary)
        return result

    # -- admin: backfill, audit, repair, rollback -------------------------------------------

    def _scan_primary(self, resume_after: str | None = None) -> Any:
        query_filter = f"PartitionKey eq '{_PRIMARY_PARTITION}'"
        if resume_after is not None:
            query_filter += f" and RowKey gt '{_quote(resume_after)}'"
        return self._table.query_entities(
            query_filter=query_filter, select=["RowKey", "status", "created_at"]
        )

    def _flush(self, status: str, pending: list[tuple[str, datetime]]) -> None:
        if not pending:
            return
        operations = [
            (
                "upsert",
                {
                    "PartitionKey": index_partition(status),
                    "RowKey": index_row_key(created_at, thread_id),
                    "u": uuid.uuid4().hex,
                },
                {"mode": "replace"},
            )
            for thread_id, created_at in pending
        ]
        self._table.submit_transaction(operations)
        pending.clear()

    def rebuild(
        self, *, resume_after: str | None = None, max_threads: int | None = None
    ) -> StatusIndexBuildResult:
        if max_threads is not None and max_threads < 1:
            raise ValueError(f"max_threads must be positive, got {max_threads}")
        self.set_state("building")
        pending: dict[str, list[tuple[str, datetime]]] = {s: [] for s in INDEXED_STATUSES}
        scanned = indexed = 0
        last: str | None = None
        complete = True
        for entity in self._scan_primary(resume_after):
            if max_threads is not None and scanned >= max_threads:
                complete = False
                break
            scanned += 1
            last = str(entity["RowKey"])
            status = entity.get("status")
            created_at = entity.get("created_at")
            if status in INDEXED_STATUSES and isinstance(created_at, datetime):
                bucket = pending[cast(str, status)]
                bucket.append((last, created_at))
                indexed += 1
                if len(bucket) >= _TRANSACTION_LIMIT:
                    self._flush(cast(str, status), bucket)
        for status, bucket in pending.items():
            self._flush(status, bucket)
        return StatusIndexBuildResult(scanned, indexed, last, complete)

    def _index_rows(self, status: str) -> Iterator[Any]:
        pk = _quote(index_partition(status))
        return iter(
            self._table.query_entities(query_filter=f"PartitionKey eq '{pk}'", select=["RowKey"])
        )

    def audit(self, *, mark_ready: bool = False) -> StatusIndexAuditReport:
        if mark_ready and self.state(refresh=True) not in ("building", "ready"):
            raise ValueError(
                f"status index state is {self.state()!r}; run rebuild_status_index() before "
                "audit_status_index(mark_ready=True)"
            )
        # Primary first, hints second: a hint is written before the primary row that needs it,
        # so a thread seen in the primary scan already has its hint when the index is scanned.
        expected: dict[str, dict[str, str]] = {s: {} for s in INDEXED_STATUSES}
        scanned = 0
        for entity in self._scan_primary():
            scanned += 1
            status = entity.get("status")
            created_at = entity.get("created_at")
            if status in INDEXED_STATUSES and isinstance(created_at, datetime):
                thread_id = str(entity["RowKey"])
                expected[cast(str, status)][index_row_key(created_at, thread_id)] = thread_id
        missing: list[str] = []
        stale = 0
        for status in INDEXED_STATUSES:
            wanted = dict(expected[status])
            for row in self._index_rows(status):
                if wanted.pop(str(row["RowKey"]), None) is None:
                    stale += 1
            missing.extend(wanted.values())
        report = StatusIndexAuditReport(
            scanned=scanned,
            expected_hints=sum(len(rows) for rows in expected.values()),
            missing=tuple(sorted(missing)),
            stale=stale,
            marked_ready=False,
        )
        if not mark_ready:
            return report
        current = self.state(refresh=True)
        if not report.clean:
            if current == "ready":
                self.set_state("building")
            return report
        self.set_state("ready")
        return StatusIndexAuditReport(
            report.scanned, report.expected_hints, report.missing, report.stale, True
        )

    def _service_now(self) -> datetime:
        """The table service's clock: the ``Timestamp`` of a row this call just wrote."""
        self._table.upsert_entity(
            {"PartitionKey": _META_PARTITION, "RowKey": _CLOCK_ROW, "n": uuid.uuid4().hex},
            mode="replace",
        )
        entity = self._table.get_entity(partition_key=_META_PARTITION, row_key=_CLOCK_ROW)
        stamp = getattr(entity, "metadata", {}).get("timestamp")
        if not isinstance(stamp, datetime):
            raise RuntimeError("the table service did not return an entity Timestamp")
        return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)

    def repair(self, grace_seconds: float) -> int:
        if grace_seconds < 0:
            raise ValueError(f"grace_seconds must be non-negative, got {grace_seconds}")
        cutoff = self._service_now() - timedelta(seconds=grace_seconds)
        deleted = 0
        for status in INDEXED_STATUSES:
            pk = _quote(index_partition(status))
            rows = iter(self._table.query_entities(query_filter=f"PartitionKey eq '{pk}'"))
            while True:
                batch = self._old_hints(rows, cutoff)
                if not batch:
                    break
                primaries = self._fetch_primaries(
                    [thread_id_of(row_key) for row_key, _ in batch],
                    select=["RowKey", "status", "created_at"],
                )
                for row_key, etag in batch:
                    primary = primaries.get(thread_id_of(row_key))
                    if self._hint_is_current(status, row_key, primary):
                        continue
                    deleted += self._delete_hint(status, row_key, etag)
        return deleted

    @staticmethod
    def _old_hints(rows: Iterator[Any], cutoff: datetime) -> list[tuple[str, str]]:
        """Up to a verification batch of ``(RowKey, ETag)`` older than ``cutoff``; ``[]`` = done.

        Hints younger than the grace period (or without a service Timestamp or ETag) are skipped
        here, never deleted: a writer's in-flight hint must survive.
        """
        batch: list[tuple[str, str]] = []
        for row in rows:
            stamp = getattr(row, "metadata", {}).get("timestamp")
            etag = getattr(row, "metadata", {}).get("etag")
            if not isinstance(stamp, datetime) or etag is None:
                continue
            if (stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)) >= cutoff:
                continue
            batch.append((str(row["RowKey"]), str(etag)))
            if len(batch) >= _VERIFY_BATCH:
                break
        return batch

    def _delete_hint(self, status: str, row_key: str, etag: str) -> int:
        try:
            self._table.delete_entity(
                index_partition(status),
                row_key,
                etag=etag,
                match_condition=self._match_conditions.IfNotModified,
            )
        except self._modified:
            return 0  # a writer re-upserted the hint after we read it: its hint wins
        except self._not_found:
            return 0
        return 1

    def disable(self, *, delete_rows: bool) -> int:
        self.set_state("disabled")
        if not delete_rows:
            return 0
        deleted = 0
        for status in INDEXED_STATUSES:
            rows = self._index_rows(status)
            while True:
                keys = [str(row["RowKey"]) for row in itertools.islice(rows, _TRANSACTION_LIMIT)]
                if not keys:
                    break
                deleted += self._delete_rows(status, keys)
        return deleted

    def _delete_rows(self, status: str, keys: list[str]) -> int:
        partition = index_partition(status)
        try:
            self._table.submit_transaction(
                [("delete", {"PartitionKey": partition, "RowKey": key}, {}) for key in keys]
            )
            return len(keys)
        except Exception:
            # An all-or-nothing batch fails as a whole if one row vanished meanwhile (a repair
            # or another disable): fall back to per-row deletes and skip the missing rows.
            removed = 0
            for key in keys:
                try:
                    self._table.delete_entity(partition, key)
                    removed += 1
                except self._not_found:
                    continue
            return removed
