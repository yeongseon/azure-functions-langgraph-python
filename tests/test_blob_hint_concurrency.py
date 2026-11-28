"""Deterministic ETag schedules over shared fake storage, not Azure evidence."""

from __future__ import annotations

import json
from typing import Any

import pytest

from azure_functions_langgraph.checkpointers.azure_blob import (
    AzureBlobCheckpointSaver,
    BlobCheckpointConflictError,
)
from azure_functions_langgraph.locks.inprocess import InProcessThreadLock
from tests.test_blob_checkpoint_io import (
    _AsyncContainer,
    _cfg,
    _cp,
    _md,
    _saver,
    _Store,
    _SyncContainer,
)
from tests.test_blob_checkpoint_io import _fake_azure as _fake_azure

pytestmark = pytest.mark.usefixtures("_fake_azure")
HINT = "threads/t/ns//latest.json"


def fresh(store: _Store, mode: str) -> AzureBlobCheckpointSaver:
    return AzureBlobCheckpointSaver(
        container_client=_SyncContainer(store),
        aio_container_client=_AsyncContainer(store) if mode == "native" else None,
    )


async def put(saver: AzureBlobCheckpointSaver, checkpoint_id: str, mode: str) -> None:
    cp = _cp(checkpoint_id, 1)
    if mode == "sync":
        saver.put(_cfg(), cp, _md(0), cp["channel_versions"])
    else:
        await saver.aput(_cfg(), cp, _md(0), cp["channel_versions"])


