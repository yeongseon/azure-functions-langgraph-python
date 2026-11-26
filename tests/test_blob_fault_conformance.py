"""Offline fault/retry contracts; shared fakes are not Azure certification."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from typing import Any

import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from tests.test_blob_checkpoint_io import (
    _AsyncClient,
    _cfg,
    _cp,
    _md,
    _saver,
    _SyncClient,
    _write_path,
)
from tests.test_blob_checkpoint_io import _fake_azure as _fake_azure

pytestmark = pytest.mark.usefixtures("_fake_azure")


async def _read_both(saver: AzureBlobCheckpointSaver) -> None:
    sync = saver.get_tuple(_cfg())
    native = await saver.aget_tuple(_cfg())
    assert sync is not None and native == sync
    assert sync.checkpoint == _cp("cp-000", 2)
    assert sync.metadata == _md(0)
    assert sync.pending_writes == [("task", "a", "original"), ("task", "b", "complete")]
    assert [item.checkpoint["id"] for item in saver.list(_cfg())] == ["cp-000"]
    assert [item.checkpoint["id"] async for item in saver.alist(_cfg())] == ["cp-000"]


@pytest.mark.parametrize("native_async", [False, True])
async def test_racing_slot_creates_keep_first_accepted_value_across_apis(
    native_async: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping slot creates return successfully and retain the first accepted value."""
    saver, store = _saver()
    cp = _cp("cp-000", 0)
    saver.put(_cfg(), cp, _md(0), {})
    upload, aupload = _SyncClient.upload_blob, _AsyncClient.upload_blob
    barrier = threading.Barrier(2)
    ready = asyncio.Event()
    arrivals = 0
    accepted: list[str] = []

    def racing(client: _SyncClient, data: bytes, metadata: dict[str, str], overwrite: bool) -> None:
        barrier.wait(timeout=5)
        with store.lock:
            upload(client, data, metadata, overwrite)
            accepted.append(saver.serde.loads_typed((metadata["serde_type"], data)))

    async def aracing(
        client: _AsyncClient, data: bytes, metadata: dict[str, str], overwrite: bool
    ) -> None:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        await aupload(client, data, metadata, overwrite)
        accepted.append(saver.serde.loads_typed((metadata["serde_type"], data)))

    monkeypatch.setattr(_SyncClient, "upload_blob", racing)
    monkeypatch.setattr(_AsyncClient, "upload_blob", aracing)
    if native_async:
        await asyncio.gather(
            saver.aput_writes(_cfg("cp-000"), [("a", "A")], "task"),
            saver.aput_writes(_cfg("cp-000"), [("a", "B")], "task"),
        )
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(saver.put_writes, _cfg("cp-000"), [("a", value)], "task")
                for value in ("A", "B")
            ]
            for future in futures:
                future.result(timeout=5)
    assert len(accepted) == 1 and accepted[0] in {"A", "B"}
    sync = saver.get_tuple(_cfg())
    assert sync is not None and sync.pending_writes == [("task", "a", accepted[0])]
    assert await saver.aget_tuple(_cfg()) == sync


