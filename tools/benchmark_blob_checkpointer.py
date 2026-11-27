"""Request-count (and optional simulated-latency) benchmark for ``AzureBlobCheckpointSaver``.

What this measures
------------------
By default every number comes from a counting in-memory stand-in for the Azure Blob container
clients (``--backend fake``). It counts **requests the saver issues**, by kind:

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

``--backend azurite`` runs the same workload through the real ``azure-storage-blob`` sync and
async clients against Azurite and counts the HTTP requests at the transport (``requests`` and
``aiohttp``), so requests the SDK makes itself, pagination and retries are included and nothing
is modeled. Each request is classified from its method, query and headers: a GET with
``comp=list`` is LIST, a PUT with ``If-None-Match: *`` is PUT*, container-level calls and
anything unexpected are OTHER (shown as its own column only when it is not zero).
``--latency-ms`` sleeps per request at the same point. Azurite is not Azure Storage: the counts
are exact for what the SDK sends, the latency is injected and never measured, and neither
backend says anything about throttling, retries by the service or real network behavior.
``tests/integration/test_checkpoint_conformance.py`` asserts that both backends count the same
requests for every scenario, so the fake cannot drift from the SDK unnoticed.

Usage (repo root, with the ``azure-blob`` extra installed)::

    PYTHONPATH=src python tools/benchmark_blob_checkpointer.py
    PYTHONPATH=src python tools/benchmark_blob_checkpointer.py --channels 40 --writes 20 --json
    PYTHONPATH=src python tools/benchmark_blob_checkpointer.py --latency-ms 20
    PYTHONPATH=src python tools/benchmark_blob_checkpointer.py --backend azurite --latency-ms 20

To compare two revisions, run the same script with ``PYTHONPATH`` pointing at each
revision's ``src`` directory and compare the printed counts.

``--backend azure --connection-string-env VAR`` uses a real Azure Blob endpoint,
reusing the Azurite HTTP-counting transports (including retries and pagination).
Only the environment variable NAME goes in argv; never supply a real credential
with ``--connection-string``. Each API creates and deletes its own private container.
Azure rows include ``wall_ms`` (client-to-Azure, not in-region unless run there).
Injected latency is forbidden in Azure mode. Memory and connection pressure are
not measured by this script. Repeat whole invocations for independent samples.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Callable, Mapping
from itertools import count
import json
import os
import re
import sys
import time
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit
import uuid

PAGE_SIZE = 5000  # List Blobs maximum results per response page
KINDS = ("GET", "HEAD", "PUT", "PUT*", "LIST", "DEL")
# Azurite's default Blob endpoint and its well-known development account key.
AZURITE_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1"
)

Blobs = dict[str, tuple[bytes, dict[str, str]]]


class Counters:
    """Per-kind request counters shared by one sync and one async container."""

    def __init__(self, latency_ms: float = 0.0) -> None:
        self.counts: Counter[str] = Counter()
        self.latency_ms = latency_ms

    def reset(self) -> None:
        self.counts.clear()

    def record(self, kind: str) -> None:
        self.counts[kind] += 1

    def total(self) -> int:
        return sum(self.counts.values())

    def snapshot(self) -> dict[str, int]:
        return {kind: self.counts.get(kind, 0) for kind in KINDS}


def classify(method: str, url: str, headers: Mapping[str, str]) -> str:
    """Counter kind of one HTTP request: a ``KINDS`` entry, or ``OTHER`` for anything else.

    ``OTHER`` covers container-level calls (create, delete, properties) and any request the
    model has no kind for, so unexpected traffic is visible instead of silently dropped.
    """
    query = parse_qs(urlsplit(url).query)
    method = method.upper()
    if method == "GET" and query.get("comp") == ["list"]:
        return "LIST"
    if "restype" in query:
        return "OTHER"
    if method == "GET":
        return "GET"
    if method == "HEAD":
        return "HEAD"
    if method == "DELETE":
        return "DEL"
    if method == "PUT":
        return "PUT*" if headers.get("If-None-Match") == "*" else "PUT"
    return "OTHER"


class _Props:
    def __init__(self, metadata: dict[str, str], etag: str = "fixture") -> None:
        self.metadata = metadata
        self.etag = etag


class _Stream:
    """Mirrors ``StorageStreamDownloader``: body via ``readall`` and ``.properties``.

    In ``azure-storage-blob`` the downloader's ``properties`` (a ``BlobProperties``
    including ``metadata``) is built from the headers of the *same* GET that carries the
    body, so reading it costs no extra request.
    """

    def __init__(self, data: bytes, metadata: dict[str, str], etag: str = "fixture") -> None:
        self._data = data
        self.properties = _Props(dict(metadata), etag)

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

    etags: dict[tuple[int, str], str] = {}
    versions = count()

    def _put(
        blobs: Blobs,
        name: str,
        data: bytes,
        metadata: dict[str, str],
        overwrite: bool,
        **kwargs: Any,
    ) -> dict[str, str]:
        if not overwrite and name in blobs:
            tick("PUT*")
            raise exists(name)
        tick("PUT" if overwrite else "PUT*")
        if "etag" in kwargs and kwargs["etag"] != etags.get((id(blobs), name), "fixture"):
            from azure.core.exceptions import ResourceModifiedError

            raise ResourceModifiedError("hint changed", status_code=412)
        blobs[name] = (bytes(data), dict(metadata))
        etag = etags[(id(blobs), name)] = str(next(versions))
        return {"etag": etag}

    class _Client:
        def __init__(self, blobs: Blobs, name: str) -> None:
            self._b, self._n = blobs, name

        def _sleep(self) -> None:
            if counters.latency_ms:
                time.sleep(counters.latency_ms / 1000)

        def upload_blob(
            self, data: bytes, metadata: dict[str, str], overwrite: bool, **kwargs: Any
        ) -> dict[str, str]:
            self._sleep()
            return _put(self._b, self._n, data, metadata, overwrite, **kwargs)

        def download_blob(self) -> _Stream:
            self._sleep()
            tick("GET")
            if self._n not in self._b:
                raise not_found(self._n)
            data, metadata = self._b[self._n]
            return _Stream(data, metadata, etags.get((id(self._b), self._n), "fixture"))

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

        async def upload_blob(
            self, data: bytes, metadata: dict[str, str], overwrite: bool, **kwargs: Any
        ) -> dict[str, str]:
            await self._sleep()
            return _put(self._b, self._n, data, metadata, overwrite, **kwargs)

        async def download_blob(self) -> _AStream:
            await self._sleep()
            tick("GET")
            if self._n not in self._b:
                raise not_found(self._n)
            data, metadata = self._b[self._n]
            return _AStream(data, metadata, etags.get((id(self._b), self._n), "fixture"))

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


Scenario = tuple[str, Callable[[], Any], Callable[[], Any]]


def _scenarios(saver: Any, last: str, channels: int, history: int, writes: int) -> list[Scenario]:
    """The measured operations as (name, sync call, async call), bound to ``saver``."""
    new_cp = _checkpoint(history + 1, channels)
    batch = [(f"n{i}", {"n": i}) for i in range(writes)]

    async def alist(limit: int | None, flt: dict[str, Any] | None = None) -> int:
        return len([t async for t in saver.alist(_config(), limit=limit, filter=flt)])

    return [
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


def _row(
    name: str,
    async_api: bool,
    counters: Counters,
    elapsed_ms: float,
    latency_ms: float,
    *,
    measured: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {"scenario": name, "api": "async" if async_api else "sync"}
    row.update(counters.snapshot())
    if counters.counts.get("OTHER"):
        row["OTHER"] = counters.counts["OTHER"]
    row["total"] = counters.total()
    if latency_ms:
        row["simulated_ms"] = round(elapsed_ms)
    if measured:
        row["wall_ms"] = round(elapsed_ms, 3)
    return row


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
    results: list[dict[str, Any]] = []
    for name, sync_call, async_call in _scenarios(saver, last, channels, history, writes):
        counters.reset()
        started = time.perf_counter()
        if async_api:
            asyncio.run(_await(async_call))
        else:
            sync_call()
        elapsed_ms = (time.perf_counter() - started) * 1000
        results.append(_row(name, async_api, counters, elapsed_ms, latency_ms))
    return results


class _AzuriteEnvironment(NamedTuple):
    sync: Any
    make_async: Callable[[], Any]
    cleanup: Callable[[], None]


def _azurite_environment(connection_string: str, counters: Counters) -> _AzuriteEnvironment:
    """A private container on Azurite whose clients count every HTTP request they send."""
    import importlib

    blob_mod = importlib.import_module("azure.storage.blob")
    aio_mod = importlib.import_module("azure.storage.blob.aio")
    transports = importlib.import_module("azure.core.pipeline.transport")

    class CountingTransport(transports.RequestsTransport):  # type: ignore[misc]
        def send(self, request: Any, **kwargs: Any) -> Any:
            counters.record(classify(request.method, request.url, request.headers))
            if counters.latency_ms:
                time.sleep(counters.latency_ms / 1000)
            return super().send(request, **kwargs)

    class CountingAioTransport(transports.AioHttpTransport):  # type: ignore[misc]
        async def send(self, request: Any, **kwargs: Any) -> Any:
            counters.record(classify(request.method, request.url, request.headers))
            if counters.latency_ms:
                await asyncio.sleep(counters.latency_ms / 1000)
            return await super().send(request, **kwargs)

    name = f"bench-{uuid.uuid4().hex[:12]}"
    sync = blob_mod.ContainerClient.from_connection_string(
        connection_string, name, transport=CountingTransport()
    )
    sync.create_container()

    def make_async() -> Any:
        return aio_mod.ContainerClient.from_connection_string(
            connection_string, name, transport=CountingAioTransport()
        )

    def cleanup() -> None:
        try:
            sync.delete_container()
        finally:
            sync.close()

    return _AzuriteEnvironment(sync, make_async, cleanup)


def run_azurite(
    channels: int,
    writes: int,
    history: int,
    latency_ms: float,
    async_api: bool,
    connection_string: str = AZURITE_CONNECTION_STRING,
    *,
    measured: bool = False,
) -> list[dict[str, Any]]:
    """Run the workload through the real SDK clients on Azurite, counting at the transport.

    Seeding uses the sync client with the counter idle. The async scenarios run in one event
    loop with an async client created inside it, because an ``aiohttp`` session cannot be
    reused across the separate loops ``asyncio.run`` makes.
    """
    import importlib

    saver_mod = importlib.import_module("azure_functions_langgraph.checkpointers.azure_blob")
    counters = Counters()
    env = _azurite_environment(connection_string, counters)
    try:
        saver = saver_mod.AzureBlobCheckpointSaver(container_client=env.sync)
        last = seed(saver, history, channels, writes)
        counters.latency_ms = latency_ms
        if not async_api:
            rows: list[dict[str, Any]] = []
            for name, sync_call, _ in _scenarios(saver, last, channels, history, writes):
                counters.reset()
                started = time.perf_counter()
                sync_call()
                elapsed_ms = (time.perf_counter() - started) * 1000
                rows.append(_row(name, False, counters, elapsed_ms, latency_ms, measured=measured))
            return rows

        async def measure() -> list[dict[str, Any]]:
            aio = env.make_async()
            try:
                async_saver = saver_mod.AzureBlobCheckpointSaver(
                    container_client=env.sync, aio_container_client=aio
                )
                measured: list[dict[str, Any]] = []
                for name, _, async_call in _scenarios(async_saver, last, channels, history, writes):
                    counters.reset()
                    started = time.perf_counter()
                    await async_call()
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    measured.append(
                        _row(name, True, counters, elapsed_ms, latency_ms, measured=measure_wall)
                    )
                return measured
            finally:
                await aio.close()

        measure_wall = measured
        return asyncio.run(measure())
    finally:
        env.cleanup()


async def _await(factory: Callable[[], Any]) -> Any:
    return await factory()


def render(
    rows: list[dict[str, Any]], channels: int, writes: int, history: int, backend: str = "fake"
) -> str:
    cols = ["scenario", "api", *KINDS]
    if any("OTHER" in r for r in rows):
        cols.append("OTHER")
    cols.append("total")
    if any("simulated_ms" in r for r in rows):
        cols.append("simulated_ms")
    if any("wall_ms" in r for r in rows):
        cols.append("wall_ms")
    origin = (
        "requests issued by the saver against a counting fake; not Azure"
        if backend == "fake"
        else "HTTP requests the Azure SDK sent to Azurite, counted at the transport; "
        "Azurite is not Azure Storage"
    )
    if backend == "azure":
        origin = "real Azure HTTP requests counted at the transport; client-to-Azure wall time"
    lines = [
        f"# channels={channels} pending_writes={writes} history={history} ({origin})",
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
    parser.add_argument("--backend", choices=("fake", "azurite", "azure"), default="fake")
    parser.add_argument(
        "--connection-string",
        default=None,
        help="storage connection string for --backend azurite (default: Azurite's Blob port)",
    )
    parser.add_argument(
        "--connection-string-env",
        metavar="VAR",
        help="Azure credential environment variable NAME, never its value",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    connection = args.connection_string or AZURITE_CONNECTION_STRING
    if args.backend == "azure":
        if args.connection_string is not None or args.latency_ms != 0:
            parser.error("Azure requires environment-only credentials and zero injected latency")
        if not args.connection_string_env or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", args.connection_string_env
        ):
            parser.error("Azure requires --connection-string-env with an environment variable name")
        connection = os.environ.get(args.connection_string_env)
        if not connection:
            parser.error("credential environment variable is not set or empty")
    elif args.connection_string_env:
        parser.error("--connection-string-env requires --backend azure")
    if args.backend in ("azurite", "azure"):
        options = {"measured": True} if args.backend == "azure" else {}
        try:
            rows = [
                row
                for async_api in (False, True)
                for row in run_azurite(
                    args.channels,
                    args.writes,
                    args.history,
                    args.latency_ms,
                    async_api,
                    connection,
                    **options,
                )
            ]
        except Exception:
            # CLI credential boundary: SDK/parser exceptions can contain credentials.
            # Do not print the exception, its traceback, or client configuration.
            if args.backend == "azure":
                print(
                    "Azure benchmark failed; exception details suppressed to protect credentials",
                    file=sys.stderr,
                )
                return 1
            raise
    else:
        rows = run(args.channels, args.writes, args.history, args.latency_ms, async_api=False)
        rows += run(args.channels, args.writes, args.history, args.latency_ms, async_api=True)
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(render(rows, args.channels, args.writes, args.history, args.backend))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