@pytest.mark.parametrize("mode", ["sync", "native", "fallback"])
@pytest.mark.parametrize("first_id", ["cp-001", "cp-002"])
@pytest.mark.parametrize("pause", ["dirty", "marker", "clean"])
@pytest.mark.parametrize("second_hint_fails", [False, True])
async def test_overlap_is_reported_and_committed_max_remains_readable(
    mode: str,
    first_id: str,
    pause: str,
    second_hint_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _saver()
    first, second = fresh(store, mode), fresh(store, mode)
    second_id = "cp-002" if first_id == "cp-001" else "cp-001"
    upload, aupload = first._upload_blob, first._aupload_blob
    fired = False

    def selected(path: str, data: bytes) -> bool:
        if pause == "marker":
            return path.endswith("checkpoint.bin")
        return path == HINT and json.loads(data)["dirty"] == (pause == "dirty")

    def fail_clean(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
        if path == HINT and json.loads(data)["dirty"] is False:
            raise TimeoutError("clean hint rejected")
        return second_upload(path, data, metadata, **kwargs)

    async def afail_clean(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
        if path == HINT and json.loads(data)["dirty"] is False:
            raise TimeoutError("clean hint rejected")
        return await second_aupload(path, data, metadata, **kwargs)

    second_upload, second_aupload = second._upload_blob, second._aupload_blob
    if second_hint_fails:
        monkeypatch.setattr(second, "_upload_blob", fail_clean)
        monkeypatch.setattr(second, "_aupload_blob", afail_clean)

    def overlap(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
        nonlocal fired
        result = upload(path, data, metadata, **kwargs) if pause == "dirty" else None
        if not fired and selected(path, data):
            fired = True
            cp = _cp(second_id, 1)
            if second_hint_fails:
                with pytest.raises(TimeoutError, match="clean hint rejected"):
                    second.put(_cfg(), cp, _md(0), cp["channel_versions"])
            else:
                second.put(_cfg(), cp, _md(0), cp["channel_versions"])
        return result if result is not None else upload(path, data, metadata, **kwargs)

    async def aoverlap(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
        nonlocal fired
        result = await aupload(path, data, metadata, **kwargs) if pause == "dirty" else None
        if not fired and selected(path, data):
            fired = True
            if second_hint_fails:
                with pytest.raises(TimeoutError, match="clean hint rejected"):
                    await put(second, second_id, "native")
            else:
                await put(second, second_id, "native")
        return result if result is not None else await aupload(path, data, metadata, **kwargs)

    monkeypatch.setattr(first, "_upload_blob", overlap)
    monkeypatch.setattr(first, "_aupload_blob", aoverlap)
    with pytest.raises(BlobCheckpointConflictError):
        await put(first, first_id, mode)

    reader = fresh(store, mode)
    latest = await reader.aget_tuple(_cfg()) if mode == "native" else reader.get_tuple(_cfg())
    assert fired and latest is not None and latest.checkpoint["id"] == "cp-002"
    assert [item.checkpoint["id"] for item in reader.list(_cfg())] == ["cp-002", "cp-001"]
    for cp_id in ("cp-001", "cp-002"):
        item = reader.get_tuple(_cfg(cp_id))
        assert item is not None and item.checkpoint == _cp(cp_id, 1)
    assert json.loads(store.blobs[HINT][0]) == {"version": 1, "dirty": True}


@pytest.mark.parametrize("mode", ["sync", "native", "fallback"])
@pytest.mark.parametrize("existing", [False, True])
async def test_stale_preflight_is_rejected_before_any_checkpoint_data(
    mode: str,
    existing: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _saver()
    first, second = fresh(store, mode), fresh(store, mode)
    if existing:
        await put(second, "cp-000", mode)
    snapshot, asnapshot = first._hint_snapshot, first._ahint_snapshot

    def stale(path: str) -> Any:
        result = snapshot(path)
        cp = _cp("cp-002", 1)
        second.put(_cfg(), cp, _md(0), cp["channel_versions"])
        return result

    async def astale(path: str) -> Any:
        result = await asnapshot(path)
        await put(second, "cp-002", "native")
        return result

    monkeypatch.setattr(first, "_hint_snapshot", stale)
    monkeypatch.setattr(first, "_ahint_snapshot", astale)
    with pytest.raises(BlobCheckpointConflictError):
        await put(first, "cp-001", mode)
    assert not any("cp-001" in name for name in store.blobs)
    latest = second.get_tuple(_cfg())
    assert latest is not None and latest.checkpoint["id"] == "cp-002"


@pytest.mark.parametrize("mode", ["sync", "native", "fallback"])
@pytest.mark.parametrize("accepted_clean", [False, True])
async def test_next_lock_holder_recovers_failed_publication_and_restores_fast_path(
    mode: str,
    accepted_clean: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _saver()
    previous = fresh(store, mode)
    lock = InProcessThreadLock()
    upload, aupload = previous._upload_blob, previous._aupload_blob

    def fail(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
        if path == HINT and json.loads(data)["dirty"] is False:
            if accepted_clean:
                upload(path, data, metadata, **kwargs)
            raise TimeoutError("publication response lost")
        return upload(path, data, metadata, **kwargs)

    async def afail(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
        if path == HINT and json.loads(data)["dirty"] is False:
            if accepted_clean:
                await aupload(path, data, metadata, **kwargs)
            raise TimeoutError("publication response lost")
        return await aupload(path, data, metadata, **kwargs)

    monkeypatch.setattr(previous, "_upload_blob", fail)
    monkeypatch.setattr(previous, "_aupload_blob", afail)
    token = lock.acquire("g", "t")
    assert token is not None
    try:
        with pytest.raises(TimeoutError, match="publication response lost"):
            await put(previous, "cp-002", mode)
    finally:
        lock.release("g", "t", token)

    successor = fresh(store, mode)
    token = lock.acquire("g", "t")
    assert token is not None and lock.acquire("g", "t") is None
    try:
        await put(successor, "cp-001", mode)
    finally:
        lock.release("g", "t", token)
    assert json.loads(store.blobs[HINT][0]) == {
        "version": 1,
        "dirty": False,
        "checkpoint_id": "cp-002",
    }
    store.ops.clear()
    latest = await successor.aget_tuple(_cfg()) if mode == "native" else successor.get_tuple(_cfg())
    assert latest is not None and latest.checkpoint["id"] == "cp-002"
    assert store.ops["list"] == 1, "only pending writes, no checkpoint history scan"


@pytest.mark.parametrize("mode", ["sync", "native", "fallback"])
@pytest.mark.parametrize("failure", ["unavailable", "future-format", "changed-again"])
async def test_conflict_cleanup_never_silently_claims_success(
    mode: str,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _saver()
    saver, other = fresh(store, mode), fresh(store, mode)
    upload, aupload = saver._upload_blob, saver._aupload_blob
    fired = False
    cleanup = False

    def replace() -> None:
        data = b'{"version":2}' if failure == "future-format" else b'{"version":1,"dirty":true}'
        _SyncContainer(store).get_blob_client(HINT).upload_blob(data, {}, True)

    def intercept(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
        nonlocal fired, cleanup
        if path == HINT and json.loads(data)["dirty"] is False and not fired:
            fired = True
            cp = _cp("cp-001", 1)
            other.put(_cfg(), cp, _md(0), cp["channel_versions"])
            if failure == "future-format":
                replace()
        elif fired and path == HINT and json.loads(data)["dirty"] is True:
            cleanup = True
            if failure == "unavailable":
                raise TimeoutError("cleanup unavailable")
            replace()
        return upload(path, data, metadata, **kwargs)

    async def aintercept(path: str, data: bytes, metadata: dict[str, str], **kwargs: Any) -> str:
        nonlocal fired, cleanup
        if path == HINT and json.loads(data)["dirty"] is False and not fired:
            fired = True
            await put(other, "cp-001", "native")
            if failure == "future-format":
                replace()
        elif fired and path == HINT and json.loads(data)["dirty"] is True:
            cleanup = True
            if failure == "unavailable":
                raise TimeoutError("cleanup unavailable")
            replace()
        return await aupload(path, data, metadata, **kwargs)

    monkeypatch.setattr(saver, "_upload_blob", intercept)
    monkeypatch.setattr(saver, "_aupload_blob", aintercept)
    with pytest.raises(TimeoutError if failure == "unavailable" else BlobCheckpointConflictError):
        await put(saver, "cp-002", mode)
    assert fired and cleanup == (failure != "future-format")
    committed = other.get_tuple(_cfg("cp-002"))
    assert committed is not None and committed.checkpoint == _cp("cp-002", 1)
    if failure == "future-format":
        assert store.blobs[HINT][0] == b'{"version":2}'
