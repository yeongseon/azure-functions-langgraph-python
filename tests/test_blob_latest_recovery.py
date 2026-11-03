"""Failure-window recovery with shared fake storage, not cloud certification."""

from __future__ import annotations

from datetime import datetime
import json

import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from tests.test_checkpointers_azure_blob import (
    AsyncMockContainerClient,
    MockContainerClient,
    _checkpoint,
    _config,
    _metadata,
)
from tests.test_checkpointers_azure_blob import (
    _install_fake_azure_modules as _install_fake_azure_modules,
)

pytestmark = pytest.mark.usefixtures("_install_fake_azure_modules")


@pytest.mark.parametrize("mode", ["sync", "native", "fallback"])
async def test_fresh_reader_recovers_committed_state_after_hint_failure(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: A is committed; B's final hint publication fails before storage accepts it.
    container = MockContainerClient()
    aio = AsyncMockContainerClient(container) if mode == "native" else None
    saver = AzureBlobCheckpointSaver(container_client=container, aio_container_client=aio)
    cfg = _config(checkpoint_ns="recovery")
    if mode == "sync":
        saver.put(cfg, _checkpoint("cp-001"), _metadata(step=1), {})
    else:
        await saver.aput(cfg, _checkpoint("cp-001"), _metadata(step=1), {})
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

    # When: B commits but publication fails, and a new saver reads the shared store.
    with pytest.raises(TimeoutError):
        if mode == "sync":
            saver.put(cfg, _checkpoint("cp-002"), _metadata(step=2), {})
        else:
            await saver.aput(cfg, _checkpoint("cp-002"), _metadata(step=2), {})
    reader = AzureBlobCheckpointSaver(container_client=container, aio_container_client=aio)
    latest = reader.get_tuple(cfg) if mode == "sync" else await reader.aget_tuple(cfg)

    # Then: latest visibility recovers without altering explicit-ID or history semantics.
    assert latest is not None
    assert latest.checkpoint["id"] == "cp-002"
    explicit = _config(checkpoint_ns="recovery", checkpoint_id="cp-001")
    older = reader.get_tuple(explicit) if mode == "sync" else await reader.aget_tuple(explicit)
    assert older is not None
    assert older.checkpoint["id"] == "cp-001"
    history = list(reader.list(cfg)) if mode == "sync" else [c async for c in reader.alist(cfg)]
    assert [c.checkpoint["id"] for c in history] == ["cp-002", "cp-001"]


@pytest.mark.parametrize("mode", ["sync", "native", "fallback"])
@pytest.mark.parametrize("phase", ["dirty", "value", "metadata", "marker", "interrupted"])
async def test_failed_write_exposes_only_committed_markers(
    mode: str, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: A is durable and B will fail at a controlled storage boundary.
    container = MockContainerClient()
    aio = AsyncMockContainerClient(container) if mode == "native" else None
    saver = AzureBlobCheckpointSaver(container_client=container, aio_container_client=aio)
    cfg = _config(checkpoint_ns="failure")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})
    upload = saver._upload_blob
    aupload = saver._aupload_blob

    def selected(path: str, data: bytes) -> bool:
        return {
            "dirty": path.endswith("latest.json") and json.loads(data).get("dirty") is True,
            "value": "/values/messages/v2.bin" in path,
            "metadata": path.endswith("cp-002/metadata.bin"),
            "marker": path.endswith("cp-002/checkpoint.bin"),
            "interrupted": path.endswith("cp-002/checkpoint.bin"),
        }[phase]

    def fail(path: str, data: bytes, metadata: dict[str, str]) -> None:
        if selected(path, data):
            if phase == "interrupted":
                upload(path, data, metadata)
                raise SystemExit("simulated process exit after marker")
            raise TimeoutError(path)
        upload(path, data, metadata)

    async def afail(path: str, data: bytes, metadata: dict[str, str]) -> None:
        if selected(path, data):
            if phase == "interrupted":
                await aupload(path, data, metadata)
                raise SystemExit("simulated process exit after marker")
            raise TimeoutError(path)
        await aupload(path, data, metadata)

    monkeypatch.setattr(saver, "_upload_blob", fail)
    monkeypatch.setattr(saver, "_aupload_blob", afail)
    b = _checkpoint(
        "cp-002", channel_values={"messages": ["B"]}, channel_versions={"messages": "v2"}
    )

    # When: B's write stops and an independent saver reads the shared storage.
    with pytest.raises(SystemExit if phase == "interrupted" else TimeoutError):
        if mode == "sync":
            saver.put(cfg, b, _metadata(), {"messages": "v2"})
        else:
            await saver.aput(cfg, b, _metadata(), {"messages": "v2"})
    reader = AzureBlobCheckpointSaver(container_client=container, aio_container_client=aio)
    latest = reader.get_tuple(cfg) if mode == "sync" else await reader.aget_tuple(cfg)

    # Then: pre-marker failures expose A; process exit after the marker exposes complete B.
    assert latest is not None
    assert latest.checkpoint["id"] == ("cp-002" if phase == "interrupted" else "cp-001")
    explicit = _config(checkpoint_ns="failure", checkpoint_id="cp-002")
    b_result = reader.get_tuple(explicit) if mode == "sync" else await reader.aget_tuple(explicit)
    if phase == "interrupted":
        assert b_result is not None
        assert b_result.checkpoint["channel_values"] == {"messages": ["B"]}
    else:
        assert b_result is None
    if phase == "dirty":
        assert not any("cp-002" in path or "/values/" in path for path in container.blobs)


