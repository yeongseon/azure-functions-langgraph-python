"""Request-count (and optional simulated-latency) benchmark for ``AzureBlobCheckpointSaver``.

What this measures
------------------
Every number comes from a counting in-memory stand-in for the Azure Blob container
clients. It counts **requests the saver issues**, by kind:

``GET``   ``download_blob()`` (one request for payloads below the SDK's single-get size)
``HEAD``  ``get_blob_properties()``
``PUT``   ``upload_blob(..., overwrite=True)``
``PUT*``  ``upload_blob(..., overwrite=False)`` (create-only, "If-None-Match: *")
``LIST``  ``list_blobs()``, modeled as one request per started page of 5000 results
``DEL``   ``delete_blob()``

It models NO network, service, throttling or retry behavior and is not Azure Storage:
request counts are a property of the saver's call pattern, not a performance claim.
``--latency-ms N`` makes every request sleep N ms (``time.sleep`` for the sync API,
``asyncio.sleep`` for the async API) so the effect of bounded read parallelism shows up in
the ``simulated_ms`` column. That column is a toy model of a fixed per-request latency;
do not quote it as a wall-clock or Azure measurement.

Usage (repo root, with the ``azure-blob`` extra installed)::

    PYTHONPATH=src python tools/benchmark_blob_checkpointer.py
    PYTHONPATH=src python tools/benchmark_blob_checkpointer.py --channels 40 --writes 20 --json
    PYTHONPATH=src python tools/benchmark_blob_checkpointer.py --latency-ms 20

To compare two revisions, run the same script with ``PYTHONPATH`` pointing at each
revision's ``src`` directory and compare the printed counts.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Callable
import json
import sys
import time
from typing import Any

PAGE_SIZE = 5000  # List Blobs maximum results per response page
KINDS = ("GET", "HEAD", "PUT", "PUT*", "LIST", "DEL")

Blobs = dict[str, tuple[bytes, dict[str, str]]]


class Counters:
    """Per-kind request counters shared by one sync and one async container."""

    def __init__(self, latency_ms: float = 0.0) -> None:
        self.counts: Counter[str] = Counter()
        self.latency_ms = latency_ms

    def reset(self) -> None:
        self.counts.clear()

    def total(self) -> int:
        return sum(self.counts.values())

    def snapshot(self) -> dict[str, int]:
        return {kind: self.counts.get(kind, 0) for kind in KINDS}


class _Props:
    def __init__(self, metadata: dict[str, str]) -> None:
        self.metadata = metadata


class _Stream:
    """Mirrors ``StorageStreamDownloader``: body via ``readall`` and ``.properties``.

    In ``azure-storage-blob`` the downloader's ``properties`` (a ``BlobProperties``
    including ``metadata``) is built from the headers of the *same* GET that carries the
    body, so reading it costs no extra request.
    """

    def __init__(self, data: bytes, metadata: dict[str, str]) -> None:
        self._data = data
        self.properties = _Props(dict(metadata))

    def readall(self) -> bytes:
        return self._data


class _AStream(_Stream):
    async def readall(self) -> bytes:  # type: ignore[override]
        return self._data


class _Item:
    def __init__(self, name: str) -> None:
        self.name = name
        self.last_modified = None


def make_counting_containers(
    sync_base: type,
    async_base: type,
    not_found: type[BaseException],
    exists: type[BaseException],
    counters: Counters,
) -> tuple[type, type]:
    """Build counting in-memory sync/async containers deriving from the given bases."""

    def tick(kind: str, n: int = 1) -> None:
        counters.counts[kind] += n

    def _put(
        blobs: Blobs, name: str, data: bytes, metadata: dict[str, str], overwrite: bool
    ) -> None:
        if not overwrite and name in blobs:
            tick("PUT*")
            raise exists(name)
        tick("PUT" if overwrite else "PUT*")
        blobs[name] = (bytes(data), dict(metadata))

    class _Client:
        def __init__(self, blobs: Blobs, name: str) -> None:
            self._b, self._n = blobs, name

        def _sleep(self) -> None:
            if counters.latency_ms:
                time.sleep(counters.latency_ms / 1000)

        def upload_blob(self, data: bytes, metadata: dict[str, str], overwrite: bool) -> None:
            self._sleep()
            _put(self._b, self._n, data, metadata, overwrite)

        def download_blob(self) -> _Stream:
            self._sleep()
            tick("GET")
            if self._n not in self._b:
                raise not_found(self._n)
            data, metadata = self._b[self._n]
            return _Stream(data, metadata)

        def get_blob_properties(self) -> _Props:
            self._sleep()
            tick("HEAD")
            if self._n not in self._b:
                raise not_found(self._n)
            return _Props(dict(self._b[self._n][1]))

        def delete_blob(self) -> None:
            self._sleep()
            tick("DEL")
            if self._n not in self._b:
                raise not_found(self._n)
            del self._b[self._n]

    class _AClient:
        def __init__(self, blobs: Blobs, name: str) -> None:
            self._b, self._n = blobs, name

        async def _sleep(self) -> None:
            if counters.latency_ms:
                await asyncio.sleep(counters.latency_ms / 1000)

        async def upload_blob(self, data: bytes, metadata: dict[str, str], overwrite: bool) -> None:
            await self._sleep()
            _put(self._b, self._n, data, metadata, overwrite)

        async def download_blob(self) -> _AStream:
            await self._sleep()
            tick("GET")
            if self._n not in self._b:
                raise not_found(self._n)
            data, metadata = self._b[self._n]
            return _AStream(data, metadata)

        async def get_blob_properties(self) -> _Props:
            await self._sleep()
            tick("HEAD")
            if self._n not in self._b:
                raise not_found(self._n)
            return _Props(dict(self._b[self._n][1]))

        async def delete_blob(self) -> None:
            await self._sleep()
            tick("DEL")
            if self._n not in self._b:
                raise not_found(self._n)
            del self._b[self._n]

    def _list(blobs: Blobs, prefix: str) -> list[_Item]:
        names = [n for n in sorted(blobs) if n.startswith(prefix)]
        tick("LIST", max(1, -(-len(names) // PAGE_SIZE)))
        return [_Item(n) for n in names]

    class _AList:
        def __init__(self, items: list[_Item]) -> None:
            self._items = items

        def __aiter__(self) -> _AList:
            self._it = iter(self._items)
            return self

        async def __anext__(self) -> _Item:
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

    class SyncContainer(sync_base):  # type: ignore[misc]
        def __init__(self, blobs: Blobs) -> None:
            self._blobs = blobs

        def get_blob_client(self, blob: str) -> _Client:
            return _Client(self._blobs, blob)

        def list_blobs(self, name_starts_with: str = "", **_: Any) -> list[_Item]:
            return _list(self._blobs, name_starts_with)

    class AsyncContainer(async_base):  # type: ignore[misc]
        def __init__(self, blobs: Blobs) -> None:
            self._blobs = blobs

        def get_blob_client(self, blob: str) -> _AClient:
            return _AClient(self._blobs, blob)

        def list_blobs(self, name_starts_with: str = "", **_: Any) -> _AList:
            return _AList(_list(self._blobs, name_starts_with))

    return SyncContainer, AsyncContainer


# --------------------------------------------------------------------------
# workload
# --------------------------------------------------------------------------

THREAD = "bench-thread"


def _config(checkpoint_id: str | None = None) -> dict[str, Any]:
    configurable: dict[str, Any] = {"thread_id": THREAD, "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _checkpoint(n: int, channels: int) -> dict[str, Any]:
    versions = {f"ch{i}": f"{n:032}.{i:016d}" for i in range(channels)}
    return {
        "v": 1,
        "id": f"1f000000-0000-6000-8000-{n:012d}",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {f"ch{i}": {"n": n, "i": i} for i in range(channels)},
        "channel_versions": versions,
        "versions_seen": {},
        "updated_channels": None,
    }


def seed(saver: Any, history: int, channels: int, writes: int) -> str:
    """Write ``history`` checkpoints (the last with ``writes`` pending writes); return last id."""
    prev: dict[str, Any] | None = None
    last = ""
    for n in range(1, history + 1):
        cp = _checkpoint(n, channels)
        config = prev if prev is not None else _config()
        prev = saver.put(
            config, cp, {"source": "loop", "step": n, "parents": {}}, cp["channel_versions"]
        )
        last = cp["id"]
    saver.put_writes(
        _config(last),
        [(f"w{i}", {"w": i}) for i in range(writes)],
        "task-seed",
        "~__pregel_pull, n",
    )
    return last


def run(
    channels: int, writes: int, history: int, latency_ms: float, async_api: bool
) -> list[dict[str, Any]]:
    """Run the workload through the sync or the async API and return per-scenario counts."""
    import importlib

    blob_mod = importlib.import_module("azure.storage.blob")
    aio_mod = importlib.import_module("azure.storage.blob.aio")
    exc_mod = importlib.import_module("azure.core.exceptions")
    saver_mod = importlib.import_module("azure_functions_langgraph.checkpointers.azure_blob")

    counters = Counters()
    sync_cls, async_cls = make_counting_containers(
        blob_mod.ContainerClient,
        aio_mod.ContainerClient,
        exc_mod.ResourceNotFoundError,
        exc_mod.ResourceExistsError,
        counters,
    )
    blobs: Blobs = {}
    saver = saver_mod.AzureBlobCheckpointSaver(
        container_client=sync_cls(blobs), aio_container_client=async_cls(blobs)
    )
    last = seed(saver, history, channels, writes)
    counters.latency_ms = latency_ms
    new_cp = _checkpoint(history + 1, channels)
    batch = [(f"n{i}", {"n": i}) for i in range(writes)]

    async def alist(limit: int | None, flt: dict[str, Any] | None = None) -> int:
        return len([t async for t in saver.alist(_config(), limit=limit, filter=flt)])

    scenarios: list[tuple[str, Callable[[], Any], Callable[[], Any]]] = [
        (
            "get_tuple(latest)",
            lambda: saver.get_tuple(_config()),
            lambda: saver.aget_tuple(_config()),
        ),
        (
            "get_tuple(by id)",
            lambda: saver.get_tuple(_config(last)),
            lambda: saver.aget_tuple(_config(last)),
        ),
        (
            "list(limit=1)",
            lambda: list(saver.list(_config(), limit=1)),
            lambda: alist(1),
        ),
        (
            "list(filter, no match)",
            lambda: list(saver.list(_config(), filter={"source": "nope"})),
            lambda: alist(None, {"source": "nope"}),
        ),
        (
            "put(new checkpoint)",
            lambda: saver.put(
                _config(last),
                new_cp,
                {"source": "loop", "step": 99, "parents": {}},
                new_cp["channel_versions"],
            ),
            lambda: saver.aput(
                _config(last),
                new_cp,
                {"source": "loop", "step": 99, "parents": {}},
                new_cp["channel_versions"],
            ),
        ),
        (
            "put_writes(first write)",
            lambda: saver.put_writes(_config(last), batch, "task-new", "~__pregel_pull, n"),
            lambda: saver.aput_writes(_config(last), batch, "task-new", "~__pregel_pull, n"),
        ),
        (
            "put_writes(retry, already written)",
            lambda: saver.put_writes(_config(last), batch, "task-new", "~__pregel_pull, n"),
            lambda: saver.aput_writes(_config(last), batch, "task-new", "~__pregel_pull, n"),
        ),
    ]
    results: list[dict[str, Any]] = []
    for name, sync_call, async_call in scenarios:
        counters.reset()
        started = time.perf_counter()
        if async_api:
            asyncio.run(_await(async_call))
        else:
            sync_call()
        elapsed_ms = (time.perf_counter() - started) * 1000
        row: dict[str, Any] = {"scenario": name, "api": "async" if async_api else "sync"}
        row.update(counters.snapshot())
        row["total"] = counters.total()
        if latency_ms:
            row["simulated_ms"] = round(elapsed_ms)
        results.append(row)
    return results


async def _await(factory: Callable[[], Any]) -> Any:
    return await factory()


def render(rows: list[dict[str, Any]], channels: int, writes: int, history: int) -> str:
    cols = ["scenario", "api", *KINDS, "total"]
    if any("simulated_ms" in r for r in rows):
        cols.append("simulated_ms")
    lines = [
        f"# channels={channels} pending_writes={writes} history={history} "
        "(requests issued by the saver against a counting fake; not Azure)",
        " | ".join(cols),
    ]
    lines += [" | ".join(str(r.get(c, "")) for c in cols) for r in rows]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--channels", type=int, default=20)
    parser.add_argument("--writes", type=int, default=10)
    parser.add_argument("--history", type=int, default=5)
    parser.add_argument("--latency-ms", type=float, default=0.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = run(args.channels, args.writes, args.history, args.latency_ms, async_api=False)
    rows += run(args.channels, args.writes, args.history, args.latency_ms, async_api=True)
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(render(rows, args.channels, args.writes, args.history))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
