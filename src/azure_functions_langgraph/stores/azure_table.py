"""Azure Table Storage-backed thread store for LangGraph Platform API.

.. versionadded:: 0.4.0
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
import importlib
import json
import logging
from typing import Any, Literal, Protocol, cast
import uuid

from ..platform.contracts import Interrupt, Thread, ThreadStatus
from ..platform.stores import (
    RunLockReleaseOutcome,
    ThreadStore,
    _release_with_outcome,
    _ReleaseSignal,
)
from ._errors import RunLockReleaseError, RunLockRenewalConflictError
from ._table_json import read_json, validate_entity, write_json
from ._table_status_index import (
    DEFAULT_OFFSET_CAP,
    DEFAULT_REPAIR_GRACE_SECONDS,
    DEFAULT_STATE_TTL_SECONDS,
    StatusIndex,
    StatusIndexAuditReport,
    StatusIndexBuildResult,
    StatusIndexState,
)

logger = logging.getLogger(__name__)

_ENTITY_SIZE_LIMIT_BYTES = 1024 * 1024
_ENTITY_SIZE_WARN_THRESHOLD = int(_ENTITY_SIZE_LIMIT_BYTES * 0.9)


class _TableClientProtocol(Protocol):
    def create_entity(self, entity: dict[str, Any]) -> None: ...

    def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]: ...

    def update_entity(
        self,
        entity: dict[str, Any],
        mode: str,
        *,
        etag: str | None = None,
        match_condition: Any = None,
    ) -> None: ...

    def delete_entity(self, partition_key: str, row_key: str) -> None: ...

    def query_entities(self, query_filter: str | None = None, **kwargs: Any) -> Any: ...


class AzureTableThreadStore(ThreadStore):
    """Persist thread metadata in Azure Table Storage.

    Note:
        Single-partition design. Works well for up to ~100K threads. At
        higher scale, the single partition may become a throughput
        bottleneck and client-side filtering for search/count becomes
        expensive. See DESIGN.md decision #8 for scale envelope details.

    Also provides atomic run-lock acquisition via Azure Table ETag
    compare-and-swap and owner-fenced lock release via conditional replacement.

    Opt-in status index (``status_index=True``, off by default): ``search(status=...)`` without a
    metadata filter can be served from per-status, time-ordered *hint* rows in the same table
    (``PartitionKey="idx:<status>"``) instead of scanning every thread. Hints are written before
    the primary compare-and-swap and verified against the primary row on every read, so results
    and ordering equal the scan path; a hint may be stale but, while every writer is index-aware,
    is never missing. Only ``idle``, ``interrupted`` and ``error`` are indexed. Readers use the
    index only while the marker row says ``ready`` and ``offset <= status_index_offset_cap``;
    otherwise they scan. Unfiltered search, metadata filters and :meth:`count` always scan.
    Operate it with :meth:`rebuild_status_index`, :meth:`audit_status_index`,
    :meth:`repair_status_index`, :meth:`status_index_state` and :meth:`disable_status_index`;
    see ``docs/adapter-reference.md`` for the rollout, rollback and consistency contract.
    """

    def __init__(
        self,
        *,
        table_client: _TableClientProtocol,
        not_found_error: type[BaseException],
        modified_error: type[BaseException],
        match_conditions: Any,
        status_index: bool = False,
        status_index_offset_cap: int = DEFAULT_OFFSET_CAP,
        status_index_state_ttl_seconds: float = DEFAULT_STATE_TTL_SECONDS,
    ) -> None:
        if not_found_error is None:
            raise ValueError(
                "not_found_error must be provided for protocol-based construction, "
                "or create the store with from_connection_string()"
            )
        if modified_error is None:
            raise ValueError(
                "modified_error must be provided for protocol-based construction, "
                "or create the store with from_connection_string()"
            )
        if match_conditions is None:
            raise ValueError(
                "match_conditions must be provided for protocol-based construction, "
                "or create the store with from_connection_string()"
            )

        self._table_client = table_client
        self._not_found_error: type[BaseException] = not_found_error
        self._modified_error: type[BaseException] = modified_error
        self._match_conditions = match_conditions
        self._release_signal = _ReleaseSignal()
        self._status_index: StatusIndex | None = (
            StatusIndex(
                table_client,
                not_found_error=not_found_error,
                modified_error=modified_error,
                match_conditions=match_conditions,
                offset_cap=status_index_offset_cap,
                state_ttl_seconds=status_index_state_ttl_seconds,
            )
            if status_index
            else None
        )

    @classmethod
    def from_connection_string(
        cls,
        connection_string: str,
        table_name: str,
        *,
        status_index: bool = False,
        status_index_offset_cap: int = DEFAULT_OFFSET_CAP,
        status_index_state_ttl_seconds: float = DEFAULT_STATE_TTL_SECONDS,
    ) -> AzureTableThreadStore:
        table_client_class, not_found_error, modified_error, match_conditions_cls = (
            cls._load_azure_sdk_symbols()
        )

        table_client = table_client_class.from_connection_string(
            conn_str=connection_string,
            table_name=table_name,
        )
        return cls(
            table_client=cast(_TableClientProtocol, table_client),
            not_found_error=not_found_error,
            modified_error=modified_error,
            match_conditions=match_conditions_cls,
            status_index=status_index,
            status_index_offset_cap=status_index_offset_cap,
            status_index_state_ttl_seconds=status_index_state_ttl_seconds,
        )

    @classmethod
    def from_table_client(
        cls,
        table_client: _TableClientProtocol,
        *,
        status_index: bool = False,
        status_index_offset_cap: int = DEFAULT_OFFSET_CAP,
        status_index_state_ttl_seconds: float = DEFAULT_STATE_TTL_SECONDS,
    ) -> AzureTableThreadStore:
        """Wrap a pre-built ``azure.data.tables.TableClient``.

        Use this when you build the ``TableClient`` yourself — for
        example with ``DefaultAzureCredential`` for Managed Identity
        deployments — so application code does not need to import or
        know about ``azure.core.exceptions`` or ``MatchConditions``.

        The target table must already exist; this factory does not
        attempt to create it. Pre-create the table out-of-band (for
        example with ``TableClient.create_table()`` or via your
        deployment pipeline) before wiring it into the store.

        Example
        -------
        ::

            from azure.data.tables import TableClient
            from azure.identity import DefaultAzureCredential

            client = TableClient(
                endpoint="https://<account>.table.core.windows.net",
                table_name="threads",
                credential=DefaultAzureCredential(),
            )
            store = AzureTableThreadStore.from_table_client(client)

        Pass ``status_index=True`` to opt in to the time-ordered status index (see the class
        docstring); the default keeps the table layout and every request unchanged.
        """
        not_found_error, modified_error, match_conditions_cls = cls._load_azure_core_symbols()
        return cls(
            table_client=table_client,
            not_found_error=not_found_error,
            modified_error=modified_error,
            match_conditions=match_conditions_cls,
            status_index=status_index,
            status_index_offset_cap=status_index_offset_cap,
            status_index_state_ttl_seconds=status_index_state_ttl_seconds,
        )

    @staticmethod
    def _load_azure_core_symbols() -> tuple[type[BaseException], type[BaseException], Any]:
        # Loads only the azure.core symbols the store needs at runtime
        # (exception classes + MatchConditions). Deliberately does NOT
        # import azure.data.tables.TableClient so that from_table_client()
        # can decouple application code from the TableClient import path
        # (e.g. DefaultAzureCredential users who construct TableClient
        # themselves).
        try:
            exceptions_module = importlib.import_module("azure.core.exceptions")
        except ImportError as exc:
            raise ImportError(
                "AzureTableThreadStore requires optional dependency 'azure-core'. "
                "Install with: pip install azure-functions-langgraph[azure-table]"
            ) from exc

        resource_not_found_error = getattr(exceptions_module, "ResourceNotFoundError", None)
        if resource_not_found_error is None:
            raise ImportError(
                "azure.core.exceptions.ResourceNotFoundError not found. "
                "Install with: pip install azure-functions-langgraph[azure-table]"
            )
        resource_modified_error = getattr(exceptions_module, "ResourceModifiedError", None)
        if resource_modified_error is None:
            raise ImportError(
                "azure.core.exceptions.ResourceModifiedError not found. "
                "Install with: pip install azure-functions-langgraph[azure-table]"
            )

        azure_core_module = importlib.import_module("azure.core")
        match_conditions_cls = getattr(azure_core_module, "MatchConditions", None)
        if match_conditions_cls is None:
            raise ImportError(
                "azure.core.MatchConditions not found. "
                "Install with: pip install azure-functions-langgraph[azure-table]"
            )

        return (
            cast(type[BaseException], resource_not_found_error),
            cast(type[BaseException], resource_modified_error),
            match_conditions_cls,
        )

    @staticmethod
    def _load_azure_sdk_symbols() -> tuple[Any, type[BaseException], type[BaseException], Any]:
        try:
            tables_module = importlib.import_module("azure.data.tables")
        except ImportError as exc:
            raise ImportError(
                "AzureTableThreadStore requires optional dependency 'azure-data-tables'. "
                "Install with: pip install azure-functions-langgraph[azure-table]"
            ) from exc

        table_client_class = getattr(tables_module, "TableClient", None)
        if table_client_class is None:
            raise ImportError(
                "azure.data.tables.TableClient not found. "
                "Install with: pip install azure-functions-langgraph[azure-table]"
            )

        not_found_error, modified_error, match_conditions_cls = (
            AzureTableThreadStore._load_azure_core_symbols()
        )
        return (
            table_client_class,
            not_found_error,
            modified_error,
            match_conditions_cls,
        )

    @staticmethod
    def _partition_key() -> str:
        return "thread"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _warn_entity_size(self, entity: dict[str, Any], thread_id: str) -> None:
        serialized = json.dumps(entity, default=str)
        size_bytes = len(serialized.encode("utf-8"))
        if size_bytes >= _ENTITY_SIZE_WARN_THRESHOLD:
            logger.warning(
                "Azure Table entity for thread %s is %s bytes (close to %s byte limit)",
                thread_id,
                size_bytes,
                _ENTITY_SIZE_LIMIT_BYTES,
            )

    def _not_found_exception(self) -> type[BaseException]:
        if self._not_found_error is None:
            raise RuntimeError(
                "not_found_error is not configured; pass not_found_error to __init__ "
                "or create store with from_connection_string()"
            )
        return self._not_found_error

    def _thread_to_entity(
        self,
        thread_id: str,
        *,
        created_at: datetime,
        updated_at: datetime,
        metadata: Mapping[str, Any] | None,
        status: ThreadStatus,
        values: dict[str, Any] | None,
        assistant_id: str | None,
        interrupts: dict[str, list[Interrupt]],
    ) -> dict[str, Any]:
        entity: dict[str, Any] = {
            "PartitionKey": self._partition_key(),
            "RowKey": thread_id,
            "created_at": self._normalize_datetime(created_at),
            "updated_at": self._normalize_datetime(updated_at),
            "status": status,
        }
        write_json(entity, "interrupts_json", interrupts)
        if metadata is not None:
            write_json(entity, "metadata_json", dict(metadata))
        if values is not None:
            write_json(entity, "values_json", values)
        if assistant_id is not None:
            entity["assistant_id"] = assistant_id
        return entity

    def _entity_to_thread(self, entity: dict[str, Any]) -> Thread:
        metadata_value = read_json(entity, "metadata_json")
        values_value = read_json(entity, "values_json")
        interrupts_value = read_json(entity, "interrupts_json", {})
        lock_expires_at = entity.get("lock_expires_at")

        return Thread.model_validate(
            {
                "thread_id": str(entity["RowKey"]),
                "created_at": self._normalize_datetime(cast(datetime, entity["created_at"])),
                "updated_at": self._normalize_datetime(cast(datetime, entity["updated_at"])),
                "metadata": metadata_value,
                "status": cast(ThreadStatus, entity.get("status", "idle")),
                "values": values_value,
                "assistant_id": entity.get("assistant_id"),
                "interrupts": interrupts_value,
                "lock_token": entity.get("lock_token"),
                "lock_expires_at": (
                    self._normalize_datetime(lock_expires_at)
                    if lock_expires_at is not None
                    else None
                ),
            }
        )

    def _metadata_matches(self, thread: Thread, metadata: Mapping[str, Any] | None) -> bool:
        if metadata is None:
            return True
        if thread.metadata is None:
            return False
        return all(
            key in thread.metadata and thread.metadata[key] == value
            for key, value in metadata.items()
        )

    def _hint_status_change(self, current: dict[str, Any], replacement: dict[str, Any]) -> None:
        """Write-ahead hint: upsert the index row of the status ``replacement`` is about to set.

        Runs before the primary compare-and-swap and is idempotent, so a retry after a lost ETag
        race writes it again harmlessly; if the CAS then loses for good the hint is merely stale.
        Does nothing when the index is off, the status does not change or it is not indexed.
        """
        index = self._status_index
        if index is None:
            return
        target = replacement.get("status")
        if target == current.get("status") or not isinstance(target, str):
            return
        index.upsert_hint(target, str(replacement["RowKey"]), replacement["created_at"])

    def _require_index(self) -> StatusIndex:
        if self._status_index is None:
            raise RuntimeError("the status index is not enabled; construct with status_index=True")
        return self._status_index

    def rebuild_status_index(
        self, *, resume_after: str | None = None, max_threads: int | None = None
    ) -> StatusIndexBuildResult:
        """Backfill hints for every indexable thread and set the marker to ``building``.

        Idempotent and resumable: hints are upserted in per-partition entity group transactions
        of at most 100, re-running is safe, and ``resume_after`` (the previous result's
        ``last_row_key``) with ``max_threads`` lets a Timer Trigger work in bounded slices.
        Readers keep scanning until :meth:`audit_status_index` ``(mark_ready=True)`` succeeds.
        Calling it on a ``ready`` index demotes it to ``building``.
        """
        return self._require_index().rebuild(resume_after=resume_after, max_threads=max_threads)

    def audit_status_index(self, *, mark_ready: bool = False) -> StatusIndexAuditReport:
        """Compare primary rows with hints; optionally set the marker to ``ready`` if clean.

        ``missing`` hints (a thread whose status is indexed but that has no hint) block
        readiness; this is how a thread created by an index-unaware writer is detected. Stale
        hints are only counted. With ``mark_ready=True`` an unclean audit never leaves the marker
        ``ready`` (a ``ready`` marker is demoted to ``building``). Memory is proportional to the
        number of threads; run it from an admin tool or Timer Trigger.
        """
        return self._require_index().audit(mark_ready=mark_ready)

    def repair_status_index(self, grace_seconds: float = DEFAULT_REPAIR_GRACE_SECONDS) -> int:
        """Delete stale hints (deleted thread, other status) and return how many were deleted.

        Only hints whose service ``Timestamp`` is older than ``grace_seconds`` (measured against
        the table service's clock, not this host's) are examined, and each is deleted with an ETag
        condition, so a concurrent writer's fresh hint always wins. It cannot add a missing hint;
        use :meth:`rebuild_status_index` for that.
        """
        return self._require_index().repair(grace_seconds)

    def status_index_state(self) -> StatusIndexState:
        """Read the marker now (uncached): ``absent``, ``building``, ``ready`` or ``disabled``."""
        return self._require_index().state(refresh=True)

    def disable_status_index(self, *, delete_rows: bool = False) -> int:
        """Set the marker to ``disabled`` (readers scan) and optionally delete every hint row.

        Returns the number of hint rows deleted (a row that vanished concurrently may be
        counted). Re-enabling requires :meth:`rebuild_status_index` and a clean
        :meth:`audit_status_index`, because writes made while the index was off are not hinted.
        """
        return self._require_index().disable(delete_rows=delete_rows)

    def _query_entities(self, *, status: ThreadStatus | None) -> list[Thread]:
        pk = self._partition_key().replace("'", "''")
        query_filter = f"PartitionKey eq '{pk}'"
        if status is not None:
            escaped_status = status.replace("'", "''")
            query_filter += f" and status eq '{escaped_status}'"

        entities = self._table_client.query_entities(query_filter=query_filter)
        return [self._entity_to_thread(entity) for entity in entities]

    def create(self, *, metadata: Mapping[str, Any] | None = None) -> Thread:
        thread_id = str(uuid.uuid4())
        now = self._now()
        thread = Thread(
            thread_id=thread_id,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata) if metadata is not None else None,
            status="idle",
            interrupts={},
        )
        entity = self._thread_to_entity(
            thread_id,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            metadata=thread.metadata,
            status=thread.status,
            values=thread.values,
            assistant_id=thread.assistant_id,
            interrupts=thread.interrupts,
        )
        self._warn_entity_size(entity, thread_id)
        validate_entity(entity)
        if self._status_index is not None:
            self._status_index.upsert_hint("idle", thread_id, thread.created_at)
        self._table_client.create_entity(entity)
        return self._entity_to_thread(entity)

    def get(self, thread_id: str) -> Thread | None:
        not_found_error = self._not_found_exception()
        try:
            entity = self._table_client.get_entity(
                partition_key=self._partition_key(),
                row_key=thread_id,
            )
        except not_found_error:
            return None
        return self._entity_to_thread(entity)

    def update(
        self,
        thread_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
        values: dict[str, Any] | None = None,
        interrupts: dict[str, list[Interrupt]] | None = None,
        assistant_id: str | None = None,
    ) -> Thread:

        def apply(replacement: dict[str, Any]) -> None:
            for field, value in (
                ("metadata_json", metadata),
                ("values_json", values),
                ("interrupts_json", interrupts),
            ):
                if value is not None:
                    write_json(replacement, field, value)
            if status is not None:
                replacement["status"] = status
            if assistant_id is not None:
                replacement["assistant_id"] = assistant_id

        return self._read_modify_write(thread_id, apply)

    def merge_metadata(self, thread_id: str, metadata: Mapping[str, Any]) -> Thread:
        """Merge ``metadata`` into the thread's current metadata with ETag compare-and-swap.

        Unlike ``update(metadata=...)`` with a map the caller merged earlier, the merge is
        computed against the row as it stands at **every** attempt: when a concurrent write
        changes the ETag, the retry reads the fresh metadata and merges the patch into it again,
        so a key that other writer added is kept. For one key the last writer wins. Everything
        else in the row (status, assistant binding, values, interrupts, lock token and expiry)
        is preserved. After three conflicting attempts the SDK's modified error is raised, as for
        :meth:`update`. This method has its own read-modify-write loop and does not call
        :meth:`update`: a subclass that overrides ``update`` must override this method as well.
        """

        def apply(replacement: dict[str, Any]) -> None:
            current = read_json(replacement, "metadata_json")
            merged = {**(current if isinstance(current, dict) else {}), **dict(metadata)}
            write_json(replacement, "metadata_json", merged)

        return self._read_modify_write(thread_id, apply)

    def _read_modify_write(self, thread_id: str, apply: Callable[[dict[str, Any]], None]) -> Thread:
        """Read the row, let ``apply`` change a copy, write it back under its ETag; retry.

        ``apply`` runs again on a fresh read after every ETag conflict (three attempts in all),
        so a change that depends on the current row content is re-derived, never re-applied from
        a stale snapshot.
        """
        not_found_error = self._not_found_exception()
        for attempt in range(3):
            try:
                entity = self._table_client.get_entity(
                    partition_key=self._partition_key(), row_key=thread_id
                )
                entity_metadata = getattr(entity, "metadata", {})
                etag = entity_metadata.get("etag") or entity.get("etag")
                replacement = {
                    key: value for key, value in entity.items() if key not in {"etag", "Timestamp"}
                }
                replacement["updated_at"] = self._now()
                apply(replacement)
                validate_entity(replacement)
                self._hint_status_change(entity, replacement)
                self._table_client.update_entity(
                    replacement,
                    mode="replace",
                    etag=etag,
                    match_condition=self._match_conditions.IfNotModified,
                )
            except self._modified_error:
                if attempt == 2:
                    raise
                continue
            except not_found_error as exc:
                raise KeyError(thread_id) from exc
            return self._entity_to_thread(replacement)
        raise AssertionError("unreachable update retry exit")

    def delete(self, thread_id: str) -> None:
        not_found_error = self._not_found_exception()
        try:
            self._table_client.delete_entity(
                partition_key=self._partition_key(),
                row_key=thread_id,
            )
        except not_found_error as exc:
            raise KeyError(thread_id) from exc

    def try_acquire_run_lock(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
    ) -> Thread | None:
        """Atomically acquire the per-thread run lock with ETag CAS.

        Lock acquisition is **atomic**: the update uses an ETag
        compare-and-swap so that exactly one concurrent caller wins.
        ``updated_at`` is updated on lock acquire and used for staleness
        detection while status is ``busy`` (see :meth:`reset_stale_locks`).

        If the Function host is terminated during graph execution, the
        thread may remain in ``busy`` status indefinitely.  Use
        :meth:`reset_stale_locks` from a periodic Timer Trigger to
        reclaim such threads.
        """
        not_found_error = self._not_found_exception()
        modified_error = self._modified_error
        match_conditions = self._match_conditions

        max_attempts = 3
        last_exc: BaseException | None = None
        for _attempt in range(max_attempts):
            try:
                entity = self._table_client.get_entity(
                    partition_key=self._partition_key(),
                    row_key=thread_id,
                )
            except not_found_error:
                raise KeyError(thread_id)

            thread = self._entity_to_thread(entity)
            if (
                thread.assistant_id is not None
                and assistant_id is not None
                and thread.assistant_id != assistant_id
            ):
                raise ValueError(
                    f"Thread {thread_id!r} is bound to assistant "
                    f"{thread.assistant_id!r}, cannot run with {assistant_id!r}"
                )
            if thread.status == "busy":
                return None

            entity_metadata = getattr(entity, "metadata", None)
            etag = entity_metadata.get("etag") if isinstance(entity_metadata, Mapping) else None
            if etag is None:
                etag = entity.get("etag") if isinstance(entity, dict) else None

            now = self._now()
            owner_token = uuid.uuid4().hex
            expires_at = now + timedelta(seconds=60)
            patch: dict[str, Any] = {
                "PartitionKey": self._partition_key(),
                "RowKey": thread_id,
                "status": "busy",
                "updated_at": now,
                "lock_token": owner_token,
                "lock_expires_at": expires_at,
            }
            if thread.assistant_id is None and assistant_id is not None:
                patch["assistant_id"] = assistant_id

            validate_entity({**entity, **patch})
            try:
                self._table_client.update_entity(
                    patch,
                    mode="merge",
                    etag=etag,
                    match_condition=match_conditions.IfNotModified,
                )
            except modified_error as exc:
                last_exc = exc
                continue
            except not_found_error:
                raise KeyError(thread_id)

            new_data = thread.model_dump()
            new_data["status"] = "busy"
            new_data["updated_at"] = now
            new_data["lock_token"] = owner_token
            new_data["lock_expires_at"] = expires_at
            if thread.assistant_id is None and assistant_id is not None:
                new_data["assistant_id"] = assistant_id
            return Thread.model_validate(new_data)

        logger.warning(
            "try_acquire_run_lock for thread %s exhausted %d retries (last: %s)",
            thread_id,
            max_attempts,
            last_exc,
        )
        return None

    def release_run_lock(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> Thread:
        """Release the per-thread run lock with owner fencing and ETag CAS.

        Before writing, this checks *two* things:

        1. **Owner token** — if ``owner_token`` is supplied and does not match
           the entity's current ``lock_token``, **or the thread is no longer
           ``busy``**, the release is ignored (a warning is logged) and the
           current entity is returned unchanged. This rejects a stale release
           from an execution whose lock has since been reclaimed by
           :meth:`reset_stale_locks` or re-acquired by a newer run. The
           ``busy`` check also covers a row that an earlier release of this
           library reclaimed without clearing its token.
        2. **ETag compare-and-swap** — the replacement is sent with
           ``match_condition=IfNotModified`` against the ETag read just
           before the write. If a concurrent write (e.g. another owner-token
           check, or a racing :meth:`reset_stale_locks` scan) changed the
            entity first, retry with a fresh read and owner check, up to three
            attempts. Exhaustion raises :class:`RunLockReleaseError`.

        A successful replacement preserves the latest entity properties except
        the obsolete ``lock_token`` and ``lock_expires_at`` properties, which
        are removed atomically with the terminal status and optional values.

        Crash recovery is a separate concern from this ownership fencing: if
        the Function host is killed mid-execution, *no* release call ever
        fires — there is no owner-token or CAS check to lose, because
        ``release_run_lock`` is simply never invoked. The thread remains
        ``busy`` until :meth:`reset_stale_locks` reclaims it from a periodic
        sweep.
        """
        if status == "busy":
            raise ValueError("release_run_lock cannot set status to 'busy'")
        not_found_error = self._not_found_exception()
        last_conflict: BaseException | None = None
        for _attempt in range(3):
            try:
                entity = self._table_client.get_entity(
                    partition_key=self._partition_key(), row_key=thread_id
                )
            except not_found_error as exc:
                raise KeyError(thread_id) from exc
            if owner_token is not None and (
                entity.get("lock_token") != owner_token or entity.get("status") != "busy"
            ):
                logger.warning("Stale run-lock release ignored for thread %s", thread_id)
                self._release_signal.record(applied=False)
                return self._entity_to_thread(entity)
            metadata = getattr(entity, "metadata", None)
            etag = metadata.get("etag") if isinstance(metadata, Mapping) else None
            if etag is None:
                etag = entity.get("etag")
            replacement = {
                key: value
                for key, value in entity.items()
                if key not in {"lock_token", "lock_expires_at", "etag", "Timestamp"}
            }
            replacement.update(status=status, updated_at=self._now())
            if values is not None:
                write_json(replacement, "values_json", values)
            self._warn_entity_size(replacement, thread_id)
            validate_entity(replacement)
            self._hint_status_change(entity, replacement)
            try:
                self._table_client.update_entity(
                    replacement,
                    mode="replace",
                    etag=etag,
                    match_condition=self._match_conditions.IfNotModified,
                )
            except self._modified_error as exc:
                last_conflict = exc
                continue
            except not_found_error as exc:
                raise KeyError(thread_id) from exc
            self._release_signal.record(applied=True)
            return self._entity_to_thread(replacement)
        raise RunLockReleaseError(thread_id) from last_conflict

    def release_run_lock_outcome(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> RunLockReleaseOutcome:
        """Release exactly like :meth:`release_run_lock` and report whether it was applied.

        ``applied`` is false when the owner token no longer owns a ``busy`` thread (the
        stale no-op described there) and true when this call replaced the row. Unlike the
        returned thread alone, it stays exact when another owner already finished and left
        the thread at the requested status.
        """
        return _release_with_outcome(
            lambda: self.release_run_lock(
                thread_id, status=status, values=values, owner_token=owner_token
            ),
            self._release_signal,
        )

    def renew_run_lock(self, thread_id: str, owner_token: str) -> bool:
        """Extend a held run lock, retrying ETag conflicts while the caller still owns it.

        A metadata-only write (for example ``PATCH /threads/{id}``) changes the row's ETag
        without touching its owner, so a conflict on the conditional write is not proof that
        ownership changed. Each attempt re-reads the row and re-checks that ``owner_token``
        still owns a ``busy`` thread before writing again, for at most three attempts:

        * the token was replaced, or the thread is no longer ``busy`` (released, reclaimed
          by :meth:`reset_stale_locks`, re-acquired): returns ``False`` and writes nothing,
          so a newer owner's record is never overwritten;
        * the conditional write succeeded: returns ``True``;
        * every attempt still lost the race while the caller kept ownership: raises
          :class:`RunLockRenewalConflictError`. The lock was neither shown to be lost nor
          extended, and the Platform renewal worker reports it as a failed renewal.

        Storage errors (a missing thread, a transport fault) propagate unchanged and are not
        mistaken for lost ownership.
        """
        last_conflict: BaseException | None = None
        for _attempt in range(3):
            entity = self._table_client.get_entity(
                partition_key=self._partition_key(), row_key=thread_id
            )
            if entity.get("lock_token") != owner_token or entity.get("status") != "busy":
                return False
            metadata = getattr(entity, "metadata", None)
            etag = metadata.get("etag") if isinstance(metadata, Mapping) else entity.get("etag")
            now = self._now()
            try:
                self._table_client.update_entity(
                    {
                        "PartitionKey": self._partition_key(),
                        "RowKey": thread_id,
                        "updated_at": now,
                        "lock_expires_at": now + timedelta(seconds=60),
                    },
                    mode="merge",
                    etag=etag,
                    match_condition=self._match_conditions.IfNotModified,
                )
            except self._modified_error as exc:
                last_conflict = exc
                continue
            return True
        raise RunLockRenewalConflictError(thread_id) from last_conflict

    def reset_stale_locks(
        self,
        older_than_seconds: int,
        status: Literal["idle", "error"] = "error",
    ) -> int:
        """Reset busy threads whose lock is older than *older_than_seconds*.

        Scans threads in ``busy`` status and conditionally resets those
        whose ``updated_at`` (set by :meth:`try_acquire_run_lock` and by
        every :meth:`renew_run_lock`) is older than the threshold.

        Reclaiming **revokes the previous owner**: the row is replaced
        without its ``lock_token`` and ``lock_expires_at``, atomically with the
        new status. A worker that comes back after the reset therefore fails
        the owner-token check in :meth:`release_run_lock` and
        :meth:`renew_run_lock`, and cannot undo the recovery disposition or
        publish stale ``values``.

        The scan's projected rows only prove that a thread *was* stale, so each
        candidate is re-read in full and re-verified (still ``busy``, still older
        than the threshold) before the write. The replacement uses **ETag CAS**
        against that fresh read, so a thread that was renewed, released or
        re-acquired since is never stomped.

        This does not stop the old graph and does not fence Blob checkpoint
        writes; see the production guide.

        Args:
            older_than_seconds: Minimum age in seconds for a busy thread
                to be considered stale.
            status: Terminal status to assign.  Must be ``"idle"`` or
                ``"error"`` (default ``"error"``).

        Returns:
            Number of threads successfully reset.

        Raises:
            ValueError: If *older_than_seconds* is negative or *status*
                is not ``"idle"`` or ``"error"``.
        Note:
            Each re-read row must expose an ETag via either
            ``entity.metadata["etag"]`` or ``entity["etag"]``.
            Rows where neither shape populates an ETag are skipped
            (logged at DEBUG) so a stale lock is never reset without
            a writable ETag for CAS; such rows are retried on the next
            scan once the SDK returns a usable ETag.
        """
        if older_than_seconds < 0:
            raise ValueError(f"older_than_seconds must be non-negative, got {older_than_seconds}")
        if status not in ("idle", "error"):
            raise ValueError(f"status must be 'idle' or 'error', got {status!r}")

        cutoff = self._now() - timedelta(seconds=older_than_seconds)
        modified_error = self._modified_error
        not_found_error = self._not_found_exception()
        match_conditions = self._match_conditions

        pk = self._partition_key().replace("'", "''")
        query_filter = f"PartitionKey eq '{pk}' and status eq 'busy'"
        entities = self._table_client.query_entities(
            query_filter=query_filter,
            select=["RowKey", "updated_at"],
        )

        reset_count = 0
        for candidate in entities:
            if not self._is_older_than(candidate.get("updated_at"), cutoff):
                continue
            row_key = str(candidate["RowKey"])

            # The projection can be stale by now: re-read the full row and decide again.
            try:
                entity = self._table_client.get_entity(
                    partition_key=self._partition_key(), row_key=row_key
                )
            except not_found_error:
                logger.debug("Skipped stale lock reset for thread %s (deleted)", row_key)
                continue
            if entity.get("status") != "busy" or not self._is_older_than(
                entity.get("updated_at"), cutoff
            ):
                logger.debug(
                    "Skipped stale lock reset for thread %s (released or renewed since the scan)",
                    row_key,
                )
                continue

            entity_metadata = getattr(entity, "metadata", None)
            etag = entity_metadata.get("etag") if isinstance(entity_metadata, Mapping) else None
            if etag is None:
                etag = entity.get("etag")
            if etag is None:
                # Without ETag, CAS update cannot guarantee safety — skip.
                logger.debug("Skipping stale thread %s: no ETag available", row_key)
                continue

            replacement = {
                key: value
                for key, value in entity.items()
                if key not in {"lock_token", "lock_expires_at", "etag", "Timestamp"}
            }
            replacement.update(status=status, updated_at=self._now())
            validate_entity(replacement)
            self._hint_status_change(entity, replacement)

            try:
                self._table_client.update_entity(
                    replacement,
                    mode="replace",
                    etag=etag,
                    match_condition=match_conditions.IfNotModified,
                )
                reset_count += 1
            except modified_error:
                # Thread was renewed, released or re-acquired since our read —
                # skip it rather than stomping a legitimate lock.
                logger.debug(
                    "Skipped stale lock reset for thread %s (ETag mismatch)",
                    row_key,
                )
                continue
            except not_found_error:
                # Thread was deleted between read and update — skip.
                logger.debug(
                    "Skipped stale lock reset for thread %s (deleted)",
                    row_key,
                )
                continue

        return reset_count

    def _is_older_than(self, updated_at: object, cutoff: datetime) -> bool:
        """Whether *updated_at* is a timestamp strictly before *cutoff* (else not stale)."""
        if not isinstance(updated_at, datetime):
            return False
        return self._normalize_datetime(updated_at) < cutoff

    def search(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Thread]:
        if limit < 0:
            raise ValueError(f"limit must be non-negative, got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")

        if self._status_index is not None and metadata is None and status is not None:
            indexed = self._status_index.search(status, limit, offset)
            if indexed is not None:
                return [self._entity_to_thread(entity) for entity in indexed]

        threads = self._query_entities(status=status)
        filtered = [thread for thread in threads if self._metadata_matches(thread, metadata)]
        filtered.sort(key=lambda thread: thread.created_at, reverse=True)
        return filtered[offset : offset + limit]

    def count(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
    ) -> int:
        threads = self._query_entities(status=status)
        return sum(1 for thread in threads if self._metadata_matches(thread, metadata))


__all__ = [
    "AzureTableThreadStore",
    "StatusIndexAuditReport",
    "StatusIndexBuildResult",
    "StatusIndexState",
]