@pytest.mark.parametrize("mode", ["sync", "native", "fallback"])
@pytest.mark.parametrize(
    "hint",
    [
        None,
        b"not-json",
        b"\xff",
        b"[]",
        b"{}",
        b'{"checkpoint_id":"cp-001"}',
        b'{"version":1,"dirty":true}',
        b'{"version":1,"dirty":false,"checkpoint_id":"missing"}',
    ],
)
async def test_untrusted_hint_reconciles_and_next_writer_repairs_monotonically(
    mode: str, hint: bytes | None
) -> None:
    # Given: two committed markers and a hint that cannot certify latest publication.
    container = MockContainerClient()
    aio = AsyncMockContainerClient(container) if mode == "native" else None
    saver = AzureBlobCheckpointSaver(container_client=container, aio_container_client=aio)
    cfg = _config(checkpoint_ns="hint")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})
    saver.put(cfg, _checkpoint("cp-002"), _metadata(), {})
    path = "threads/thread-1/ns/hint/latest.json"
    if hint is None:
        del container.blobs[path]
    else:
        container.get_blob_client(path).upload_blob(hint, metadata={}, overwrite=True)

    # When: the reader reconciles, then a successful older-ID write completes publication.
    latest = saver.get_tuple(cfg) if mode == "sync" else await saver.aget_tuple(cfg)
    if mode == "sync":
        saver.put(cfg, _checkpoint("cp-000"), _metadata(), {})
    else:
        await saver.aput(cfg, _checkpoint("cp-000"), _metadata(), {})

    # Then: reads recover B and the successful writer repairs without regressing B.
    assert latest is not None
    assert latest.checkpoint["id"] == "cp-002"
    assert json.loads(container.blobs[path].data)["checkpoint_id"] == "cp-002"
    assert json.loads(container.blobs[path].data)["dirty"] is False


@pytest.mark.parametrize("mode", ["sync", "native", "fallback"])
async def test_clean_hint_read_avoids_checkpoint_history_scan(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a clean publication and a listing guard on the authoritative history prefix.
    container = MockContainerClient()
    aio = AsyncMockContainerClient(container) if mode == "native" else None
    saver = AzureBlobCheckpointSaver(container_client=container, aio_container_client=aio)
    cfg = _config(checkpoint_ns="fast")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})
    scan = saver._list_checkpoint_ids
    ascan = saver._alist_checkpoint_ids
    scans: list[str] = []

    def count_scan(thread_id: str, ns: str) -> list[str]:
        scans.append(ns)
        return scan(thread_id, ns)

    async def acount_scan(thread_id: str, ns: str) -> list[str]:
        scans.append(ns)
        return await ascan(thread_id, ns)

    monkeypatch.setattr(saver, "_list_checkpoint_ids", count_scan)
    monkeypatch.setattr(saver, "_alist_checkpoint_ids", acount_scan)

    # When: a healthy latest lookup loads its clean target directly.
    latest = saver.get_tuple(cfg) if mode == "sync" else await saver.aget_tuple(cfg)

    # Then: no full checkpoint listing is needed (pending-write listing remains separate).
    assert latest is not None
    assert latest.checkpoint["id"] == "cp-001"
    assert scans == []


