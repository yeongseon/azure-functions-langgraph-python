"""Low-level Blob I/O; checkpoint publication and ownership checks stay in the saver."""

from __future__ import annotations

import builtins
import importlib
import logging

from azure_functions_langgraph.checkpointers._blob_clients import (
    _AsyncContainerClientProtocol,
    _BlobItemProtocol,
    _ContainerClientProtocol,
)
from azure_functions_langgraph.checkpointers._blob_hint import (
    HintSnapshot,
    conflict_error,
    download_etag,
    hint_conditions,
    response_etag,
)

logger = logging.getLogger("azure_functions_langgraph.checkpointers.azure_blob")


def _downloader_metadata(downloader: object) -> dict[str, str] | None:
    """Return the blob metadata carried by a download response, or ``None`` if absent.

    ``None`` (a downloader without ``properties`` or without a ``metadata`` mapping, for
    example a duck-typed client) makes the caller fall back to a separate properties
    request. A real, empty mapping is a valid answer: the blob has no metadata.
    """
    metadata = getattr(getattr(downloader, "properties", None), "metadata", None)
    return dict(metadata) if isinstance(metadata, dict) else None


class _BlobStorage:
    _container_client: _ContainerClientProtocol
    _aio_container_client: _AsyncContainerClientProtocol | None
    _not_found_error: type[BaseException]
    _exists_error: type[BaseException]

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

    def _hint_snapshot(self, blob_path: str) -> HintSnapshot:
        try:
            stream = self._container_client.get_blob_client(blob_path).download_blob()
            return HintSnapshot(stream.readall(), download_etag(stream.properties))
        except self._not_found_error:
            return HintSnapshot(None, None)

    def _blob_exists(self, blob_path: str) -> bool:
        try:
            self._container_client.get_blob_client(blob_path).get_blob_properties()
        except self._not_found_error:
            return False
        return True

    def _upload_blob(
        self,
        blob_path: str,
        payload: bytes,
        metadata: dict[str, str],
        overwrite: bool = True,
        *,
        etag: str | None = None,
    ) -> str:
        client = self._container_client.get_blob_client(blob_path)
        if not blob_path.endswith("/latest.json"):
            client.upload_blob(payload, metadata=metadata, overwrite=overwrite)
            return ""
        conditions = hint_conditions(etag) if etag is not None else {}
        http_error = importlib.import_module("azure.core.exceptions").HttpResponseError
        try:
            result = client.upload_blob(
                payload, metadata=metadata, overwrite=overwrite, **conditions
            )
        except self._exists_error as exc:
            raise conflict_error() from exc
        except http_error as exc:
            if getattr(exc, "status_code", None) == 412:
                raise conflict_error() from exc
            raise
        return response_etag(result)

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

    def _require_aio_container_client(self) -> _AsyncContainerClientProtocol:
        client = self._aio_container_client
        if client is None:  # pragma: no cover - guarded by public methods
            raise RuntimeError("aio_container_client is not configured")
        return client

    async def _adownload_blob(self, blob_path: str) -> bytes | None:
        client = self._require_aio_container_client().get_blob_client(blob_path)
        try:
            downloader = await client.download_blob()
            return await downloader.readall()
        except self._not_found_error:
            return None

    async def _ahint_snapshot(self, blob_path: str) -> HintSnapshot:
        client = self._require_aio_container_client().get_blob_client(blob_path)
        try:
            stream = await client.download_blob()
            return HintSnapshot(await stream.readall(), download_etag(stream.properties))
        except self._not_found_error:
            return HintSnapshot(None, None)

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
        self,
        blob_path: str,
        payload: bytes,
        metadata: dict[str, str],
        overwrite: bool = True,
        *,
        etag: str | None = None,
    ) -> str:
        client = self._require_aio_container_client().get_blob_client(blob_path)
        if not blob_path.endswith("/latest.json"):
            await client.upload_blob(payload, metadata=metadata, overwrite=overwrite)
            return ""
        conditions = hint_conditions(etag) if etag is not None else {}
        http_error = importlib.import_module("azure.core.exceptions").HttpResponseError
        try:
            result = await client.upload_blob(
                payload, metadata=metadata, overwrite=overwrite, **conditions
            )
        except self._exists_error as exc:
            raise conflict_error() from exc
        except http_error as exc:
            if getattr(exc, "status_code", None) == 412:
                raise conflict_error() from exc
            raise
        return response_etag(result)

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
