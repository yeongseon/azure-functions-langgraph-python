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

import importlib
from typing import Any, Iterator, Sequence
import uuid

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata
import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver

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

    async def aget_tuple(self, config: RunnableConfig) -> Any:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Any:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)


@pytest.fixture
def azurite_container_client() -> Iterator[Any]:
    """Yield a fresh, uniquely-named live Azurite blob ContainerClient."""
    try:
        blob_module = importlib.import_module("azure.storage.blob")
        blob_service_client = blob_module.BlobServiceClient.from_connection_string(
            AZURITE_BLOB_CONNECTION_STRING
        )
        # Probe connectivity so an unavailable Azurite skips rather than errors.
        _ = blob_service_client.get_service_properties()
    except Exception as exc:  # pragma: no cover - only when Azurite is absent
        pytest.skip(f"Azurite Blob Storage not available: {exc}")

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