@pytest.mark.parametrize("mid_pass", [False, True])
def test_orphan_collection_stops_for_dirty_publication(
    mid_pass: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an old orphan which a paused writer may reference before committing its marker.
    container = MockContainerClient()
    saver = AzureBlobCheckpointSaver(container_client=container)
    cfg = _config(checkpoint_ns="gc")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})
    value = "threads/thread-1/ns/gc/values/messages/v2.bin"
    container.get_blob_client(value).upload_blob(b"orphan", metadata={}, overwrite=True)
    path = "threads/thread-1/ns/gc/latest.json"
    listing = saver._list_value_blobs

    def dirty() -> None:
        container.get_blob_client(path).upload_blob(
            b'{"version":1,"dirty":true}', metadata={}, overwrite=True
        )

    def dirty_during_listing(
        thread_id: str, ns: str
    ) -> list[tuple[str, tuple[str, str] | None, datetime | None]]:
        values = listing(thread_id, ns)
        dirty()
        return values

    if mid_pass:
        monkeypatch.setattr(saver, "_list_value_blobs", dirty_during_listing)
    else:
        dirty()

    # When: GC encounters publication uncertainty initially or at the per-orphan recheck.
    result = saver.collect_orphaned_values("thread-1", checkpoint_ns="gc", dry_run=False)

    # Then: the old value is preserved and the namespace is explicitly skipped.
    assert value in container.blobs
    assert result.deleted == []
    assert result.skipped_namespaces == [("thread-1", "gc")]


@pytest.mark.parametrize("native", [False, True])
async def test_reader_during_write_cannot_clear_recovery_intent(
    native: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: B's writer pauses before its marker while a fresh reader sees A.
    container = MockContainerClient()
    aio = AsyncMockContainerClient(container) if native else None
    saver = AzureBlobCheckpointSaver(container_client=container, aio_container_client=aio)
    reader = AzureBlobCheckpointSaver(container_client=container, aio_container_client=aio)
    cfg = _config(checkpoint_ns="paused")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})
    upload = saver._upload_blob
    aupload = saver._aupload_blob

    def pause_then_fail(path: str, data: bytes, metadata: dict[str, str]) -> None:
        if path.endswith("cp-002/checkpoint.bin"):
            prior = reader.get_tuple(cfg)
            assert prior is not None
            assert prior.checkpoint["id"] == "cp-001"
        if path.endswith("latest.json") and json.loads(data).get("dirty") is False:
            raise TimeoutError(path)
        upload(path, data, metadata)

    async def apause_then_fail(path: str, data: bytes, metadata: dict[str, str]) -> None:
        if path.endswith("cp-002/checkpoint.bin"):
            prior = await reader.aget_tuple(cfg)
            assert prior is not None
            assert prior.checkpoint["id"] == "cp-001"
        if path.endswith("latest.json") and json.loads(data).get("dirty") is False:
            raise TimeoutError(path)
        await aupload(path, data, metadata)

    monkeypatch.setattr(saver, "_upload_blob", pause_then_fail)
    monkeypatch.setattr(saver, "_aupload_blob", apause_then_fail)

    # When: B resumes and commits, then final clean publication fails.
    with pytest.raises(TimeoutError):
        if native:
            await saver.aput(cfg, _checkpoint("cp-002"), _metadata(), {})
        else:
            saver.put(cfg, _checkpoint("cp-002"), _metadata(), {})

    # Then: the earlier reader did not erase dirty intent and B survives retention.
    reader.delete_old_checkpoints("thread-1", checkpoint_ns="paused", keep_last=1)
    latest = await reader.aget_tuple(cfg) if native else reader.get_tuple(cfg)
    assert latest is not None
    assert latest.checkpoint["id"] == "cp-002"