@pytest.mark.parametrize("native_async", [False, True])
@pytest.mark.parametrize("phase", ["slot", "marker"])
async def test_accepted_upload_timeout_retry_preserves_slots_and_repairs_hint(
    native_async: bool, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accepted slot/marker timeout propagates; retry completes without replacing old slots."""
    saver, store = _saver()
    cp = _cp("cp-000", 2)
    if phase == "slot":
        saver.put(_cfg(), cp, _md(0), cp["channel_versions"])
    else:
        saver.put_writes(_cfg("cp-000"), [("a", "original"), ("b", "complete")], "task")
    upload, aupload = _SyncClient.upload_blob, _AsyncClient.upload_blob
    fault = TimeoutError("accepted but response lost")
    target = _write_path(0) if phase == "slot" else "checkpoint.bin"
    fired = False

    def fail(
        client: _SyncClient, data: bytes, metadata: dict[str, str], overwrite: bool, **kwargs: Any
    ) -> dict[str, str]:
        nonlocal fired
        result = upload(client, data, metadata, overwrite, **kwargs)
        if not fired and client._n.endswith(target):
            fired = True
            raise fault
        return result

    async def afail(
        client: _AsyncClient,
        data: bytes,
        metadata: dict[str, str],
        overwrite: bool,
        **kwargs: Any,
    ) -> dict[str, str]:
        nonlocal fired
        result = await aupload(client, data, metadata, overwrite, **kwargs)
        if not fired and client._n.endswith(target):
            fired = True
            raise fault
        return result

    async def attempt(retry: bool) -> None:
        if phase == "slot":
            values = [("a", "replacement" if retry else "original"), ("b", "complete")]
            if native_async:
                await saver.aput_writes(_cfg("cp-000"), values, "task")
            else:
                saver.put_writes(_cfg("cp-000"), values, "task")
        elif native_async:
            await saver.aput(_cfg(), cp, _md(0), cp["channel_versions"])
        else:
            saver.put(_cfg(), cp, _md(0), cp["channel_versions"])

    monkeypatch.setattr(_SyncClient, "upload_blob", fail)
    monkeypatch.setattr(_AsyncClient, "upload_blob", afail)
    with pytest.raises(TimeoutError) as caught:
        await attempt(False)
    assert caught.value is fault and fired
    first_slot = store.blobs[_write_path(0)]
    if phase == "marker":
        assert json.loads(store.blobs["threads/t/ns//latest.json"][0])["dirty"] is True
    await attempt(True)
    assert store.blobs[_write_path(0)] == first_slot
    assert json.loads(store.blobs["threads/t/ns//latest.json"][0]) == {
        "version": 1,
        "dirty": False,
        "checkpoint_id": "cp-000",
    }
    await _read_both(saver)


@pytest.mark.parametrize("after_marker", [False, True])
async def test_cancelled_native_publication_retry_recovers_complete_cross_api_tuple(
    after_marker: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation around the marker exposes only committed state; retry publishes cleanly."""
    saver, store = _saver()
    cp = _cp("cp-000", 2)
    saver.put_writes(_cfg("cp-000"), [("a", "original"), ("b", "complete")], "task")
    upload = _AsyncClient.upload_blob
    entered, release = asyncio.Event(), asyncio.Event()
    blocked = False

    async def paused(
        client: _AsyncClient,
        data: bytes,
        metadata: dict[str, str],
        overwrite: bool,
        **kwargs: Any,
    ) -> dict[str, str]:
        nonlocal blocked
        if client._n.endswith("checkpoint.bin") and not blocked:
            blocked = True
            if after_marker:
                await upload(client, data, metadata, overwrite, **kwargs)
            entered.set()
            await release.wait()
        return await upload(client, data, metadata, overwrite, **kwargs)

    monkeypatch.setattr(_AsyncClient, "upload_blob", paused)
    task = asyncio.create_task(saver.aput(_cfg(), cp, _md(0), cp["channel_versions"]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    sync = saver.get_tuple(_cfg())
    assert await saver.aget_tuple(_cfg()) == sync
    if after_marker:
        assert sync is not None and sync.checkpoint == cp
    else:
        assert sync is None
        assert saver.get_tuple(_cfg("cp-000")) is None
        assert await saver.aget_tuple(_cfg("cp-000")) is None
    assert json.loads(store.blobs["threads/t/ns//latest.json"][0])["dirty"] is True
    await saver.aput(_cfg(), cp, _md(0), cp["channel_versions"])
    assert json.loads(store.blobs["threads/t/ns//latest.json"][0])["dirty"] is False
    await _read_both(saver)


@pytest.mark.parametrize("after_marker", [False, True])
async def test_sync_keyboard_interrupt_retry_recovers_complete_cross_api_tuple(
    after_marker: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KeyboardInterrupt after metadata/marker propagates and retry restores clean publication."""
    saver, store = _saver()
    cp = _cp("cp-000", 2)
    saver.put_writes(_cfg("cp-000"), [("a", "original"), ("b", "complete")], "task")
    upload = _SyncClient.upload_blob
    fault = KeyboardInterrupt("publication interrupted")
    target = "checkpoint.bin" if after_marker else "metadata.bin"
    fired = False

    def interrupted(
        client: _SyncClient,
        data: bytes,
        metadata: dict[str, str],
        overwrite: bool,
        **kwargs: Any,
    ) -> dict[str, str]:
        nonlocal fired
        result = upload(client, data, metadata, overwrite, **kwargs)
        if not fired and client._n.endswith(target):
            fired = True
            raise fault
        return result

    monkeypatch.setattr(_SyncClient, "upload_blob", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        saver.put(_cfg(), cp, _md(0), cp["channel_versions"])
    assert caught.value is fault
    sync = saver.get_tuple(_cfg())
    assert await saver.aget_tuple(_cfg()) == sync
    if after_marker:
        assert sync is not None and sync.checkpoint == cp
    else:
        assert sync is None
    assert json.loads(store.blobs["threads/t/ns//latest.json"][0])["dirty"] is True
    saver.put(_cfg(), cp, _md(0), cp["channel_versions"])
    assert json.loads(store.blobs["threads/t/ns//latest.json"][0])["dirty"] is False
    await _read_both(saver)
