"""Azure Blob Storage-backed checkpoint saver for LangGraph."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
import importlib
import json
import logging
import random
from typing import Any, List, Protocol, cast
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


class _BlobDownloadProtocol(Protocol):
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


class _ContainerClientProtocol(Protocol):
    def get_blob_client(self, blob: str) -> _BlobClientProtocol: ...

    def list_blobs(self, name_starts_with: str = "") -> Sequence[_BlobItemProtocol]: ...

logger = logging.getLogger(__name__)


class AzureBlobCheckpointSaver(BaseCheckpointSaver[str]):
    """Persist LangGraph checkpoints in Azure Blob Storage.

    Note:
        This checkpointer assumes single-writer-per-thread semantics.
        Concurrent writes to the same thread from multiple processes or
        Azure Functions instances may corrupt checkpoint data. See
        DESIGN.md decision #7 for details.
    """

    def __init__(
        self,
        *,
        container_client: _ContainerClientProtocol,
        serde: SerializerProtocol | None = None,
    ) -> None:
        """Create a saver bound to an Azure Blob container client."""
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
                "container_client must be an instance of "
                "azure.storage.blob.ContainerClient"
            )

        super().__init__(serde=serde)
        self._container_client: _ContainerClientProtocol = cast(
            _ContainerClientProtocol,
            container_client,
        )

        try:
            azure_core_exceptions = importlib.import_module("azure.core.exceptions")
            resource_not_found_error = getattr(
                azure_core_exceptions, "ResourceNotFoundError", None
            )
            if resource_not_found_error is None:
                raise ImportError(
                    "azure.core.exceptions.ResourceNotFoundError not found"
                )
        except ImportError as exc:
            raise ImportError(
                "AzureBlobCheckpointSaver requires 'azure-core'. "
                "Install with: pip install azure-functions-langgraph[azure-blob]"
            ) from exc
        self._not_found_error: type[BaseException] = cast(
            type[BaseException],
            resource_not_found_error,
        )
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
        return f"{next_v:032}.{next_h:016}"

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

        # Optimization: try latest.json hint, but verify it is actually the
        # latest checkpoint.  If the hint is stale (e.g. concurrent writer),
        # fall through to the authoritative prefix scan.
        latest_hint = self._read_latest_checkpoint_id(thread_id, checkpoint_ns)
        actual_latest = self._find_latest_checkpoint_id(thread_id, checkpoint_ns)
        if actual_latest is None:
            return None

        # If the hint matches the scan result we avoid re-scanning, but we
        # always trust the scan over the hint.
        resolved_id = actual_latest
        # When the hint is valid AND matches, we already know the id.
        if latest_hint is not None and latest_hint == actual_latest:
            resolved_id = latest_hint

        return self._build_tuple(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=resolved_id,
            return_config=self._checkpoint_config(thread_id, checkpoint_ns, resolved_id),
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

                    checkpoint_tuple = self._build_tuple(
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        return_config=self._checkpoint_config(
                            thread_id,
                            checkpoint_ns,
                            checkpoint_id,
                        ),
                    )
                    if checkpoint_tuple is None:
                        continue

                    if filter and not all(
                        query_value == checkpoint_tuple.metadata.get(query_key)
                        for query_key, query_value in filter.items()
                    ):
                        continue

                    if remaining is not None:
                        if remaining <= 0:
                            return
                        remaining -= 1

                    yield checkpoint_tuple

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store checkpoint state, metadata, channel values, and latest hint."""
        thread_id = self._config_thread_id(config)
        checkpoint_ns = self._config_checkpoint_ns(config)
        checkpoint_id = checkpoint["id"]
        parent_checkpoint_id = get_checkpoint_id(config)

        # Write order: values → metadata → checkpoint (commit marker) → latest.
        # checkpoint.bin acts as the commit marker: if it exists, the checkpoint
        # is valid.  Partial failures before it leave harmless orphans.
        checkpoint_data: dict[str, Any] = dict(checkpoint)
        channel_values: dict[str, Any] = checkpoint_data.pop("channel_values", {})

        # 1. Channel value blobs
        for channel, version in new_versions.items():
            value_blob_path = self._value_blob_path(thread_id, checkpoint_ns, channel, version)
            if channel in channel_values:
                serde_type, payload = self.serde.dumps_typed(channel_values[channel])
            else:
                serde_type, payload = "empty", b""
            self._upload_blob(value_blob_path, payload, {"serde_type": serde_type})

        # 2. Metadata blob (before commit marker)
        metadata_payload_type, metadata_payload = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
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
        self._upload_blob(
            self._checkpoint_blob_path(thread_id, checkpoint_ns, checkpoint_id),
            checkpoint_payload,
            checkpoint_blob_metadata,
        )

        # 4. Monotonic latest.json hint (best-effort, after commit marker)
        current_latest = self._read_latest_checkpoint_id(thread_id, checkpoint_ns)
        if current_latest is None or checkpoint_id >= current_latest:
            latest_payload = json.dumps(
                {"checkpoint_id": checkpoint_id},
                separators=(",", ":"),
            ).encode()
            self._upload_blob(
                self._latest_blob_path(thread_id, checkpoint_ns),
                latest_payload,
                {},
            )
        return self._checkpoint_config(thread_id, checkpoint_ns, checkpoint_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store task writes for a checkpoint, preserving MemorySaver semantics."""
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

            if write_index >= 0 and self._blob_exists(write_blob_path):
                continue

            serde_type, payload = self.serde.dumps_typed(value)
            self._upload_blob(
                write_blob_path,
                payload,
                {
                    "serde_type": serde_type,
                    "channel": quote(channel, safe=""),
                    "task_id": quote(task_id, safe=""),
                    "task_path": quote(task_path, safe=""),
                },
            )

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
           orphaned and are not removed. Full value-blob garbage
           collection is tracked separately and will land as an opt-in
           helper in a future release.

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
           orphaned and are not removed. Full value-blob garbage
           collection is tracked separately and will land as an opt-in
           helper in a future release.

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

        metadata_blob_result = self._download_typed_blob(
            self._metadata_blob_path(thread_id, checkpoint_ns, checkpoint_id)
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
        values: dict[str, Any] = {}
        for channel, version in channel_versions.items():
            typed_blob = self._download_typed_blob(
                self._value_blob_path(thread_id, checkpoint_ns, channel, version)
            )
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
    ) -> List[tuple[str, str, Any]]:
        writes_prefix = self._writes_prefix(thread_id, checkpoint_ns, checkpoint_id)
        loaded: List[tuple[str, int, str, Any]] = []

        for blob in self._container_client.list_blobs(name_starts_with=writes_prefix):
            # Parse relative to writes_prefix: expect "{task_id}/{idx}.bin"
            if not blob.name.startswith(writes_prefix):
                continue
            relative = blob.name[len(writes_prefix):]
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
            typed_blob_result = self._download_typed_blob(blob.name)
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
            loaded.append((resolved_task_id, write_index, channel, value))

        loaded.sort(key=lambda item: (item[0], item[1]))
        return [(task_id, channel, value) for task_id, _idx, channel, value in loaded]

    def _find_latest_checkpoint_id(self, thread_id: str, checkpoint_ns: str) -> str | None:
        checkpoint_ids = self._list_checkpoint_ids(thread_id, checkpoint_ns)
        if not checkpoint_ids:
            return None
        return checkpoint_ids[0]

    def _list_checkpoint_ids(self, thread_id: str, checkpoint_ns: str) -> List[str]:
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

    def _list_thread_ids(self) -> List[str]:
        thread_ids: set[str] = set()
        for blob in self._container_client.list_blobs(name_starts_with="threads/"):
            path_parts = blob.name.split("/")
            if len(path_parts) >= 2 and path_parts[0] == "threads":
                thread_ids.add(unquote(path_parts[1]))
        return sorted(thread_ids)

    def _list_checkpoint_namespaces(self, thread_id: str) -> List[str]:
        namespace_prefix = f"{self._thread_prefix(thread_id)}ns/"
        checkpoint_namespaces: set[str] = set()

        for blob in self._container_client.list_blobs(name_starts_with=namespace_prefix):
            path_parts = blob.name.split("/")
            if len(path_parts) >= 4 and path_parts[2] == "ns":
                checkpoint_namespaces.add(unquote(path_parts[3]))

        return sorted(checkpoint_namespaces)

    def _read_latest_checkpoint_id(self, thread_id: str, checkpoint_ns: str) -> str | None:
        latest_path = self._latest_blob_path(thread_id, checkpoint_ns)
        latest_payload = self._download_blob(latest_path)
        if latest_payload is None:
            return None

        try:
            data = json.loads(latest_payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Ignoring malformed latest.json at %s", latest_path)
            return None

        if not isinstance(data, dict):
            return None

        checkpoint_id = data.get("checkpoint_id")
        if isinstance(checkpoint_id, str):
            return checkpoint_id
        return None

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

    def _download_typed_blob(
        self, blob_path: str
    ) -> tuple[str, bytes, dict[str, str]] | None:
        """Download a blob and return ``(serde_type, payload, full_metadata)``."""
        payload = self._download_blob(blob_path)
        if payload is None:
            return None

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

    def _upload_blob(self, blob_path: str, payload: bytes, metadata: dict[str, str]) -> None:
        self._container_client.get_blob_client(blob_path).upload_blob(
            payload,
            metadata=metadata,
            overwrite=True,
        )

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
        return (
            f"{self._checkpoints_prefix(thread_id, checkpoint_ns)}"
            f"{self._escape(checkpoint_id)}/"
        )

    def _checkpoint_blob_path(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return (
            f"{self._checkpoint_base_prefix(thread_id, checkpoint_ns, checkpoint_id)}"
            "checkpoint.bin"
        )

    def _metadata_blob_path(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return (
            f"{self._checkpoint_base_prefix(thread_id, checkpoint_ns, checkpoint_id)}"
            "metadata.bin"
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
