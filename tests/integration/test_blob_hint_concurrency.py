"""Real SDK preconditions on the configured local emulator; not Azure certification."""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

from azure_functions_langgraph.checkpointers.azure_blob import (
    AzureBlobCheckpointSaver,
    BlobCheckpointConflictError,
)
from tests.integration.test_checkpoint_conformance import (
    _blob_connection_string,
)
from tests.integration.test_checkpoint_conformance import (
    azurite_container_client as azurite_container_client,
)
from tests.test_checkpointers_azure_blob import _checkpoint, _config, _metadata

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("fail_second_hint", [False, True])
async def test_sdk_rejects_stale_publication_and_reader_recovers_max(
    azurite_container_client: Any,
    native: bool,
    fail_second_hint: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aio_module = importlib.import_module("azure.storage.blob.aio")
    async with aio_module.ContainerClient.from_connection_string(
        _blob_connection_string(), azurite_container_client.container_name
    ) as aio:
        first = AzureBlobCheckpointSaver(
            container_client=azurite_container_client, aio_container_client=aio
        )
        second = AzureBlobCheckpointSaver(
            container_client=azurite_container_client, aio_container_client=aio
        )
        cfg = _config()
        upload, aupload = first._upload_blob, first._aupload_blob
        second_upload, second_aupload = second._upload_blob, second._aupload_blob
        fired = False

        def fail(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
            if path.endswith("latest.json") and not json.loads(data)["dirty"]:
                raise TimeoutError("injected clean hint failure")
            return second_upload(path, data, metadata, **kwargs)

        async def afail(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
            if path.endswith("latest.json") and not json.loads(data)["dirty"]:
                raise TimeoutError("injected clean hint failure")
            return await second_aupload(path, data, metadata, **kwargs)

        if fail_second_hint:
            monkeypatch.setattr(second, "_upload_blob", fail)
            monkeypatch.setattr(second, "_aupload_blob", afail)

        def overlap(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
            nonlocal fired
            result = upload(path, data, metadata, **kwargs)
            if path.endswith("latest.json") and not fired:
                fired = True
                try:
                    second.put(cfg, _checkpoint("cp-002"), _metadata(), {})
                except TimeoutError:
                    if not fail_second_hint:
                        raise
            return result

        async def aoverlap(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
            nonlocal fired
            result = await aupload(path, data, metadata, **kwargs)
            if path.endswith("latest.json") and not fired:
                fired = True
                try:
                    await second.aput(cfg, _checkpoint("cp-002"), _metadata(), {})
                except TimeoutError:
                    if not fail_second_hint:
                        raise
            return result

        monkeypatch.setattr(first, "_upload_blob", overlap)
        monkeypatch.setattr(first, "_aupload_blob", aoverlap)
        with pytest.raises(BlobCheckpointConflictError) as caught:
            if native:
                await first.aput(cfg, _checkpoint("cp-001"), _metadata(), {})
            else:
                first.put(cfg, _checkpoint("cp-001"), _metadata(), {})
        assert fired and getattr(caught.value.__cause__, "status_code", None) == 412
        reader = AzureBlobCheckpointSaver(
            container_client=azurite_container_client, aio_container_client=aio
        )
        latest = await reader.aget_tuple(cfg) if native else reader.get_tuple(cfg)
        assert latest is not None and latest.checkpoint["id"] == "cp-002"
        assert [item.checkpoint["id"] for item in reader.list(cfg)] == ["cp-002", "cp-001"]
