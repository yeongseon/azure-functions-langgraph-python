"""Azure Blob Storage-backed checkpoint saver for LangGraph."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import importlib
import json
import logging
import random
from typing import Any, Protocol, TypeVar, cast
from urllib.parse import quote, unquote

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol

from azure_functions_langgraph.locks.guard import (
    OwnershipGuard,
    ThreadLeaseLostError,
    check_ownership_guard,
    resolve_ownership_guard,
)

_LATEST_HINT_VERSION = 1
"""The only ``latest.json`` hint ``version`` this release reads and writes."""


class BlobCheckpointFormatError(RuntimeError):
    """A stored object has a format version this release does not understand.

    Raised by writers instead of replacing data they cannot interpret. Nothing
    has been written when it is raised. The message never contains blob content.
    """


def _is_known_hint_version(value: object) -> bool:
    # ``True == 1`` in Python, so require a real ``int``.
    return type(value) is int and value == _LATEST_HINT_VERSION


def _describe_unknown_version(value: object) -> str:
    if type(value) is int and abs(value) < 1_000_000:
        return f"version {value}"
    return "an unrecognized version"


def _downloader_metadata(downloader: object) -> dict[str, str] | None:
    """Return the blob metadata carried by a download response, or ``None`` if absent.

    ``None`` (a downloader without ``properties`` or without a ``metadata`` mapping, for
    example a duck-typed client) makes the caller fall back to a separate properties
    request. A real, empty mapping is a valid answer: the blob has no metadata.
    """
    metadata = getattr(getattr(downloader, "properties", None), "metadata", None)
    return dict(metadata) if isinstance(metadata, dict) else None


_MAX_CONCURRENT_BLOB_READS = 8
"""Upper bound on in-flight blob downloads for one checkpoint read."""

_T = TypeVar("_T")
_R = TypeVar("_R")


def _map_bounded(func: Callable[[_T], _R], items: Sequence[_T]) -> builtins.list[_R]:
    """Apply ``func`` to ``items`` with bounded thread concurrency, preserving order."""
    if len(items) <= 1:
        return [func(item) for item in items]
    workers = min(_MAX_CONCURRENT_BLOB_READS, len(items))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(func, items))


async def _amap_bounded(
    func: Callable[[_T], Awaitable[_R]], items: Sequence[_T]
) -> builtins.list[_R]:
    """Await ``func`` over ``items`` with a bounded semaphore, preserving order.

    If any call fails or the caller is cancelled, all sibling tasks are
    cancelled and awaited before the exception propagates.
    """
    if len(items) <= 1:
        return [await func(item) for item in items]
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BLOB_READS)

    async def run(item: _T) -> _R:
        async with semaphore:
            return await func(item)

    tasks = [asyncio.ensure_future(run(item)) for item in items]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


@dataclass
class OrphanedValueCollectionResult:
    """Result of :meth:`AzureBlobCheckpointSaver.collect_orphaned_values`.

    Attributes
    ----------
    dry_run:
        Whether the caller requested a dry run. When ``True`` the helper
        does not delete anything; ``would_delete`` lists every blob path
        that *would* have been removed in a non-dry-run pass over the
        same store snapshot.
    would_delete:
        Channel value blob paths the helper identified as orphaned. In a
        dry run this is the only populated list. In a real run this
        captures the candidates *before* the per-blob re-check: a blob
        listed here may still be skipped (and absent from ``deleted``)
        if a concurrent writer references it mid-collection.
        Blobs deferred by the recent-write grace window are **not**
        listed here; they appear in ``skipped_recent`` instead.
    deleted:
        Channel value blob paths actually deleted. Only populated when
        ``dry_run`` is ``False``. Always a subset of ``would_delete``.
    skipped_namespaces:
        ``(thread_id, checkpoint_ns)`` pairs whose survivor set could
        not be trusted — either ``latest.json`` could not be loaded, or
        at least one surviving checkpoint blob was unreadable / could
        not be deserialized. The helper skips such namespaces entirely
        to avoid deleting live data on a misconfigured or transiently
        unavailable store. Failure reasons are logged at WARNING level.
    skipped_recent:
        Channel value blob paths that were recent enough to fall inside
        the ``grace_period_seconds`` window (or that exposed no
        ``last_modified`` timestamp). These are deferred to a future GC
        pass so an in-flight checkpoint write that uploaded the value
        blob *before* finalizing its checkpoint commit marker is not
        deleted out from under it.
    """

    dry_run: bool
    would_delete: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped_namespaces: list[tuple[str, str]] = field(default_factory=list)
    skipped_recent: list[str] = field(default_factory=list)


class _BlobDownloadProtocol(Protocol):
    # ``StorageStreamDownloader.properties`` is a ``BlobProperties`` built from the
    # response headers of the same GET that carries the body, so its ``metadata`` costs no
    # extra request. Typed optional so duck-typed clients without it keep working.
    @property
    def properties(self) -> _BlobPropertiesProtocol | None: ...

    def readall(self) -> bytes: ...


class _BlobPropertiesProtocol(Protocol):
    metadata: dict[str, str] | None


class _BlobClientProtocol(Protocol):
    def upload_blob(self, data: bytes, metadata: dict[str, str], overwrite: bool) -> None: ...

    def download_blob(self) -> _BlobDownloadProtocol: ...

    def get_blob_properties(self) -> _BlobPropertiesProtocol: ...

    def delete_blob(self) -> None: ...


class _BlobItemProtocol(Protocol):
    name: str
    last_modified: datetime | None


class _ContainerClientProtocol(Protocol):
    def get_blob_client(self, blob: str) -> _BlobClientProtocol: ...

    def list_blobs(self, name_starts_with: str = "") -> Sequence[_BlobItemProtocol]: ...


class _AsyncBlobDownloadProtocol(Protocol):
    @property
    def properties(self) -> _BlobPropertiesProtocol | None: ...

    async def readall(self) -> bytes: ...


class _AsyncBlobClientProtocol(Protocol):
    async def upload_blob(self, data: bytes, metadata: dict[str, str], overwrite: bool) -> None: ...

    async def download_blob(self) -> _AsyncBlobDownloadProtocol: ...

    async def get_blob_properties(self) -> _BlobPropertiesProtocol: ...

    async def delete_blob(self) -> None: ...


class _AsyncContainerClientProtocol(Protocol):
    def get_blob_client(self, blob: str) -> _AsyncBlobClientProtocol: ...

    def list_blobs(self, name_starts_with: str = "") -> AsyncIterator[_BlobItemProtocol]: ...


logger = logging.getLogger(__name__)


class AzureBlobCheckpointSaver(BaseCheckpointSaver[str]):
    """Persist LangGraph checkpoints in Azure Blob Storage.

    Note:
        This checkpointer assumes single-writer-per-thread semantics.
        Concurrent writes to the same thread from multiple processes or
        Azure Functions instances may corrupt checkpoint data. See
        DESIGN.md decision #7 for details.

    Note:
        Writes are unconditional: nothing at the storage layer fences a stale
        writer. When a write is accompanied by an
        :class:`~azure_functions_langgraph.locks.guard.OwnershipGuard` (handed
        out by a lock created with ``guard_checkpoint_writes=True``, for
        example
        :class:`~azure_functions_langgraph.locks.azure_blob.AzureBlobLeaseThreadLock`),
        this saver checks it immediately before **every** blob upload in
        :meth:`put`, :meth:`aput`, :meth:`put_writes` and :meth:`aput_writes`
        and raises
        :class:`~azure_functions_langgraph.locks.guard.ThreadLeaseLostError`
        once ownership is detected as lost. That reduces writes after
        *locally detected* loss. It is not fencing: a check can pass an
        instant before an upload that is then accepted, and
        :meth:`delete_thread`, :meth:`adelete_thread`, the retention helpers
        and reads are never guarded.
    """

    def __init__(
        self,
        *,
        container_client: _ContainerClientProtocol,
        aio_container_client: _AsyncContainerClientProtocol | None = None,
        serde: SerializerProtocol | None = None,
        require_ownership_guard: bool = False,
    ) -> None:
        """Create a saver bound to an Azure Blob container client.

        Parameters
        ----------
        container_client:
            Synchronous ``azure.storage.blob.ContainerClient`` used by every
            synchronous method.
        aio_container_client:
            Optional asynchronous ``azure.storage.blob.aio.ContainerClient``.
            When provided, the async methods (:meth:`aget_tuple`,
            :meth:`alist`, :meth:`aput`, :meth:`aput_writes`) perform native
            non-blocking blob I/O through it. When omitted, those methods fall
            back to running the synchronous implementation in a thread executor
            and emit a one-time warning.
        serde:
            Optional serializer protocol; defaults to LangGraph's serializer.
        require_ownership_guard:
            When ``True`` the saver fails closed: any ``put`` / ``put_writes``
            (or async counterpart) that is not accompanied by an
            :class:`~azure_functions_langgraph.locks.guard.OwnershipGuard` is
            rejected with
            :class:`~azure_functions_langgraph.locks.guard.ThreadLeaseLostError`
            (``reason="missing"``) before any blob is written. Use it only
            when every writer is expected to hold a guard-capable lock:
            writes that legitimately carry no guard are rejected too, namely
            Service Bus ``session_exclusive=True`` runs (they take no lock),
            runs under a lock without the capability (for example
            ``InProcessThreadLock``), a lock created with
            ``guard_checkpoint_writes=False``, and direct maintenance writes.
            Defaults to ``False``: a write without a guard proceeds exactly as
            before.
        """
        try:
            azure_blob_module = importlib.import_module("azure.storage.blob")
        except ImportError as exc:
            raise ImportError(
                "AzureBlobCheckpointSaver requires optional dependency "
                "'azure-storage-blob'. Install with: "
                "pip install azure-functions-langgraph[azure-blob]"
            ) from exc

        azure_container_client = getattr(azure_blob_module, "ContainerClient", None)
        if azure_container_client is None or not isinstance(
            container_client, azure_container_client
        ):
            raise TypeError(
                "container_client must be an instance of azure.storage.blob.ContainerClient"
            )

        super().__init__(serde=serde)
        self._container_client: _ContainerClientProtocol = cast(
            _ContainerClientProtocol,
            container_client,
        )

        try:
            azure_core_exceptions = importlib.import_module("azure.core.exceptions")
            resource_not_found_error = getattr(azure_core_exceptions, "ResourceNotFoundError", None)
            if resource_not_found_error is None:
                raise ImportError("azure.core.exceptions.ResourceNotFoundError not found")
            resource_exists_error = getattr(azure_core_exceptions, "ResourceExistsError", None)
            if resource_exists_error is None:
                raise ImportError("azure.core.exceptions.ResourceExistsError not found")
        except ImportError as exc:
            raise ImportError(
                "AzureBlobCheckpointSaver requires 'azure-core'. "
                "Install with: pip install azure-functions-langgraph[azure-blob]"
            ) from exc
        self._not_found_error: type[BaseException] = cast(
            type[BaseException],
            resource_not_found_error,
        )
        self._exists_error: type[BaseException] = cast(
            type[BaseException],
            resource_exists_error,
        )

        if aio_container_client is not None:
            try:
                azure_blob_aio_module = importlib.import_module("azure.storage.blob.aio")
            except ImportError as exc:
                raise ImportError(
                    "AzureBlobCheckpointSaver async support requires optional "
                    "dependency 'azure-storage-blob'. Install with: "
                    "pip install azure-functions-langgraph[azure-blob]"
                ) from exc
            azure_aio_container_client = getattr(azure_blob_aio_module, "ContainerClient", None)
            if azure_aio_container_client is None or not isinstance(
                aio_container_client, azure_aio_container_client
            ):
                raise TypeError(
                    "aio_container_client must be an instance of "
                    "azure.storage.blob.aio.ContainerClient"
                )
        self._aio_container_client: _AsyncContainerClientProtocol | None = (
            cast(_AsyncContainerClientProtocol, aio_container_client)
            if aio_container_client is not None
            else None
        )
        self._sync_fallback_warned = False
        self._require_ownership_guard = require_ownership_guard

    def get_next_version(self, current: str | None, channel: None) -> str:
        """Return the next monotonic channel version string."""
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        next_v = current_v + 1
        next_h = random.random()  # nosec B311 - non-crypto ordering/uniqueness only
        # Fixed 16-decimal precision keeps the suffix a stable ``0.<digits>`` shape.
        # A bare ``:016`` width spec left-zero-pads short reprs (e.g. ``0.5`` ->
        # ``0000000000000.5``), which is inconsistent and non-deterministic. See #397.
        return f"{next_v:032}.{next_h:.16f}"

    @property
    def lock_scope_identity(self) -> str | None:
        """Stable identity of the physical checkpoint storage, or ``None``.

        ``"azure-blob:<account>/<container>"`` when the container client
        reports both names (the real ``azure.storage.blob.ContainerClient``
        does). Every saver, graph, app and process that writes the same
        container reports the same string, so thread locks can be scoped to the
        storage rather than to a graph name. Checkpoint blobs are not prefixed,
        so the container is the whole collision scope.
        """
        account = getattr(self._container_client, "account_name", None)
        container = getattr(self._container_client, "container_name", None)
        if not isinstance(account, str) or not isinstance(container, str):
            return None
        if not account or not container:
            return None
        return f"azure-blob:{account}/{container}"

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Fetch a checkpoint tuple by config or latest checkpoint hint."""
        thread_id = self._config_thread_id(config)
        checkpoint_ns = self._config_checkpoint_ns(config)
        requested_checkpoint_id = get_checkpoint_id(config)

        if requested_checkpoint_id:
            return self._build_tuple(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=requested_checkpoint_id,
                return_config=config,
            )

        # Only a versioned clean hint certifies completed publication.
        latest_hint = self._read_latest_checkpoint_id(thread_id, checkpoint_ns)
        if latest_hint is not None:
            # Try loading the hinted checkpoint directly — avoids prefix scan.
            hint_tuple = self._build_tuple(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=latest_hint,
                return_config=self._checkpoint_config(thread_id, checkpoint_ns, latest_hint),
            )
            if hint_tuple is not None:
                return hint_tuple
            # Hint blob missing/corrupt — fall through to scan.

        # Hint was missing or stale — fall back to authoritative prefix scan.
        actual_latest = self._find_latest_checkpoint_id(thread_id, checkpoint_ns)
        if actual_latest is None:
            return None

        return self._build_tuple(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=actual_latest,
            return_config=self._checkpoint_config(thread_id, checkpoint_ns, actual_latest),
        )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints matching config and optional metadata filters."""
        if limit is not None and limit <= 0:
            return
        remaining = limit
        config_checkpoint_ns = self._config_checkpoint_ns(config) if config else None
        config_checkpoint_id = get_checkpoint_id(config) if config else None
        before_checkpoint_id = get_checkpoint_id(before) if before else None

        thread_ids = [self._config_thread_id(config)] if config else self._list_thread_ids()
        for thread_id in thread_ids:
            checkpoint_namespaces = (
                [config_checkpoint_ns]
                if config_checkpoint_ns is not None
                else self._list_checkpoint_namespaces(thread_id)
            )
            for checkpoint_ns in checkpoint_namespaces:
                checkpoint_ids = self._list_checkpoint_ids(thread_id, checkpoint_ns)
                for checkpoint_id in checkpoint_ids:
                    if config_checkpoint_id is not None and checkpoint_id != config_checkpoint_id:
                        continue
                    if before_checkpoint_id is not None and checkpoint_id >= before_checkpoint_id:
                        continue

                    preloaded_metadata: tuple[str, bytes, dict[str, str]] | None = None
                    if filter:
                        # Read the small metadata blob first so non-matching
                        # checkpoints skip channel-value and write downloads.
                        preloaded_metadata = self._download_typed_blob(
                            self._metadata_blob_path(thread_id, checkpoint_ns, checkpoint_id)
                        )
                        if preloaded_metadata is None:
                            continue
                        prefetched: CheckpointMetadata = self.serde.loads_typed(
                            (preloaded_metadata[0], preloaded_metadata[1])
                        )
                        if not all(
                            query_value == prefetched.get(query_key)
                            for query_key, query_value in filter.items()
                        ):
                            continue

                    checkpoint_tuple = self._build_tuple(
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        return_config=self._checkpoint_config(
                            thread_id,
                            checkpoint_ns,
                            checkpoint_id,
                        ),
                        preloaded_metadata=preloaded_metadata,
                    )
                    if checkpoint_tuple is None:
                        continue

                    if remaining is not None:
                        remaining -= 1

                    yield checkpoint_tuple

                    if remaining is not None and remaining <= 0:
                        return

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store checkpoint state, metadata, channel values, and latest hint."""
        guard = self._write_guard(config)
        thread_id = self._config_thread_id(config)
        checkpoint_ns = self._config_checkpoint_ns(config)
        checkpoint_id = checkpoint["id"]
        parent_checkpoint_id = get_checkpoint_id(config)

        # Persist dirty intent first so a crash after the marker cannot hide it.
        # checkpoint.bin acts as the commit marker: if it exists, the checkpoint
        # is valid.  Partial failures before it leave harmless orphans.
        # The hint is only a hint: ``current_latest`` feeds ``max(current_latest, id)`` in the
        # clean hint published below, so it must name a checkpoint that exists. The one HEAD
        # probe is what keeps that true; it is deliberately not removed (#860). A clean hint
        # can name a missing checkpoint (``delete_checkpoints_before`` /
        # ``delete_old_checkpoints`` leave ``latest.json`` alone), and then, without the
        # probe, (a) a hint above this id would be re-published and keep pointing at nothing,
        # and (b) a hint below this id would hide a committed checkpoint newer than this id,
        # because the scan that finds it would be skipped. Skipping the probe whenever the
        # hint is below this id is not equivalent either (case b). Pinned by
        # ``test_put_probes_the_hinted_checkpoint_before_trusting_it`` in
        # tests/test_blob_format_compat.py; see docs/blob-checkpoint-format.md.
        current_latest = self._read_latest_checkpoint_id(thread_id, checkpoint_ns, for_write=True)
        if current_latest is None or not self._blob_exists(
            self._checkpoint_blob_path(thread_id, checkpoint_ns, current_latest)
        ):
            current_latest = self._find_latest_checkpoint_id(thread_id, checkpoint_ns)
        check_ownership_guard(guard)
        self._upload_blob(
            self._latest_blob_path(thread_id, checkpoint_ns),
            b'{"version":1,"dirty":true}',
            {},
        )
        checkpoint_data: dict[str, Any] = dict(checkpoint)
        channel_values: dict[str, Any] = checkpoint_data.pop("channel_values", {})

        # 1. Channel value blobs
        for channel, version in new_versions.items():
            value_blob_path = self._value_blob_path(thread_id, checkpoint_ns, channel, version)
            if channel in channel_values:
                serde_type, payload = self.serde.dumps_typed(channel_values[channel])
            else:
                serde_type, payload = "empty", b""
            check_ownership_guard(guard)
            self._upload_blob(value_blob_path, payload, {"serde_type": serde_type})

        # 2. Metadata blob (before commit marker)
        metadata_payload_type, metadata_payload = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        check_ownership_guard(guard)
        self._upload_blob(
            self._metadata_blob_path(thread_id, checkpoint_ns, checkpoint_id),
            metadata_payload,
            {"serde_type": metadata_payload_type},
        )

        # 3. Checkpoint blob (commit marker — existence = valid checkpoint)
        checkpoint_serde_type, checkpoint_payload = self.serde.dumps_typed(checkpoint_data)
        checkpoint_blob_metadata: dict[str, str] = {"serde_type": checkpoint_serde_type}
        if parent_checkpoint_id is not None:
            checkpoint_blob_metadata["parent_id"] = quote(parent_checkpoint_id, safe="")
        check_ownership_guard(guard)
        self._upload_blob(
            self._checkpoint_blob_path(thread_id, checkpoint_ns, checkpoint_id),
            checkpoint_payload,
            checkpoint_blob_metadata,
        )

        # 4. Publish clean only after the marker, without regressing the pointer.
        latest_payload = json.dumps(
            {
                "version": 1,
                "dirty": False,
                "checkpoint_id": max(current_latest or "", checkpoint_id),
            },
            separators=(",", ":"),
        ).encode()
        check_ownership_guard(guard)
        self._upload_blob(self._latest_blob_path(thread_id, checkpoint_ns), latest_payload, {})
        return self._checkpoint_config(thread_id, checkpoint_ns, checkpoint_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store task writes for a checkpoint, preserving MemorySaver semantics."""
        guard = self._write_guard(config)
        thread_id = self._config_thread_id(config)
        checkpoint_ns = self._config_checkpoint_ns(config)
        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id is None:
            raise ValueError("checkpoint_id is required in config to store writes")

        for index, (channel, value) in enumerate(writes):
            write_index = WRITES_IDX_MAP.get(channel, index)
            write_blob_path = self._write_blob_path(
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                task_id,
                write_index,
            )

            serde_type, payload = self.serde.dumps_typed(value)
            write_metadata = {
                "serde_type": serde_type,
                "channel": quote(channel, safe=""),
                "task_id": quote(task_id, safe=""),
                "task_path": quote(task_path, safe=""),
            }
            check_ownership_guard(guard)
            if write_index >= 0:
                # Write-once slot: one create-only request instead of an existence
                # check plus upload. An existing blob (a retried write) is left as is.
                self._create_blob(write_blob_path, payload, write_metadata)
            else:
                self._upload_blob(write_blob_path, payload, write_metadata)

    def delete_thread(self, thread_id: str) -> None:
        """Delete all blobs for a thread."""
        thread_prefix = self._thread_prefix(thread_id)
        for blob in self._container_client.list_blobs(name_starts_with=thread_prefix):
            self._container_client.get_blob_client(blob.name).delete_blob()

    def delete_checkpoints_before(
        self,
        thread_id: str,
        *,
        before_checkpoint_id: str,
        checkpoint_ns: str | None = None,
    ) -> int:
        """Delete checkpoints older than ``before_checkpoint_id``.

        Checkpoint IDs are lexicographically sortable timestamps, so "older"
        means ``id < before_checkpoint_id``. The reference checkpoint itself
        is preserved.

        Only checkpoint marker, metadata, and write blobs under the
        checkpoint base prefix are deleted. Channel value blobs (under
        ``values/``) and the ``latest.json`` pointer are intentionally
        left untouched: values may still be referenced by retained
        checkpoints, and the latest pointer remains valid as long as the
        latest checkpoint itself is retained.

        .. note::
           This is safe but not exhaustive. Channel value blobs that
           were referenced *only* by the now-deleted checkpoints become
           orphaned and are not removed. Use
           :meth:`collect_orphaned_values` as the second step to reclaim
           them.

        Parameters
        ----------
        thread_id:
            Target thread identifier.
        before_checkpoint_id:
            Cutoff checkpoint id (exclusive); checkpoints strictly older are
            deleted.
        checkpoint_ns:
            Restrict deletion to a single checkpoint namespace. ``None``
            (default) sweeps every namespace under the thread.

        Returns
        -------
        int
            Number of distinct checkpoint ids deleted across the targeted
            namespaces.
        """
        namespaces = (
            [checkpoint_ns]
            if checkpoint_ns is not None
            else self._list_checkpoint_namespaces(thread_id)
        )

        deleted = 0
        for ns in namespaces:
            for cid in self._list_checkpoint_ids(thread_id, ns):
                if cid >= before_checkpoint_id:
                    continue
                self._delete_checkpoint_blobs(thread_id, ns, cid)
                deleted += 1
        return deleted

    def delete_old_checkpoints(
        self,
        thread_id: str,
        *,
        keep_last: int = 20,
        checkpoint_ns: str | None = None,
    ) -> int:
        """Retain only the ``keep_last`` newest checkpoints per namespace.

        Only checkpoint marker, metadata, and write blobs are deleted.
        Channel value blobs and ``latest.json`` are preserved (see
        :meth:`delete_checkpoints_before` for the rationale).

        .. note::
           This is safe but not exhaustive. Channel value blobs that
           were referenced *only* by the now-deleted checkpoints become
           orphaned and are not removed. Use
           :meth:`collect_orphaned_values` as the second step to reclaim
           them.

        Parameters
        ----------
        thread_id:
            Target thread identifier.
        keep_last:
            Number of newest checkpoints to keep per namespace. Must be
            non-negative; ``0`` deletes every checkpoint in the namespace.
        checkpoint_ns:
            Restrict pruning to a single namespace. ``None`` (default)
            prunes every namespace under the thread.

        Returns
        -------
        int
            Number of checkpoints deleted across the targeted namespaces.
        """
        if keep_last < 0:
            raise ValueError("keep_last must be non-negative")

        namespaces = (
            [checkpoint_ns]
            if checkpoint_ns is not None
            else self._list_checkpoint_namespaces(thread_id)
        )

        deleted = 0
        for ns in namespaces:
            checkpoint_ids = self._list_checkpoint_ids(thread_id, ns)
            for cid in checkpoint_ids[keep_last:]:
                self._delete_checkpoint_blobs(thread_id, ns, cid)
                deleted += 1
        return deleted

    def collect_orphaned_values(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str | None = None,
        dry_run: bool = True,
        grace_period_seconds: int = 300,
    ) -> OrphanedValueCollectionResult:
        """Garbage-collect channel value blobs orphaned by checkpoint deletion.

        :meth:`delete_old_checkpoints` and :meth:`delete_checkpoints_before`
        intentionally preserve channel value blobs because they may still
        be referenced by retained checkpoints. This helper closes the
        loop: it walks the surviving checkpoints, builds the set of
        ``(channel, version)`` pairs they reference, and removes any
        ``values/`` blob outside that retained set.

        Safety contract:

        * Per-namespace fail-closed: the namespace is skipped (recorded
          in :attr:`OrphanedValueCollectionResult.skipped_namespaces`)
           if ``latest.json`` is dirty, legacy, or cannot be loaded **or** any surviving
          checkpoint blob listed under the namespace is unreadable /
          fails deserialization. Without a complete, trustworthy
          survivor set the helper has no way to compute the retained
          ``(channel, version)`` set, so it refuses to delete.
        * Set semantics: a blob is preserved if **any** surviving
          checkpoint references its ``(channel, version)``. Versions
          shared across checkpoints are never deleted as long as one
          owner survives.
        * Recent-write grace period: any value blob whose
          ``last_modified`` falls inside the ``grace_period_seconds``
          window — or whose ``last_modified`` is unavailable — is
          deferred to a future GC pass and recorded in
          :attr:`OrphanedValueCollectionResult.skipped_recent`.
          The checkpoint write order is dirty intent → values → metadata → checkpoint
          → ``latest.json``, so a value blob that exists but is not yet
          referenced by any checkpoint may simply be a write in flight.
        * Concurrency re-check: even after the grace filter, the
          survivor set is re-scanned per orphan immediately before
          deletion. This protects an *older* value blob that a newly
          finalized checkpoint started referencing after the initial
          snapshot. Such blobs appear in ``would_delete`` but not in
          ``deleted``.

        Parameters
        ----------
        thread_id:
            Target thread identifier.
        checkpoint_ns:
            Restrict collection to a single namespace. ``None`` (default)
            sweeps every namespace under the thread.
        dry_run:
            When ``True`` (default), nothing is deleted; the returned
            ``would_delete`` list captures every blob a non-dry-run pass
            over the same snapshot would attempt to delete.
        grace_period_seconds:
            Defer deletion of any value blob whose ``last_modified`` is
            within this many seconds of now (default 300). Set to ``0``
            to disable the recent-write guard — only safe when the
            caller can guarantee no concurrent checkpoint writes (e.g.
            an offline maintenance window).

        Returns
        -------
        OrphanedValueCollectionResult
            Audit record of what was identified, deleted, and skipped.
        """
        result = OrphanedValueCollectionResult(dry_run=dry_run)

        namespaces = (
            [checkpoint_ns]
            if checkpoint_ns is not None
            else self._list_checkpoint_namespaces(thread_id)
        )

        cutoff = self._utcnow() - timedelta(seconds=grace_period_seconds)

        for ns in namespaces:
            if self._read_latest_checkpoint_id(thread_id, ns) is None:
                logger.warning(
                    "collect_orphaned_values: skipping namespace (thread=%s, ns=%s); "
                    "latest.json missing, dirty, legacy or unreadable",
                    thread_id,
                    ns,
                )
                result.skipped_namespaces.append((thread_id, ns))
                continue

            retained = self._collect_retained_versions(thread_id, ns)
            if retained is None:
                result.skipped_namespaces.append((thread_id, ns))
                continue

            value_blobs = self._list_value_blobs(thread_id, ns)

            ns_orphans: list[str] = []
            for blob_path, channel_version, last_modified in value_blobs:
                if channel_version is None or channel_version in retained:
                    continue
                if last_modified is None or last_modified > cutoff:
                    result.skipped_recent.append(blob_path)
                    continue
                ns_orphans.append(blob_path)
                result.would_delete.append(blob_path)

            if dry_run:
                continue

            for blob_path in ns_orphans:
                if self._read_latest_checkpoint_id(thread_id, ns) is None:
                    result.skipped_namespaces.append((thread_id, ns))
                    break
                fresh_retained = self._collect_retained_versions(thread_id, ns)
                if fresh_retained is None:
                    logger.warning(
                        "collect_orphaned_values: aborting deletes for namespace "
                        "(thread=%s, ns=%s) mid-pass; survivor re-scan untrustworthy",
                        thread_id,
                        ns,
                    )
                    result.skipped_namespaces.append((thread_id, ns))
                    break
                channel_version = self._parse_value_blob_path(thread_id, ns, blob_path)
                if channel_version is not None and channel_version in fresh_retained:
                    continue
                try:
                    self._container_client.get_blob_client(blob_path).delete_blob()
                except self._not_found_error:
                    pass
                result.deleted.append(blob_path)

        return result

    def _utcnow(self) -> datetime:
        return datetime.now(UTC)

    def _collect_retained_versions(
        self, thread_id: str, checkpoint_ns: str
    ) -> set[tuple[str, str]] | None:
        retained: set[tuple[str, str]] = set()
        for cid in self._list_checkpoint_ids(thread_id, checkpoint_ns):
            blob_path = self._checkpoint_blob_path(thread_id, checkpoint_ns, cid)
            typed_blob = self._download_typed_blob(blob_path)
            if typed_blob is None:
                logger.warning(
                    "collect_orphaned_values: surviving checkpoint blob %s missing or "
                    "lacks serde_type metadata; treating namespace as untrustworthy",
                    blob_path,
                )
                return None
            try:
                checkpoint_data: Checkpoint = self.serde.loads_typed((typed_blob[0], typed_blob[1]))
                channel_versions = checkpoint_data["channel_versions"]
            except Exception:
                logger.warning(
                    "collect_orphaned_values: failed to deserialize surviving "
                    "checkpoint blob %s; treating namespace as untrustworthy",
                    blob_path,
                )
                return None
            for channel, version in channel_versions.items():
                retained.add((channel, str(version)))
        return retained

    def _list_value_blobs(
        self, thread_id: str, checkpoint_ns: str
    ) -> builtins.list[tuple[str, tuple[str, str] | None, datetime | None]]:
        values_prefix = f"{self._namespace_prefix(thread_id, checkpoint_ns)}values/"
        items: list[tuple[str, tuple[str, str] | None, datetime | None]] = []
        for blob in self._container_client.list_blobs(name_starts_with=values_prefix):
            last_modified = getattr(blob, "last_modified", None)
            items.append(
                (
                    blob.name,
                    self._parse_value_blob_path(thread_id, checkpoint_ns, blob.name),
                    last_modified,
                )
            )
        return items

    def _parse_value_blob_path(
        self, thread_id: str, checkpoint_ns: str, blob_path: str
    ) -> tuple[str, str] | None:
        values_prefix = f"{self._namespace_prefix(thread_id, checkpoint_ns)}values/"
        if not blob_path.startswith(values_prefix):
            return None
        relative = blob_path[len(values_prefix) :]
        parts = relative.split("/")
        if len(parts) != 2 or not parts[1].endswith(".bin"):
            return None
        channel = unquote(parts[0])
        version = unquote(parts[1].removesuffix(".bin"))
        return (channel, version)

    def _delete_checkpoint_blobs(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> None:
        base_prefix = self._checkpoint_base_prefix(thread_id, checkpoint_ns, checkpoint_id)
        for blob in self._container_client.list_blobs(name_starts_with=base_prefix):
            try:
                self._container_client.get_blob_client(blob.name).delete_blob()
            except self._not_found_error:
                continue

    def _build_tuple(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        return_config: RunnableConfig,
        preloaded_metadata: tuple[str, bytes, dict[str, str]] | None = None,
    ) -> CheckpointTuple | None:
        checkpoint_blob_path = self._checkpoint_blob_path(thread_id, checkpoint_ns, checkpoint_id)
        checkpoint_blob_result = self._download_typed_blob(checkpoint_blob_path)
        if checkpoint_blob_result is None:
            return None

        serde_type, payload, checkpoint_blob_metadata = checkpoint_blob_result
        raw_parent_id = checkpoint_blob_metadata.get("parent_id")
        parent_checkpoint_id = unquote(raw_parent_id) if raw_parent_id else None
        checkpoint_data: Checkpoint = self.serde.loads_typed((serde_type, payload))
        channel_values = self._load_channel_values(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            channel_versions=checkpoint_data["channel_versions"],
        )

        metadata_blob_result = (
            preloaded_metadata
            if preloaded_metadata is not None
            else self._download_typed_blob(
                self._metadata_blob_path(thread_id, checkpoint_ns, checkpoint_id)
            )
        )
        if metadata_blob_result is None:
            return None
        checkpoint_metadata: CheckpointMetadata = self.serde.loads_typed(
            (metadata_blob_result[0], metadata_blob_result[1])
        )

        return CheckpointTuple(
            config=return_config,
            checkpoint={**checkpoint_data, "channel_values": channel_values},
            metadata=checkpoint_metadata,
            parent_config=(
                self._checkpoint_config(thread_id, checkpoint_ns, parent_checkpoint_id)
                if parent_checkpoint_id
                else None
            ),
            pending_writes=self._load_pending_writes(thread_id, checkpoint_ns, checkpoint_id),
        )

    def _load_channel_values(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        channel_versions: ChannelVersions,
    ) -> dict[str, Any]:
        channels = builtins.list(channel_versions.items())
        results = _map_bounded(
            lambda item: self._download_typed_blob(
                self._value_blob_path(thread_id, checkpoint_ns, item[0], item[1])
            ),
            channels,
        )
        values: dict[str, Any] = {}
        for (channel, _version), typed_blob in zip(channels, results, strict=True):
            if typed_blob is None:
                continue
            if typed_blob[0] == "empty":
                continue
            values[channel] = self.serde.loads_typed((typed_blob[0], typed_blob[1]))
        return values

    def _load_pending_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> builtins.list[tuple[str, str, Any]]:
        writes_prefix = self._writes_prefix(thread_id, checkpoint_ns, checkpoint_id)
        loaded: list[tuple[str, str, int, str, Any]] = []
        candidates: list[tuple[str, str, int]] = []

        for blob in self._container_client.list_blobs(name_starts_with=writes_prefix):
            # Parse relative to writes_prefix: expect "{task_id}/{idx}.bin"
            if not blob.name.startswith(writes_prefix):
                continue
            relative = blob.name[len(writes_prefix) :]
            rel_parts = relative.split("/")
            if len(rel_parts) != 2:
                continue

            task_id = unquote(rel_parts[0])
            file_name = rel_parts[1]
            if not file_name.endswith(".bin"):
                continue

            try:
                write_index = int(unquote(file_name.removesuffix(".bin")))
            except ValueError:
                continue
            candidates.append((blob.name, task_id, write_index))

        results = _map_bounded(
            lambda candidate: self._download_typed_blob(candidate[0]),
            candidates,
        )
        for (_blob_name, task_id, write_index), typed_blob_result in zip(
            candidates, results, strict=True
        ):
            if typed_blob_result is None:
                continue
            # Use metadata from the download result (avoids extra API call)
            blob_meta = typed_blob_result[2]
            raw_channel = blob_meta.get("channel")
            raw_metadata_task_id = blob_meta.get("task_id")
            if raw_channel is None:
                continue
            channel = unquote(raw_channel)
            resolved_task_id = unquote(raw_metadata_task_id) if raw_metadata_task_id else task_id

            value = self.serde.loads_typed((typed_blob_result[0], typed_blob_result[1]))
            task_path = unquote(blob_meta.get("task_path", ""))
            loaded.append((task_path, resolved_task_id, write_index, channel, value))

        loaded.sort(key=lambda item: (item[0], item[1], item[2]))
        return [(task_id, channel, value) for _path, task_id, _idx, channel, value in loaded]

    def _find_latest_checkpoint_id(self, thread_id: str, checkpoint_ns: str) -> str | None:
        checkpoint_ids = self._list_checkpoint_ids(thread_id, checkpoint_ns)
        if not checkpoint_ids:
            return None
        return checkpoint_ids[0]

    def _list_checkpoint_ids(self, thread_id: str, checkpoint_ns: str) -> builtins.list[str]:
        checkpoints_prefix = self._checkpoints_prefix(thread_id, checkpoint_ns)
        checkpoint_ids: set[str] = set()

        for blob in self._container_client.list_blobs(name_starts_with=checkpoints_prefix):
            if not blob.name.endswith("/checkpoint.bin"):
                continue

            path_parts = blob.name.split("/")
            if len(path_parts) != 7:
                continue

            checkpoint_ids.add(unquote(path_parts[5]))

        return sorted(checkpoint_ids, reverse=True)

    def _list_thread_ids(self) -> builtins.list[str]:
        thread_ids: set[str] = set()
        for blob in self._container_client.list_blobs(name_starts_with="threads/"):
            path_parts = blob.name.split("/")
            if len(path_parts) >= 2 and path_parts[0] == "threads":
                thread_ids.add(unquote(path_parts[1]))
        return sorted(thread_ids)

    def _list_checkpoint_namespaces(self, thread_id: str) -> builtins.list[str]:
        namespace_prefix = f"{self._thread_prefix(thread_id)}ns/"
        checkpoint_namespaces: set[str] = set()

        for blob in self._container_client.list_blobs(name_starts_with=namespace_prefix):
            path_parts = blob.name.split("/")
            if len(path_parts) >= 4 and path_parts[2] == "ns":
                checkpoint_namespaces.add(unquote(path_parts[3]))

        return sorted(checkpoint_namespaces)

    def _read_latest_checkpoint_id(
        self, thread_id: str, checkpoint_ns: str, *, for_write: bool = False
    ) -> str | None:
        latest_path = self._latest_blob_path(thread_id, checkpoint_ns)
        latest_payload = self._download_blob(latest_path)
        if latest_payload is None:
            return None

        return self._parse_latest_checkpoint_id(latest_payload, latest_path, for_write=for_write)

    def _parse_latest_checkpoint_id(
        self, latest_payload: bytes, latest_path: str, *, for_write: bool = False
    ) -> str | None:
        """Return the trusted hinted checkpoint id, or ``None`` when not trusted.

        Readers ignore a hint they do not understand and scan instead. With
        ``for_write=True`` (the pre-write read in ``put`` / ``aput``) a JSON
        object whose ``version`` is present but not a version this release
        understands raises :class:`BlobCheckpointFormatError`, because the
        writer would otherwise replace it. Legacy (no ``version``), dirty,
        malformed and non-object hints are still treated as "no usable hint".
        """

        try:
            data = json.loads(latest_payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Ignoring malformed latest.json at %s", latest_path)
            return None

        if not isinstance(data, dict):
            return None
        if "version" in data and not _is_known_hint_version(data["version"]):
            if for_write:
                raise BlobCheckpointFormatError(
                    "Refusing to overwrite the latest.json checkpoint hint: it was written in "
                    f"{_describe_unknown_version(data['version'])}, but this release only "
                    f"understands version {_LATEST_HINT_VERSION}. No data was modified. "
                    "Upgrade azure-functions-langgraph or restore the hint before writing."
                )
            return None
        if "version" not in data or data.get("dirty") is not False:
            return None

        checkpoint_id = data.get("checkpoint_id")
        if isinstance(checkpoint_id, str) and checkpoint_id:
            return checkpoint_id
        return None

    def _write_guard(self, config: RunnableConfig) -> OwnershipGuard | None:
        """Resolve the ownership guard for one write, failing closed when required.

        Runs before any blob I/O. A guard bound by the adapter for the current
        context wins over one carried in ``config``; a value under the private
        config key that is not a real guard is ignored.
        """
        guard = resolve_ownership_guard(config)
        if guard is None and self._require_ownership_guard:
            raise ThreadLeaseLostError(reason="missing")
        return guard

    def _checkpoint_config(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def _download_typed_blob(self, blob_path: str) -> tuple[str, bytes, dict[str, str]] | None:
        """Download a blob and return ``(serde_type, payload, full_metadata)``."""
        try:
            downloader = self._container_client.get_blob_client(blob_path).download_blob()
            payload = downloader.readall()
        except self._not_found_error:
            return None

        # Body and metadata come from one GET (the downloader's ``properties``).
        metadata = _downloader_metadata(downloader)
        if metadata is None:
            metadata = self._blob_metadata(blob_path)
        serde_type = metadata.get("serde_type")
        if not serde_type:
            logger.warning("Blob missing serde_type metadata: %s", blob_path)
            return None

        return (serde_type, payload, metadata)

    def _blob_metadata(self, blob_path: str) -> dict[str, str]:
        try:
            properties = self._container_client.get_blob_client(blob_path).get_blob_properties()
        except self._not_found_error:
            return {}
        metadata = properties.metadata
        return metadata if metadata is not None else {}

    def _download_blob(self, blob_path: str) -> bytes | None:
        try:
            return self._container_client.get_blob_client(blob_path).download_blob().readall()
        except self._not_found_error:
            return None

    def _blob_exists(self, blob_path: str) -> bool:
        try:
            self._container_client.get_blob_client(blob_path).get_blob_properties()
        except self._not_found_error:
            return False
        return True

    def _upload_blob(
        self, blob_path: str, payload: bytes, metadata: dict[str, str], overwrite: bool = True
    ) -> None:
        self._container_client.get_blob_client(blob_path).upload_blob(
            payload,
            metadata=metadata,
            overwrite=overwrite,
        )

    def _create_blob(self, blob_path: str, payload: bytes, metadata: dict[str, str]) -> bool:
        """Create ``blob_path`` only if absent; return ``False`` when it already exists.

        ``overwrite=False`` makes the service evaluate the condition atomically
        (``If-None-Match: *``), so two writers cannot both create the slot.
        """
        try:
            self._upload_blob(blob_path, payload, metadata, overwrite=False)
        except self._exists_error:
            return False
        return True

    def _thread_prefix(self, thread_id: str) -> str:
        return f"threads/{self._escape(thread_id)}/"

    def _namespace_prefix(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._thread_prefix(thread_id)}ns/{self._escape(checkpoint_ns)}/"

    def _checkpoints_prefix(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._namespace_prefix(thread_id, checkpoint_ns)}checkpoints/"

    def _checkpoint_base_prefix(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> str:
        return f"{self._checkpoints_prefix(thread_id, checkpoint_ns)}{self._escape(checkpoint_id)}/"

    def _checkpoint_blob_path(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return (
            f"{self._checkpoint_base_prefix(thread_id, checkpoint_ns, checkpoint_id)}checkpoint.bin"
        )

    def _metadata_blob_path(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return (
            f"{self._checkpoint_base_prefix(thread_id, checkpoint_ns, checkpoint_id)}metadata.bin"
        )

    def _writes_prefix(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._checkpoint_base_prefix(thread_id, checkpoint_ns, checkpoint_id)}writes/"

    def _write_blob_path(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        task_id: str,
        write_index: int,
    ) -> str:
        return (
            f"{self._writes_prefix(thread_id, checkpoint_ns, checkpoint_id)}"
            f"{self._escape(task_id)}/{self._escape(str(write_index))}.bin"
        )

    def _value_blob_path(
        self,
        thread_id: str,
        checkpoint_ns: str,
        channel: str,
        version: str | int | float,
    ) -> str:
        return (
            f"{self._namespace_prefix(thread_id, checkpoint_ns)}values/"
            f"{self._escape(channel)}/{self._escape(str(version))}.bin"
        )

    def _latest_blob_path(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._namespace_prefix(thread_id, checkpoint_ns)}latest.json"

    def _config_thread_id(self, config: RunnableConfig) -> str:
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise ValueError("configurable must be provided in RunnableConfig")

        thread_id = configurable.get("thread_id")
        if thread_id is None:
            raise ValueError("thread_id is required in config.configurable")
        return str(thread_id)

    def _config_checkpoint_ns(self, config: RunnableConfig) -> str:
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise ValueError("configurable must be provided in RunnableConfig")
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        return str(checkpoint_ns)

    def _escape(self, segment: str) -> str:
        return quote(segment, safe="")

    # ------------------------------------------------------------------
    # Native async API
    #
    # These mirror the synchronous methods above but perform blob I/O
    # through ``aio_container_client`` (``azure.storage.blob.aio``). When no
    # aio client was provided the public methods fall back to running the
    # synchronous implementation in a thread executor (with a one-time
    # warning) so a graph driven by ``ainvoke``/``astream`` still works.
    # ------------------------------------------------------------------
    def _require_aio_container_client(self) -> _AsyncContainerClientProtocol:
        client = self._aio_container_client
        if client is None:  # pragma: no cover - guarded by public methods
            raise RuntimeError("aio_container_client is not configured")
        return client

    def _warn_sync_fallback(self, method: str) -> None:
        if not self._sync_fallback_warned:
            self._sync_fallback_warned = True
            logger.warning(
                "AzureBlobCheckpointSaver.%s was called without an "
                "'aio_container_client'; falling back to running the "
                "synchronous implementation in a thread executor. Pass "
                "'aio_container_client' for native async blob I/O.",
                method,
            )

    async def _adownload_blob(self, blob_path: str) -> bytes | None:
        client = self._require_aio_container_client().get_blob_client(blob_path)
        try:
            downloader = await client.download_blob()
            return await downloader.readall()
        except self._not_found_error:
            return None

    async def _ablob_metadata(self, blob_path: str) -> dict[str, str]:
        client = self._require_aio_container_client().get_blob_client(blob_path)
        try:
            properties = await client.get_blob_properties()
        except self._not_found_error:
            return {}
        metadata = properties.metadata
        return metadata if metadata is not None else {}

    async def _adownload_typed_blob(
        self, blob_path: str
    ) -> tuple[str, bytes, dict[str, str]] | None:
        """Async twin of :meth:`_download_typed_blob`."""
        client = self._require_aio_container_client().get_blob_client(blob_path)
        try:
            downloader = await client.download_blob()
            payload = await downloader.readall()
        except self._not_found_error:
            return None

        metadata = _downloader_metadata(downloader)
        if metadata is None:
            metadata = await self._ablob_metadata(blob_path)
        serde_type = metadata.get("serde_type")
        if not serde_type:
            logger.warning("Blob missing serde_type metadata: %s", blob_path)
            return None

        return (serde_type, payload, metadata)

    async def _ablob_exists(self, blob_path: str) -> bool:
        client = self._require_aio_container_client().get_blob_client(blob_path)
        try:
            await client.get_blob_properties()
        except self._not_found_error:
            return False
        return True

    async def _aupload_blob(
        self, blob_path: str, payload: bytes, metadata: dict[str, str], overwrite: bool = True
    ) -> None:
        client = self._require_aio_container_client().get_blob_client(blob_path)
        await client.upload_blob(payload, metadata=metadata, overwrite=overwrite)

    async def _acreate_blob(self, blob_path: str, payload: bytes, metadata: dict[str, str]) -> bool:
        """Async twin of :meth:`_create_blob`."""
        try:
            await self._aupload_blob(blob_path, payload, metadata, overwrite=False)
        except self._exists_error:
            return False
        return True

    async def _alist_blob_items(self, prefix: str) -> builtins.list[_BlobItemProtocol]:
        container = self._require_aio_container_client()
        items: list[_BlobItemProtocol] = []
        async for blob in container.list_blobs(name_starts_with=prefix):
            items.append(blob)
        return items

    async def _aread_latest_checkpoint_id(
        self, thread_id: str, checkpoint_ns: str, *, for_write: bool = False
    ) -> str | None:
        latest_path = self._latest_blob_path(thread_id, checkpoint_ns)
        latest_payload = await self._adownload_blob(latest_path)
        if latest_payload is None:
            return None
        return self._parse_latest_checkpoint_id(latest_payload, latest_path, for_write=for_write)

    async def _afind_latest_checkpoint_id(self, thread_id: str, checkpoint_ns: str) -> str | None:
        checkpoint_ids = await self._alist_checkpoint_ids(thread_id, checkpoint_ns)
        if not checkpoint_ids:
            return None
        return checkpoint_ids[0]

    async def _alist_checkpoint_ids(self, thread_id: str, checkpoint_ns: str) -> builtins.list[str]:
        checkpoints_prefix = self._checkpoints_prefix(thread_id, checkpoint_ns)
        checkpoint_ids: set[str] = set()

        for blob in await self._alist_blob_items(checkpoints_prefix):
            if not blob.name.endswith("/checkpoint.bin"):
                continue

            path_parts = blob.name.split("/")
            if len(path_parts) != 7:
                continue

            checkpoint_ids.add(unquote(path_parts[5]))

        return sorted(checkpoint_ids, reverse=True)

    async def _alist_thread_ids(self) -> builtins.list[str]:
        thread_ids: set[str] = set()
        for blob in await self._alist_blob_items("threads/"):
            path_parts = blob.name.split("/")
            if len(path_parts) >= 2 and path_parts[0] == "threads":
                thread_ids.add(unquote(path_parts[1]))
        return sorted(thread_ids)

    async def _alist_checkpoint_namespaces(self, thread_id: str) -> builtins.list[str]:
        namespace_prefix = f"{self._thread_prefix(thread_id)}ns/"
        checkpoint_namespaces: set[str] = set()

        for blob in await self._alist_blob_items(namespace_prefix):
            path_parts = blob.name.split("/")
            if len(path_parts) >= 4 and path_parts[2] == "ns":
                checkpoint_namespaces.add(unquote(path_parts[3]))

        return sorted(checkpoint_namespaces)

    async def _aload_channel_values(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        channel_versions: ChannelVersions,
    ) -> dict[str, Any]:
        channels = builtins.list(channel_versions.items())
        results = await _amap_bounded(
            lambda item: self._adownload_typed_blob(
                self._value_blob_path(thread_id, checkpoint_ns, item[0], item[1])
            ),
            channels,
        )
        values: dict[str, Any] = {}
        for (channel, _version), typed_blob in zip(channels, results, strict=True):
            if typed_blob is None:
                continue
            if typed_blob[0] == "empty":
                continue
            values[channel] = self.serde.loads_typed((typed_blob[0], typed_blob[1]))
        return values

    async def _aload_pending_writes(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> builtins.list[tuple[str, str, Any]]:
        writes_prefix = self._writes_prefix(thread_id, checkpoint_ns, checkpoint_id)
        loaded: list[tuple[str, str, int, str, Any]] = []
        candidates: list[tuple[str, str, int]] = []

        for blob in await self._alist_blob_items(writes_prefix):
            if not blob.name.startswith(writes_prefix):
                continue
            relative = blob.name[len(writes_prefix) :]
            rel_parts = relative.split("/")
            if len(rel_parts) != 2:
                continue

            task_id = unquote(rel_parts[0])
            file_name = rel_parts[1]
            if not file_name.endswith(".bin"):
                continue

            try:
                write_index = int(unquote(file_name.removesuffix(".bin")))
            except ValueError:
                continue
            candidates.append((blob.name, task_id, write_index))

        results = await _amap_bounded(
            lambda candidate: self._adownload_typed_blob(candidate[0]),
            candidates,
        )
        for (_blob_name, task_id, write_index), typed_blob_result in zip(
            candidates, results, strict=True
        ):
            if typed_blob_result is None:
                continue
            blob_meta = typed_blob_result[2]
            raw_channel = blob_meta.get("channel")
            raw_metadata_task_id = blob_meta.get("task_id")
            if raw_channel is None:
                continue
            channel = unquote(raw_channel)
            resolved_task_id = unquote(raw_metadata_task_id) if raw_metadata_task_id else task_id

            value = self.serde.loads_typed((typed_blob_result[0], typed_blob_result[1]))
            task_path = unquote(blob_meta.get("task_path", ""))
            loaded.append((task_path, resolved_task_id, write_index, channel, value))

        loaded.sort(key=lambda item: (item[0], item[1], item[2]))
        return [(task_id, channel, value) for _path, task_id, _idx, channel, value in loaded]

    async def _abuild_tuple(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        return_config: RunnableConfig,
        preloaded_metadata: tuple[str, bytes, dict[str, str]] | None = None,
    ) -> CheckpointTuple | None:
        checkpoint_blob_path = self._checkpoint_blob_path(thread_id, checkpoint_ns, checkpoint_id)
        checkpoint_blob_result = await self._adownload_typed_blob(checkpoint_blob_path)
        if checkpoint_blob_result is None:
            return None

        serde_type, payload, checkpoint_blob_metadata = checkpoint_blob_result
        raw_parent_id = checkpoint_blob_metadata.get("parent_id")
        parent_checkpoint_id = unquote(raw_parent_id) if raw_parent_id else None
        checkpoint_data: Checkpoint = self.serde.loads_typed((serde_type, payload))
        channel_values = await self._aload_channel_values(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            channel_versions=checkpoint_data["channel_versions"],
        )

        metadata_blob_result = (
            preloaded_metadata
            if preloaded_metadata is not None
            else await self._adownload_typed_blob(
                self._metadata_blob_path(thread_id, checkpoint_ns, checkpoint_id)
            )
        )
        if metadata_blob_result is None:
            return None
        checkpoint_metadata: CheckpointMetadata = self.serde.loads_typed(
            (metadata_blob_result[0], metadata_blob_result[1])
        )

        return CheckpointTuple(
            config=return_config,
            checkpoint={**checkpoint_data, "channel_values": channel_values},
            metadata=checkpoint_metadata,
            parent_config=(
                self._checkpoint_config(thread_id, checkpoint_ns, parent_checkpoint_id)
                if parent_checkpoint_id
                else None
            ),
            pending_writes=await self._aload_pending_writes(
                thread_id, checkpoint_ns, checkpoint_id
            ),
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Asynchronously fetch a checkpoint tuple (native aio when available)."""
        if self._aio_container_client is None:
            self._warn_sync_fallback("aget_tuple")
            return await asyncio.to_thread(self.get_tuple, config)

        thread_id = self._config_thread_id(config)
        checkpoint_ns = self._config_checkpoint_ns(config)
        requested_checkpoint_id = get_checkpoint_id(config)

        if requested_checkpoint_id:
            return await self._abuild_tuple(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=requested_checkpoint_id,
                return_config=config,
            )

        latest_hint = await self._aread_latest_checkpoint_id(thread_id, checkpoint_ns)
        if latest_hint is not None:
            hint_tuple = await self._abuild_tuple(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=latest_hint,
                return_config=self._checkpoint_config(thread_id, checkpoint_ns, latest_hint),
            )
            if hint_tuple is not None:
                return hint_tuple

        actual_latest = await self._afind_latest_checkpoint_id(thread_id, checkpoint_ns)
        if actual_latest is None:
            return None

        return await self._abuild_tuple(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=actual_latest,
            return_config=self._checkpoint_config(thread_id, checkpoint_ns, actual_latest),
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Asynchronously list checkpoints (native aio when available)."""
        if self._aio_container_client is None:
            self._warn_sync_fallback("alist")
            items = await asyncio.to_thread(
                lambda: list(self.list(config, filter=filter, before=before, limit=limit))
            )
            for item in items:
                yield item
            return

        if limit is not None and limit <= 0:
            return
        remaining = limit
        config_checkpoint_ns = self._config_checkpoint_ns(config) if config else None
        config_checkpoint_id = get_checkpoint_id(config) if config else None
        before_checkpoint_id = get_checkpoint_id(before) if before else None

        thread_ids = [self._config_thread_id(config)] if config else await self._alist_thread_ids()
        for thread_id in thread_ids:
            checkpoint_namespaces = (
                [config_checkpoint_ns]
                if config_checkpoint_ns is not None
                else await self._alist_checkpoint_namespaces(thread_id)
            )
            for checkpoint_ns in checkpoint_namespaces:
                checkpoint_ids = await self._alist_checkpoint_ids(thread_id, checkpoint_ns)
                for checkpoint_id in checkpoint_ids:
                    if config_checkpoint_id is not None and checkpoint_id != config_checkpoint_id:
                        continue
                    if before_checkpoint_id is not None and checkpoint_id >= before_checkpoint_id:
                        continue

                    preloaded_metadata: tuple[str, bytes, dict[str, str]] | None = None
                    if filter:
                        # Read the small metadata blob first so non-matching
                        # checkpoints skip channel-value and write downloads.
                        preloaded_metadata = await self._adownload_typed_blob(
                            self._metadata_blob_path(thread_id, checkpoint_ns, checkpoint_id)
                        )
                        if preloaded_metadata is None:
                            continue
                        prefetched: CheckpointMetadata = self.serde.loads_typed(
                            (preloaded_metadata[0], preloaded_metadata[1])
                        )
                        if not all(
                            query_value == prefetched.get(query_key)
                            for query_key, query_value in filter.items()
                        ):
                            continue

                    checkpoint_tuple = await self._abuild_tuple(
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        return_config=self._checkpoint_config(
                            thread_id,
                            checkpoint_ns,
                            checkpoint_id,
                        ),
                        preloaded_metadata=preloaded_metadata,
                    )
                    if checkpoint_tuple is None:
                        continue

                    if remaining is not None:
                        remaining -= 1

                    yield checkpoint_tuple

                    if remaining is not None and remaining <= 0:
                        return

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Asynchronously store a checkpoint (native aio when available)."""
        if self._aio_container_client is None:
            self._warn_sync_fallback("aput")
            return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

        guard = self._write_guard(config)
        thread_id = self._config_thread_id(config)
        checkpoint_ns = self._config_checkpoint_ns(config)
        checkpoint_id = checkpoint["id"]
        parent_checkpoint_id = get_checkpoint_id(config)

        # Same hinted-checkpoint existence probe as in ``put`` (see the comment there, #860).
        current_latest = await self._aread_latest_checkpoint_id(
            thread_id, checkpoint_ns, for_write=True
        )
        if current_latest is None or not await self._ablob_exists(
            self._checkpoint_blob_path(thread_id, checkpoint_ns, current_latest)
        ):
            current_latest = await self._afind_latest_checkpoint_id(thread_id, checkpoint_ns)
        check_ownership_guard(guard)
        await self._aupload_blob(
            self._latest_blob_path(thread_id, checkpoint_ns),
            b'{"version":1,"dirty":true}',
            {},
        )

        checkpoint_data: dict[str, Any] = dict(checkpoint)
        channel_values: dict[str, Any] = checkpoint_data.pop("channel_values", {})

        # 1. Channel value blobs
        for channel, version in new_versions.items():
            value_blob_path = self._value_blob_path(thread_id, checkpoint_ns, channel, version)
            if channel in channel_values:
                serde_type, payload = self.serde.dumps_typed(channel_values[channel])
            else:
                serde_type, payload = "empty", b""
            check_ownership_guard(guard)
            await self._aupload_blob(value_blob_path, payload, {"serde_type": serde_type})

        # 2. Metadata blob (before commit marker)
        metadata_payload_type, metadata_payload = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        check_ownership_guard(guard)
        await self._aupload_blob(
            self._metadata_blob_path(thread_id, checkpoint_ns, checkpoint_id),
            metadata_payload,
            {"serde_type": metadata_payload_type},
        )

        # 3. Checkpoint blob (commit marker — existence = valid checkpoint)
        checkpoint_serde_type, checkpoint_payload = self.serde.dumps_typed(checkpoint_data)
        checkpoint_blob_metadata: dict[str, str] = {"serde_type": checkpoint_serde_type}
        if parent_checkpoint_id is not None:
            checkpoint_blob_metadata["parent_id"] = quote(parent_checkpoint_id, safe="")
        check_ownership_guard(guard)
        await self._aupload_blob(
            self._checkpoint_blob_path(thread_id, checkpoint_ns, checkpoint_id),
            checkpoint_payload,
            checkpoint_blob_metadata,
        )

        # 4. Publish clean only after the marker, without regressing the pointer.
        latest_payload = json.dumps(
            {
                "version": 1,
                "dirty": False,
                "checkpoint_id": max(current_latest or "", checkpoint_id),
            },
            separators=(",", ":"),
        ).encode()
        check_ownership_guard(guard)
        await self._aupload_blob(
            self._latest_blob_path(thread_id, checkpoint_ns), latest_payload, {}
        )
        return self._checkpoint_config(thread_id, checkpoint_ns, checkpoint_id)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Asynchronously store task writes (native aio when available)."""
        if self._aio_container_client is None:
            self._warn_sync_fallback("aput_writes")
            await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)
            return

        guard = self._write_guard(config)
        thread_id = self._config_thread_id(config)
        checkpoint_ns = self._config_checkpoint_ns(config)
        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id is None:
            raise ValueError("checkpoint_id is required in config to store writes")

        for index, (channel, value) in enumerate(writes):
            write_index = WRITES_IDX_MAP.get(channel, index)
            write_blob_path = self._write_blob_path(
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                task_id,
                write_index,
            )

            serde_type, payload = self.serde.dumps_typed(value)
            write_metadata = {
                "serde_type": serde_type,
                "channel": quote(channel, safe=""),
                "task_id": quote(task_id, safe=""),
                "task_path": quote(task_path, safe=""),
            }
            check_ownership_guard(guard)
            if write_index >= 0:
                # Write-once slot: one create-only request instead of an existence
                # check plus upload. An existing blob (a retried write) is left as is.
                await self._acreate_blob(write_blob_path, payload, write_metadata)
            else:
                await self._aupload_blob(write_blob_path, payload, write_metadata)

    async def adelete_thread(self, thread_id: str) -> None:
        """Asynchronously delete all blobs for a thread (native aio when available)."""
        if self._aio_container_client is None:
            self._warn_sync_fallback("adelete_thread")
            await asyncio.to_thread(self.delete_thread, thread_id)
            return

        container = self._require_aio_container_client()
        thread_prefix = self._thread_prefix(thread_id)
        async for blob in container.list_blobs(name_starts_with=thread_prefix):
            await container.get_blob_client(blob.name).delete_blob()
