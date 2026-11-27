"""Duck-typed Blob transport contracts; importing these never loads Azure SDKs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, Protocol


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
    def upload_blob(
        self, data: bytes, metadata: dict[str, str], overwrite: bool, **kwargs: Any
    ) -> dict[str, Any]: ...

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
    async def upload_blob(
        self, data: bytes, metadata: dict[str, str], overwrite: bool, **kwargs: Any
    ) -> dict[str, Any]: ...

    async def download_blob(self) -> _AsyncBlobDownloadProtocol: ...

    async def get_blob_properties(self) -> _BlobPropertiesProtocol: ...

    async def delete_blob(self) -> None: ...


class _AsyncContainerClientProtocol(Protocol):
    def get_blob_client(self, blob: str) -> _AsyncBlobClientProtocol: ...

    def list_blobs(self, name_starts_with: str = "") -> AsyncIterator[_BlobItemProtocol]: ...
