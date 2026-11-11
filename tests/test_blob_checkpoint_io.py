"""Request-count and behavior tests for Blob checkpoint read I/O (#860)."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterator
import importlib
import sys
import types
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata
import pytest


class _NotFound(Exception):
    pass


class _Props:
    def __init__(self, metadata: dict[str, str]) -> None:
        self.metadata = metadata


class _Stream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class _Store:
    """Shared blob store with per-operation request counting."""

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.ops: Counter[str] = Counter()
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail_download_suffix: str | None = None


class _SyncClient:
    def __init__(self, store: _Store, name: str) -> None:
        self._s = store
        self._n = name

    def upload_blob(self, data: bytes, metadata: dict[str, str], overwrite: bool) -> None:
        self._s.ops["upload"] += 1
        if not overwrite and self._n in self._s.blobs:
            raise ValueError("exists")
        self._s.blobs[self._n] = (data, dict(metadata))

    def download_blob(self) -> _Stream:
        self._s.ops["download"] += 1
        self._s.in_flight += 1
        self._s.max_in_flight = max(self._s.max_in_flight, self._s.in_flight)
        try:
            if self._n not in self._s.blobs:
                raise _NotFound(self._n)
            return _Stream(self._s.blobs[self._n][0])
        finally:
            self._s.in_flight -= 1

    def get_blob_properties(self) -> _Props:
        self._s.ops["properties"] += 1
        if self._n not in self._s.blobs:
            raise _NotFound(self._n)
        return _Props(dict(self._s.blobs[self._n][1]))

    def delete_blob(self) -> None:
        del self._s.blobs[self._n]


class _Item:
    def __init__(self, name: str) -> None:
        self.name = name
        self.last_modified = None


class _SyncContainer:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def get_blob_client(self, blob: str) -> _SyncClient:
        return _SyncClient(self.store, blob)

    def list_blobs(self, name_starts_with: str = "") -> list[_Item]:
        self.store.ops["list"] += 1
        return [_Item(n) for n in sorted(self.store.blobs) if n.startswith(name_starts_with)]


class _AsyncStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def readall(self) -> bytes:
        return self._data


class _AsyncClient:
    def __init__(self, store: _Store, name: str) -> None:
        self._s = store
        self._n = name

    async def upload_blob(self, data: bytes, metadata: dict[str, str], overwrite: bool) -> None:
        self._s.ops["upload"] += 1
        self._s.blobs[self._n] = (data, dict(metadata))

    async def download_blob(self) -> _AsyncStream:
        self._s.ops["download"] += 1
        self._s.in_flight += 1
        self._s.max_in_flight = max(self._s.max_in_flight, self._s.in_flight)
        try:
            await asyncio.sleep(0.001)
            if self._s.fail_download_suffix and self._n.endswith(self._s.fail_download_suffix):
                raise RuntimeError("boom")
            if self._n not in self._s.blobs:
                raise _NotFound(self._n)
            return _AsyncStream(self._s.blobs[self._n][0])
        finally:
            self._s.in_flight -= 1

    async def get_blob_properties(self) -> _Props:
        self._s.ops["properties"] += 1
        if self._n not in self._s.blobs:
            raise _NotFound(self._n)
        return _Props(dict(self._s.blobs[self._n][1]))

    async def delete_blob(self) -> None:
        del self._s.blobs[self._n]


class _AsyncContainer:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def get_blob_client(self, blob: str) -> _AsyncClient:
        return _AsyncClient(self.store, blob)

    async def list_blobs(self, name_starts_with: str = "") -> Any:
        self.store.ops["list"] += 1
        for n in sorted(self.store.blobs):
            if n.startswith(name_starts_with):
                yield _Item(n)


@pytest.fixture(autouse=True)
def _fake_azure(monkeypatch: Any) -> None:
    blob_mod = types.ModuleType("azure.storage.blob")
    aio_mod = types.ModuleType("azure.storage.blob.aio")
    setattr(blob_mod, "ContainerClient", _SyncContainer)
    setattr(aio_mod, "ContainerClient", _AsyncContainer)
    setattr(blob_mod, "aio", aio_mod)
    exc_mod = types.ModuleType("azure.core.exceptions")
    setattr(exc_mod, "ResourceNotFoundError", _NotFound)
    for name, mod in (
        ("azure", types.ModuleType("azure")),
        ("azure.storage", types.ModuleType("azure.storage")),
        ("azure.storage.blob", blob_mod),
        ("azure.storage.blob.aio", aio_mod),
        ("azure.core", types.ModuleType("azure.core")),
        ("azure.core.exceptions", exc_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


def _saver() -> tuple[Any, _Store]:
    module = importlib.import_module("azure_functions_langgraph.checkpointers.azure_blob")
    store = _Store()
    saver = module.AzureBlobCheckpointSaver(
        container_client=_SyncContainer(store), aio_container_client=_AsyncContainer(store)
    )
    return saver, store


def _cfg(checkpoint_id: str | None = None) -> RunnableConfig:
    cfg: RunnableConfig = {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}
    if checkpoint_id:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return cfg


def _cp(cp_id: str, channels: int) -> Checkpoint:
    return {
        "v": 1,
        "id": cp_id,
        "ts": "2026-01-01T00:00:00Z",
        "channel_values": {f"ch{i}": f"{cp_id}-v{i}" for i in range(channels)},
        "channel_versions": {f"ch{i}": f"{cp_id}.{i}" for i in range(channels)},
        "versions_seen": {},
        "updated_channels": None,
    }


def _md(step: int) -> CheckpointMetadata:
    return {"source": "loop", "step": step, "run_id": "r", "parents": {}}


def _seed(saver: Any, count: int, channels: int, writes: int) -> None:
    for i in range(count):
        cp = _cp(f"cp-{i:03d}", channels)
        saver.put(_cfg(), cp, _md(i), dict(cp["channel_versions"]))
        saver.put_writes(
            _cfg(cp["id"]),
            [(f"w{j}", f"{cp['id']}-w{j}") for j in range(writes)],
            task_id="task",
        )


def _ids(items: Iterator[Any] | list[Any]) -> list[str]:
    return [i.checkpoint["id"] for i in items]


def test_get_tuple_request_count_is_stable_and_results_ordered() -> None:
    saver, store = _saver()
    _seed(saver, 1, channels=12, writes=5)
    store.ops.clear()
    result = saver.get_tuple(_cfg("cp-000"))
    assert result is not None
    assert list(result.checkpoint["channel_values"]) == [f"ch{i}" for i in range(12)]
    assert result.pending_writes == [("task", f"w{j}", f"cp-000-w{j}") for j in range(5)]
    # Parallelism must not change the request count (1 checkpoint + 1 metadata
    # + 12 values + 5 writes downloads, each paired with a properties call).
    assert store.ops["download"] == 19
    assert store.ops["properties"] == 19
    assert store.ops["list"] == 1


def test_sync_reads_are_concurrent_but_bounded() -> None:
    saver, store = _saver()
    _seed(saver, 1, channels=40, writes=0)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.azure_blob")
    assert saver.get_tuple(_cfg("cp-000")) is not None
    assert store.max_in_flight <= module._MAX_CONCURRENT_BLOB_READS


async def test_async_reads_are_concurrent_bounded_and_ordered() -> None:
    saver, store = _saver()
    _seed(saver, 1, channels=40, writes=6)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.azure_blob")
    store.max_in_flight = 0
    result = await saver.aget_tuple(_cfg("cp-000"))
    assert result is not None
    assert list(result.checkpoint["channel_values"]) == [f"ch{i}" for i in range(40)]
    assert result.pending_writes == [("task", f"w{j}", f"cp-000-w{j}") for j in range(6)]
    assert 1 < store.max_in_flight <= module._MAX_CONCURRENT_BLOB_READS
    sync_result = saver.get_tuple(_cfg("cp-000"))
    assert sync_result is not None
    assert sync_result.checkpoint == result.checkpoint
    assert sync_result.pending_writes == result.pending_writes


async def test_async_read_failure_cancels_sibling_downloads() -> None:
    saver, store = _saver()
    _seed(saver, 1, channels=20, writes=0)
    store.fail_download_suffix = next(n for n in store.blobs if "ch3" in n and "values" in n)
    with pytest.raises(RuntimeError, match="boom"):
        await saver.aget_tuple(_cfg("cp-000"))
    await asyncio.sleep(0.01)
    assert store.in_flight == 0


async def test_async_read_cancellation_leaves_no_inflight_tasks() -> None:
    saver, store = _saver()
    _seed(saver, 1, channels=30, writes=0)
    task = asyncio.ensure_future(saver.aget_tuple(_cfg("cp-000")))
    await asyncio.sleep(0.003)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.01)
    assert store.in_flight == 0


def test_list_limit_zero_makes_no_requests() -> None:
    saver, store = _saver()
    _seed(saver, 3, channels=3, writes=2)
    store.ops.clear()
    assert list(saver.list(_cfg(), limit=0)) == []
    assert sum(store.ops.values()) == 0


async def test_alist_limit_zero_makes_no_requests() -> None:
    saver, store = _saver()
    _seed(saver, 3, channels=3, writes=2)
    store.ops.clear()
    assert [i async for i in saver.alist(_cfg(), limit=0)] == []
    assert sum(store.ops.values()) == 0


def test_list_does_not_build_checkpoint_beyond_limit() -> None:
    saver, store = _saver()
    _seed(saver, 5, channels=4, writes=2)
    store.ops.clear()
    items = list(saver.list(_cfg(), limit=2))
    assert len(items) == 2
    # Each built tuple: checkpoint + metadata + 4 values + 2 writes = 8 downloads.
    assert store.ops["download"] == 16


async def test_alist_does_not_build_checkpoint_beyond_limit() -> None:
    saver, store = _saver()
    _seed(saver, 5, channels=4, writes=2)
    store.ops.clear()
    items = [i async for i in saver.alist(_cfg(), limit=2)]
    assert len(items) == 2
    assert store.ops["download"] == 16


def test_list_filter_skips_full_build_for_non_matching() -> None:
    saver, store = _saver()
    _seed(saver, 5, channels=4, writes=2)
    store.ops.clear()
    assert list(saver.list(_cfg(), filter={"step": 99})) == []
    # Only the 5 metadata blobs are downloaded.
    assert store.ops["download"] == 5


async def test_alist_filter_skips_full_build_for_non_matching() -> None:
    saver, store = _saver()
    _seed(saver, 5, channels=4, writes=2)
    store.ops.clear()
    assert [i async for i in saver.alist(_cfg(), filter={"step": 99})] == []
    assert store.ops["download"] == 5


def test_list_filter_match_does_not_redownload_metadata() -> None:
    saver, store = _saver()
    _seed(saver, 3, channels=2, writes=1)
    store.ops.clear()
    items = list(saver.list(_cfg(), filter={"step": 1}))
    assert _ids(items) == ["cp-001"]
    # 3 metadata probes + (checkpoint + 2 values + 1 write) for the one match.
    assert store.ops["download"] == 3 + 4
    assert items[0].metadata["step"] == 1


def test_list_ordering_filter_before_limit_semantics_preserved() -> None:
    saver, _ = _saver()
    _seed(saver, 5, channels=2, writes=0)
    everything = _ids(saver.list(_cfg()))
    assert everything == sorted(everything, reverse=True)
    assert _ids(saver.list(_cfg(), limit=3)) == everything[:3]
    assert _ids(saver.list(_cfg(), filter={"run_id": "r"}, limit=2)) == everything[:2]
    assert _ids(saver.list(_cfg(), before=_cfg("cp-003"), limit=2)) == ["cp-002", "cp-001"]


async def test_alist_matches_list() -> None:
    saver, _ = _saver()
    _seed(saver, 5, channels=2, writes=1)
    expected = _ids(saver.list(_cfg(), filter={"run_id": "r"}, limit=4))
    got = _ids([i async for i in saver.alist(_cfg(), filter={"run_id": "r"}, limit=4)])
    assert got == expected


def test_list_skips_checkpoint_with_missing_metadata_under_filter() -> None:
    saver, store = _saver()
    _seed(saver, 2, channels=1, writes=0)
    meta_name = next(n for n in store.blobs if "cp-000" in n and "metadata" in n)
    del store.blobs[meta_name]
    assert _ids(saver.list(_cfg(), filter={"run_id": "r"})) == ["cp-001"]
    assert _ids(saver.list(_cfg())) == ["cp-001"]
