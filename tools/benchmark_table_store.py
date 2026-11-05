"""Reproducible benchmark for ``AzureTableThreadStore.search()`` and ``count()``.

Every number printed by this script comes from one of two backends:

* ``fake``: a counting in-memory stand-in for ``TableClient``. It measures scan
  amplification (entities read, modeled request pages), peak Python memory and
  client-side CPU latency. It models NO network or service latency.
* ``azurite``: the Azurite emulator on loopback through the real
  ``azure-data-tables`` SDK. It measures SDK + emulator latency only.

A third backend, ``--connection-string-env VAR``, runs against a REAL Azure Table
endpoint (the connection string is read from the environment variable named VAR,
never from argv, and is never printed). It counts HTTP requests actually sent by
the SDK pipeline (retries included), so request counts are measured, and its
latency includes client-to-Azure round trips from wherever you run it.

Neither fake nor Azurite is Azure Storage. Do not quote these numbers as Azure
measurements, and do not derive a supported thread count from them.

Usage (from the repo root)::

    hatch run table:python tools/benchmark_table_store.py --sizes 1000,10000,100000
    hatch run table:python tools/benchmark_table_store.py --azurite --sizes 1000,10000
    hatch run table:python tools/benchmark_table_store.py --prototype-check

``--azurite`` connects to an Azurite table service YOU started, for example
``azurite-table --silent --location "$(mktemp -d)" --tablePort 10112``, and uses
``--azurite-port`` (default 10112). The script creates and deletes its own
tables named ``bench<size>r<8 hex>`` (unique per run, created never reused) and deletes
only the tables it created; it never starts or stops processes.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
import contextlib
from datetime import UTC, datetime, timedelta
import json
import os
import re
import statistics
import time
import tracemalloc
from typing import Any
import uuid

from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

PAGE_SIZE = 1000  # max entities per Table query response page (service limit)
STATUSES = ("idle", "busy", "interrupted", "error")
STATUS_WEIGHTS = (0.80, 0.10, 0.07, 0.03)
TENANTS = 50  # a tenant filter therefore matches about 2% of threads
KINDS = ("chat", "batch", "eval")
AZURITE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)
_STATUS_FILTER = re.compile(r"^PartitionKey eq '([^']*)'(?: and status eq '([^']*)')?$")


class CountingFakeTableClient:
    """In-memory ``TableClient`` that counts entities yielded by queries."""

    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        self.entities_read = 0
        self.queries = 0
        self._sorted_keys: list[tuple[str, str]] | None = None

    def reset_counters(self) -> None:
        self.entities_read = 0
        self.queries = 0

    @property
    def requests(self) -> int:
        """Modeled HTTP requests: one per started page of ``PAGE_SIZE`` entities."""
        return max(self.queries, -(-self.entities_read // PAGE_SIZE))

    def create_entity(self, entity: dict[str, Any]) -> None:
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = entity
        self._sorted_keys = None

    def query_entities(self, query_filter: str | None = None, **_: Any) -> Iterator[dict[str, Any]]:
        match = _STATUS_FILTER.match(query_filter or "")
        if match is None:
            raise ValueError(f"fake supports only partition/status filters, got {query_filter!r}")
        partition, status = match.group(1), match.group(2)
        self.queries += 1
        # Azure returns entities ordered by (PartitionKey, RowKey).
        if self._sorted_keys is None:  # sort once, not per query, to keep latency honest
            self._sorted_keys = sorted(self.entities)
        for key in (k for k in self._sorted_keys if k[0] == partition):
            entity = self.entities[key]
            if status is None or entity.get("status") == status:
                self.entities_read += 1
                yield dict(entity)


class _Rng:
    """Tiny deterministic xorshift64* generator (benchmark data, not security)."""

    def __init__(self, seed: int) -> None:
        self._state = (seed * 0x9E3779B97F4A7C15 + 1) & (2**64 - 1)

    def _next(self) -> int:
        x = self._state
        x ^= x >> 12
        x ^= (x << 25) & (2**64 - 1)
        x ^= x >> 27
        self._state = x
        return (x * 0x2545F4914F6CDD1D) & (2**64 - 1)

    def random(self) -> float:
        return self._next() / 2**64

    def randrange(self, stop: int) -> int:
        return self._next() % stop

    def getrandbits(self, bits: int) -> int:
        out = 0
        for _ in range(-(-bits // 64)):
            out = (out << 64) | self._next()
        return out & (2**bits - 1)

    def choice(self, items: tuple[str, ...]) -> str:
        return items[self.randrange(len(items))]

    def choices(self, items: tuple[str, ...], weights: tuple[float, ...]) -> list[str]:
        point, acc = self.random(), 0.0
        for item, weight in zip(items, weights, strict=True):
            acc += weight
            if point < acc:
                return [item]
        return [items[-1]]


def make_entity(rng: _Rng, base: datetime, index: int) -> dict[str, Any]:
    created = base + timedelta(seconds=(index // 2) * 5)  # pairs share created_at (tie-break)
    metadata = {
        "tenant": f"t{rng.randrange(TENANTS)}",
        "kind": rng.choice(KINDS),
        "user": f"user-{rng.randrange(5000)}",
    }
    return {
        "PartitionKey": "thread",
        "RowKey": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "created_at": created,
        "updated_at": created,
        "status": rng.choices(STATUSES, STATUS_WEIGHTS)[0],
        "interrupts_json": "{}",
        "metadata_json": json.dumps(metadata),
    }


def populate(client: Any, size: int, seed: int = 7) -> list[dict[str, Any]]:
    rng = _Rng(seed)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    entities = [make_entity(rng, base, i) for i in range(size)]
    if isinstance(client, CountingFakeTableClient):
        for entity in entities:
            client.create_entity(entity)
    else:
        for start in range(0, size, 100):  # one entity-group transaction per 100 rows
            batch = entities[start : start + 100]
            client.submit_transaction([("create", e) for e in batch])
    return entities


def workloads(
    size: int,
) -> list[tuple[str, str, dict[str, Any], Callable[[AzureTableThreadStore], Any]]]:
    deep = size  # status=idle holds ~80% of rows; offset 0.6N stays inside that set
    return [
        ("search limit=10 (no filter)", "search", {}, lambda s: s.search(limit=10)),
        (
            "search status=idle limit=10",
            "search",
            {},
            lambda s: s.search(status="idle", limit=10),
        ),
        (
            "search status=idle limit=10 offset=0.6N",
            "search",
            {},
            lambda s: s.search(status="idle", limit=10, offset=int(deep * 0.6)),
        ),
        (
            "search tenant=t7 limit=10",
            "search",
            {},
            lambda s: s.search(metadata={"tenant": "t7"}, limit=10),
        ),
        (
            "search tenant+kind+status=idle limit=10",
            "search",
            {},
            lambda s: s.search(metadata={"tenant": "t7", "kind": "chat"}, status="idle", limit=10),
        ),
        ("count status=busy", "count", {}, lambda s: s.count(status="busy")),
        ("count tenant=t7", "count", {}, lambda s: s.count(metadata={"tenant": "t7"})),
    ]


def measure(
    store: AzureTableThreadStore,
    counter: Any,
    call: Callable[[AzureTableThreadStore], Any],
    reps: int,
) -> dict[str, Any]:
    times: list[float] = []
    for _ in range(reps):
        counter.reset_counters()
        start = time.perf_counter()
        call(store)
        times.append((time.perf_counter() - start) * 1000)
    counter.reset_counters()
    tracemalloc.start()
    result = call(store)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    returned = len(result) if isinstance(result, list) else result
    return {
        "latency_ms_median": round(statistics.median(times), 1),
        "latency_ms_max": round(max(times), 1),
        "entities_read": counter.entities_read,
        "requests_modeled": counter.requests,
        "requests_measured": getattr(counter, "requests_measured", None),
        "peak_mem_mib": round(peak / 1024 / 1024, 1),
        "result": returned,
    }


class RequestCounter:
    """Counts every HTTP request the SDK actually sends (retries included).

    azure-data-tables ignores user pipeline policies, so the count is taken at the
    transport: pass ``counting_transport(counter)`` as ``transport=``. Each attempt
    is a billable transaction.
    """

    def __init__(self) -> None:
        self.sent = 0


def counting_transport(counter: RequestCounter) -> Any:
    """Return a ``requests`` based SDK transport that increments ``counter.sent`` per send."""
    from azure.core.pipeline.transport import RequestsTransport  # type: ignore[import-not-found]

    class _CountingTransport(RequestsTransport):  # type: ignore[misc]
        def send(self, request: Any, **kwargs: Any) -> Any:
            counter.sent += 1
            return super().send(request, **kwargs)

    return _CountingTransport()


class CountingProxy:
    """Counts entities/queries for a real SDK ``TableClient`` (Azurite or Azure)."""

    def __init__(self, inner: Any, request_counter: RequestCounter | None = None) -> None:
        self._inner = inner
        self._request_counter = request_counter
        self.entities_read = 0
        self.queries = 0

    def reset_counters(self) -> None:
        self.entities_read = 0
        self.queries = 0
        if self._request_counter is not None:
            self._request_counter.sent = 0

    @property
    def requests_measured(self) -> int | None:
        return None if self._request_counter is None else self._request_counter.sent

    @property
    def requests(self) -> int:
        return max(self.queries, -(-self.entities_read // PAGE_SIZE))

    def query_entities(self, query_filter: str | None = None, **kwargs: Any) -> Iterator[Any]:
        self.queries += 1
        for entity in self._inner.query_entities(query_filter=query_filter, **kwargs):
            self.entities_read += 1
            yield entity

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def run_fake(sizes: list[int], reps: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for size in sizes:
        client = CountingFakeTableClient()
        populate(client, size)
        store = AzureTableThreadStore(
            table_client=client,  # type: ignore[arg-type]
            not_found_error=KeyError,
            modified_error=RuntimeError,
            match_conditions=object(),
        )
        for name, _, _, call in workloads(size):
            rows.append(
                {
                    "backend": "fake",
                    "size": size,
                    "workload": name,
                    **measure(store, client, call, reps),
                }
            )
    return rows


def run_tables(
    service: Any,
    backend: str,
    sizes: list[int],
    reps: int,
    counter: RequestCounter | None = None,
) -> list[dict[str, Any]]:
    """Seed and measure one fresh table per size; delete only the tables created here."""
    run_id = uuid.uuid4().hex[:8]
    created: list[str] = []
    rows: list[dict[str, Any]] = []
    try:
        for size in sizes:
            name = f"bench{size}r{run_id}"
            raw = service.create_table(name)  # fails loudly if it already exists
            created.append(name)
            populate(raw, size)
            proxy = CountingProxy(raw, counter)
            store = AzureTableThreadStore.from_table_client(proxy)  # type: ignore[arg-type]
            for label, _, _, call in workloads(size):
                rows.append(
                    {
                        "backend": backend,
                        "size": size,
                        "workload": label,
                        **measure(store, proxy, call, reps),
                    }
                )
    finally:
        for name in created:
            with contextlib.suppress(Exception):
                service.delete_table(name)
    return rows


def run_azurite(sizes: list[int], reps: int, port: int) -> list[dict[str, Any]]:
    from azure.data.tables import TableServiceClient  # type: ignore[import-not-found]

    conn = (
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        f"AccountKey={AZURITE_KEY};TableEndpoint=http://127.0.0.1:{port}/devstoreaccount1"
    )
    return run_tables(TableServiceClient.from_connection_string(conn), "azurite", sizes, reps)


def run_azure(sizes: list[int], reps: int, env_var: str) -> list[dict[str, Any]]:
    """Run against a real Table endpoint; the connection string comes from ``env_var``."""
    conn = os.environ.get(env_var)
    if not conn:
        raise SystemExit(f"environment variable {env_var} is not set or empty")
    from azure.data.tables import TableServiceClient  # type: ignore[import-not-found]

    counter = RequestCounter()
    service = TableServiceClient.from_connection_string(conn, transport=counting_transport(counter))
    return run_tables(service, "azure", sizes, reps, counter)


# --- Prototype of a bounded alternative (benchmark-only; NOT production code) ---
# A time-ordered index partition per status: RowKey = inverted created_at + thread id.
# Table returns rows sorted by RowKey, so newest-first search without metadata filters
# can stop after offset+limit rows instead of scanning the partition.


def index_row_key(created_at: datetime, thread_id: str) -> str:
    inverted = 10**16 - int(created_at.timestamp() * 1_000_000)
    return f"{inverted:020d}_{thread_id}"


def build_index(entities: list[dict[str, Any]]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    index: dict[str, list[tuple[str, dict[str, Any]]]] = {s: [] for s in STATUSES}
    for entity in entities:
        index[entity["status"]].append(
            (index_row_key(entity["created_at"], entity["RowKey"]), entity)
        )
    for rows in index.values():
        rows.sort(key=lambda pair: pair[0])
    return index


def indexed_search(
    index: dict[str, list[tuple[str, dict[str, Any]]]], status: str, limit: int, offset: int
) -> tuple[list[str], int]:
    """Return (thread ids, entities read) with early termination."""
    if limit <= 0:
        return [], 0
    out: list[str] = []
    read = 0
    for read, (_, entity) in enumerate(index[status], start=1):
        if read > offset:
            out.append(entity["RowKey"])
        if len(out) >= limit:
            break
    return out, read


def prototype_check(size: int = 5000) -> dict[str, Any]:
    client = CountingFakeTableClient()
    entities = populate(client, size)
    store = AzureTableThreadStore(
        table_client=client,  # type: ignore[arg-type]
        not_found_error=KeyError,
        modified_error=RuntimeError,
        match_conditions=object(),
    )
    index = build_index(entities)
    checked = 0
    for status in STATUSES:
        total = sum(1 for e in entities if e["status"] == status)
        # count correctness: baseline count() vs index cardinality
        if not store.count(status=status) == total == len(index[status]):  # type: ignore[arg-type]
            raise SystemExit(f"count mismatch for status={status}")
        for limit, offset in (
            (10, 0),
            (0, 0),
            (1, 0),
            (25, 3),
            (10, max(total - 5, 0)),
            (10, total + 50),
        ):
            baseline = [
                t.thread_id for t in store.search(status=status, limit=limit, offset=offset)
            ]  # type: ignore[arg-type]
            proto, _ = indexed_search(index, status, limit, offset)
            if baseline != proto:
                raise SystemExit(f"search mismatch: {status=} {limit=} {offset=}")
            checked += 1
    return {
        "entities": size,
        "equivalent_cases": checked,
        "tie_break": "RowKey asc on equal created_at",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sizes", default="1000,10000,100000")
    parser.add_argument("--azurite", action="store_true", help="use Azurite instead of the fake")
    parser.add_argument("--azurite-port", type=int, default=10112)
    parser.add_argument(
        "--connection-string-env",
        metavar="VAR",
        help="run against a real Table endpoint; connection string read from env var VAR",
    )
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--prototype-check", action="store_true")
    args = parser.parse_args()
    if args.prototype_check:
        print(json.dumps(prototype_check(), indent=2))
        return
    sizes = [int(s) for s in args.sizes.split(",")]
    if args.connection_string_env:
        backend = "azure"
        rows = run_azure(sizes, args.reps, args.connection_string_env)
    elif args.azurite:
        backend = "azurite"
        rows = run_azurite(sizes, args.reps, args.azurite_port)
    else:
        backend = "fake"
        rows = run_fake(sizes, args.reps)
    print(format_report(backend, rows))


def format_report(backend: str, rows: list[dict[str, Any]]) -> str:
    note = (
        "REAL Azure Storage measurements (latency includes client-to-Azure round trips)"
        if backend == "azure"
        else "NOT Azure Storage measurements"
    )
    lines = [
        f"# backend={backend}: {note}",
        "| size | workload | entities read | requests (modeled) | requests (measured) | "
        "peak MiB | median ms | max ms | result |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        measured = "n/a" if r["requests_measured"] is None else r["requests_measured"]
        lines.append(
            f"| {r['size']} | {r['workload']} | {r['entities_read']} | {r['requests_modeled']} | "
            f"{measured} | {r['peak_mem_mib']} | {r['latency_ms_median']} | "
            f"{r['latency_ms_max']} | {r['result']} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
