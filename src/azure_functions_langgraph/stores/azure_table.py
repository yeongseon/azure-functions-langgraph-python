"""Azure Table Storage-backed thread store for LangGraph Platform API.

.. versionadded:: 0.4.0
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import json
import logging
from typing import Any, Literal, Mapping, Protocol, cast
import uuid

from ..platform.contracts import Interrupt, Thread, ThreadStatus
from ..platform.stores import ThreadStore

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
    compare-and-swap and best-effort lock release via merge updates.
    """

    def __init__(
        self,
        *,
        table_client: _TableClientProtocol,
        not_found_error: type[BaseException],
        modified_error: type[BaseException],
        match_conditions: Any,
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

    @classmethod
    def from_connection_string(
        cls,
        connection_string: str,
        table_name: str,
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
        )

    @classmethod
    def from_table_client(cls, table_client: _TableClientProtocol) -> AzureTableThreadStore:
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
        """
        not_found_error, modified_error, match_conditions_cls = cls._load_azure_core_symbols()
        return cls(
            table_client=table_client,
            not_found_error=not_found_error,
            modified_error=modified_error,
            match_conditions=match_conditions_cls,
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
        return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _json_default(value: Any) -> Any:
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return dump()
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

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
            "interrupts_json": json.dumps(interrupts, default=self._json_default),
        }
        if metadata is not None:
            entity["metadata_json"] = json.dumps(dict(metadata), default=self._json_default)
        if values is not None:
            entity["values_json"] = json.dumps(values, default=self._json_default)
        if assistant_id is not None:
            entity["assistant_id"] = assistant_id
        return entity

    def _entity_to_thread(self, entity: dict[str, Any]) -> Thread:
        metadata_json = entity.get("metadata_json")
        values_json = entity.get("values_json")
        interrupts_json = entity.get("interrupts_json")

        metadata_value = None if metadata_json is None else json.loads(metadata_json)
        values_value = None if values_json is None else json.loads(values_json)
        interrupts_value = {}
        if interrupts_json is not None:
            interrupts_value = json.loads(interrupts_json)

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
        not_found_error = self._not_found_exception()
        patch: dict[str, Any] = {
            "PartitionKey": self._partition_key(),
            "RowKey": thread_id,
            "updated_at": self._now(),
        }

        if metadata is not None:
            patch["metadata_json"] = json.dumps(dict(metadata), default=self._json_default)
        if status is not None:
            patch["status"] = status
        if values is not None:
            patch["values_json"] = json.dumps(values, default=self._json_default)
        if interrupts is not None:
            patch["interrupts_json"] = json.dumps(interrupts, default=self._json_default)
        if assistant_id is not None:
            patch["assistant_id"] = assistant_id

        self._warn_entity_size(patch, thread_id)
        try:
            self._table_client.update_entity(patch, mode="merge")
        except not_found_error as exc:
            raise KeyError(thread_id) from exc

        # Re-read the merged entity to return accurate state
        merged = self._table_client.get_entity(
            partition_key=self._partition_key(),
            row_key=thread_id,
        )
        return self._entity_to_thread(merged)

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
            patch: dict[str, Any] = {
                "PartitionKey": self._partition_key(),
                "RowKey": thread_id,
                "status": "busy",
                "updated_at": now,
            }
            if thread.assistant_id is None and assistant_id is not None:
                patch["assistant_id"] = assistant_id

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
    ) -> Thread:
        """Release the per-thread run lock without ETag concurrency.

        Lock release is **best-effort**: the merge update does *not*
        use ETag concurrency because failing to release a lock is
        operationally worse than a rare race.  If the Function host
        is killed mid-execution, the lock is *not* released and the
        thread remains ``busy`` until :meth:`reset_stale_locks` reclaims
        it.
        """
        if status == "busy":
            raise ValueError("release_run_lock cannot set status to 'busy'")
        not_found_error = self._not_found_exception()
        patch: dict[str, Any] = {
            "PartitionKey": self._partition_key(),
            "RowKey": thread_id,
            "status": status,
            "updated_at": self._now(),
        }
        if values is not None:
            patch["values_json"] = json.dumps(values, default=self._json_default)
        self._warn_entity_size(patch, thread_id)
        try:
            self._table_client.update_entity(patch, mode="merge")
        except not_found_error as exc:
            raise KeyError(thread_id) from exc
        merged = self._table_client.get_entity(
            partition_key=self._partition_key(),
            row_key=thread_id,
        )
        return self._entity_to_thread(merged)

    def reset_stale_locks(
        self,
        older_than_seconds: int,
        status: Literal["idle", "error"] = "error",
    ) -> int:
        """Reset busy threads whose lock is older than *older_than_seconds*.

        Scans threads in ``busy`` status and conditionally resets those
        whose ``updated_at`` (set by :meth:`try_acquire_run_lock`) is older
        than the threshold.  Each reset uses **ETag CAS** so a thread that
        has been legitimately re-acquired since the query is never stomped.

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
        """
        if older_than_seconds < 0:
            raise ValueError(
                f"older_than_seconds must be non-negative, got {older_than_seconds}"
            )
        if status not in ("idle", "error"):
            raise ValueError(
                f"status must be 'idle' or 'error', got {status!r}"
            )

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
        for entity in entities:
            updated_at = entity.get("updated_at")
            if updated_at is None:
                continue
            if isinstance(updated_at, datetime):
                normalized = self._normalize_datetime(updated_at)
            else:
                continue
            if normalized >= cutoff:
                continue

            # Extract ETag for CAS
            entity_metadata = getattr(entity, "metadata", None)
            etag = (
                entity_metadata.get("etag")
                if isinstance(entity_metadata, Mapping)
                else None
            )
            if etag is None:
                etag = entity.get("etag") if isinstance(entity, dict) else None
            if etag is None:
                # Without ETag, CAS update cannot guarantee safety — skip.
                logger.debug(
                    "Skipping stale thread %s: no ETag available",
                    entity.get("RowKey"),
                )
                continue

            row_key = str(entity["RowKey"])
            patch: dict[str, Any] = {
                "PartitionKey": self._partition_key(),
                "RowKey": row_key,
                "status": status,
                "updated_at": self._now(),
            }

            try:
                self._table_client.update_entity(
                    patch,
                    mode="merge",
                    etag=etag,
                    match_condition=match_conditions.IfNotModified,
                )
                reset_count += 1
            except modified_error:
                # Thread was re-acquired or modified since our query —
                # skip it rather than stomping a legitimate lock.
                logger.debug(
                    "Skipped stale lock reset for thread %s (ETag mismatch)",
                    row_key,
                )
                continue
            except not_found_error:
                # Thread was deleted between query and update — skip.
                logger.debug(
                    "Skipped stale lock reset for thread %s (deleted)",
                    row_key,
                )
                continue

        return reset_count

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


__all__ = ["AzureTableThreadStore"]
