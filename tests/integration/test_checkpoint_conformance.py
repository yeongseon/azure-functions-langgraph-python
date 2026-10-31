"""Azurite-backed conformance suite for AzureBlobCheckpointSaver.

Runs LangGraph's official ``langgraph-checkpoint-conformance`` harness against
``AzureBlobCheckpointSaver`` wired to a **live Azurite blob backend** (not the
in-process ``MockContainerClient`` used by the unit-test conformance run in
``tests/test_checkpointers_azure_blob.py``). This exercises the real Azure
Storage Blob client code paths end-to-end, guarding against contract drift that
a mock cannot surface. See issue #344.

The test is marked ``integration`` and skips cleanly when Azurite (or the
optional conformance dependency) is unavailable, so the default unit-test run is
unaffected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
import importlib
import json
from typing import Any
import uuid

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata, CheckpointTuple
import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from tests.storage_service import fail_or_skip_service, require_tcp_service
from tests.test_checkpointers_azure_blob import _checkpoint, _config, _metadata

pytestmark = pytest.mark.integration

# Blob endpoint on Azurite's default blob port (10000). Well-known devstore key.
AZURITE_BLOB_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1"
)

# The conformance harness is an optional dev dependency.
try:
    _conformance: Any = importlib.import_module("langgraph.checkpoint.conformance")
    _checkpointer_test = _conformance.checkpointer_test
    _validate = _conformance.validate
    _HAS_CONFORMANCE = True
except ImportError:  # pragma: no cover - exercised only without the optional dep
    _HAS_CONFORMANCE = False


class _AsyncConformanceSaver(AzureBlobCheckpointSaver):
    """Async wrappers forwarding to the sync AzureBlobCheckpointSaver methods.

    The conformance harness detects base capabilities via async method overrides
    and drives the saver through its async API, so we forward each async
    entrypoint to the real synchronous storage logic (the same pattern the
    upstream ``InMemorySaver`` uses).
    """

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Any,
    ) -> RunnableConfig:
        result: RunnableConfig = self.put(config, checkpoint, metadata, new_versions)
        return result

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)


@pytest.fixture
def azurite_container_client() -> Iterator[Any]:
    """Yield a fresh, uniquely-named live Azurite blob ContainerClient."""
    require_tcp_service("127.0.0.1", 10000, "Azurite Blob")
    try:
        blob_module = importlib.import_module("azure.storage.blob")
        blob_service_client = blob_module.BlobServiceClient.from_connection_string(
            AZURITE_BLOB_CONNECTION_STRING
        )
        # Probe connectivity so an unavailable Azurite skips rather than errors.
        _ = blob_service_client.get_service_properties()
    except Exception as exc:  # pragma: no cover - only when Azurite is absent
        fail_or_skip_service(f"Azurite Blob Storage is incompatible or unavailable: {exc}")

    container_name = f"aflgconf{uuid.uuid4().hex[:18]}"
    container_client = blob_service_client.create_container(container_name)
    try:
        yield container_client
    finally:
        try:
            container_client.delete_container()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass


@pytest.mark.skipif(
    not _HAS_CONFORMANCE,
    reason="langgraph-checkpoint-conformance not installed",
)
async def test_conformance_base_capabilities_azurite(
    azurite_container_client: Any,
) -> None:
    """AzureBlobCheckpointSaver passes the upstream contract on a live backend."""

    @_checkpointer_test(name="AzureBlobCheckpointSaver-azurite")  # type: ignore[untyped-decorator]
    async def _factory() -> Any:
        yield _AsyncConformanceSaver(container_client=azurite_container_client)

    report = await _validate(_factory)

    base_capabilities = ("put", "put_writes", "get_tuple", "list", "delete_thread")
    for cap in base_capabilities:
        result = report.results[cap]
        assert result.detected, f"base capability {cap!r} was not detected"
        assert result.passed, f"base capability {cap!r} failed conformance: {result.failures!r}"


@pytest.mark.parametrize("native", [False, True])
async def test_native_and_fallback_conformance_azurite(
    azurite_container_client: Any, native: bool
) -> None:
    aio_module = importlib.import_module("azure.storage.blob.aio")
    async with aio_module.ContainerClient.from_connection_string(
        AZURITE_BLOB_CONNECTION_STRING, azurite_container_client.container_name
    ) as aio:

        async def factory() -> Any:
            yield AzureBlobCheckpointSaver(
                container_client=azurite_container_client,
                aio_container_client=aio if native else None,
            )

        report = await _validate(
            _checkpointer_test(name=f"AzureBlobCheckpointSaver-azurite-{native}")(factory)
        )
        for cap in ("put", "put_writes", "get_tuple", "list", "delete_thread"):
            result = report.results[cap]
            assert result.detected
            assert result.passed, result.failures


@pytest.mark.parametrize("mode", ["sync", "native", "fallback"])
async def test_latest_publication_failure_recovers_on_azurite(
    azurite_container_client: Any, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: A is stored on Azurite; only B's final clean publication is interrupted.
    aio_module = importlib.import_module("azure.storage.blob.aio")
    async with aio_module.ContainerClient.from_connection_string(
        AZURITE_BLOB_CONNECTION_STRING, azurite_container_client.container_name
    ) as aio:
        saver = AzureBlobCheckpointSaver(
            container_client=azurite_container_client,
            aio_container_client=aio if mode == "native" else None,
        )
        cfg = _config(checkpoint_ns="recovery")
        saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})
        upload = saver._upload_blob
        aupload = saver._aupload_blob

        def fail_clean(path: str, data: bytes, metadata: dict[str, str]) -> None:
            if path.endswith("latest.json") and json.loads(data).get("dirty") is not True:
                raise TimeoutError(path)
            upload(path, data, metadata)

        async def afail_clean(path: str, data: bytes, metadata: dict[str, str]) -> None:
            if path.endswith("latest.json") and json.loads(data).get("dirty") is not True:
                raise TimeoutError(path)
            await aupload(path, data, metadata)

        monkeypatch.setattr(saver, "_upload_blob", fail_clean)
        monkeypatch.setattr(saver, "_aupload_blob", afail_clean)

        # When: the real backend accepts B's marker and a fresh saver reads after failure.
        with pytest.raises(TimeoutError):
            if mode == "sync":
                saver.put(cfg, _checkpoint("cp-002"), _metadata(), {})
            else:
                await saver.aput(cfg, _checkpoint("cp-002"), _metadata(), {})
        reader = AzureBlobCheckpointSaver(
            container_client=azurite_container_client,
            aio_container_client=aio if mode == "native" else None,
        )
        latest = reader.get_tuple(cfg) if mode == "sync" else await reader.aget_tuple(cfg)

        # Then: B is visible while explicit A and both history entries remain intact.
        assert latest is not None
        assert latest.checkpoint["id"] == "cp-002"
        older = reader.get_tuple(_config(checkpoint_ns="recovery", checkpoint_id="cp-001"))
        assert older is not None
        assert older.checkpoint["id"] == "cp-001"
        assert [c.checkpoint["id"] for c in reader.list(cfg)] == ["cp-002", "cp-001"]
