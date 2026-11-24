"""Real SDK status-index evidence using the existing Table benchmark's env-var transport.

Run: hatch run table:python -m tools.benchmark_table_status_index
     --connection-string-env BENCH_TABLE_CS --sizes 1000,10000,100000 --reps 3

No fake/prototype claims: this measures the shipped store, seeded by the original
benchmark generator. JSON contains no endpoint, credential or entity payload.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict
import importlib.metadata
import json
import os
import sys
import time
from typing import Any
import uuid

from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore
from tools.benchmark_table_store import (
    CountingProxy,
    RequestCounter,
    counting_transport,
    measure,
    populate,
)


def snapshot(proxy: CountingProxy) -> dict[str, Any]:
    """Include HTTP rows fetched, not only iterator rows consumed (page overfetch)."""
    counter = proxy._request_counter
    if counter is None:
        raise RuntimeError("benchmark requires the HTTP transport counter")
    return {
        "requests": counter.sent,
        "methods": dict(counter.methods),
        "rows_received": counter.rows_received,
        "bytes_received": counter.bytes_received,
        "entities_consumed": proxy.entities_read,
    }


def sample(proxy: CountingProxy, action: Callable[[], Any]) -> dict[str, Any]:
    proxy.reset_counters()
    start = time.perf_counter()
    result = action()
    return {
        "ms": round((time.perf_counter() - start) * 1000, 1),
        **snapshot(proxy),
        "result": result,
    }


def read_cases(size: int) -> list[dict[str, Any]]:
    return (
        [
            {"status": status, "limit": 10, "offset": 0}
            for status in ("idle", "interrupted", "error", "busy")
        ]
        + [
            {"status": "idle", "limit": limit, "offset": offset}
            for limit, offset in ((0, 0), (10, 100), (10, 200), (10, 201), (1000, 0))
        ]
        + [{"limit": 10}, {"metadata": {"tenant": "t7"}, "limit": 10}]
    )


def run_size(raw: Any, counter: RequestCounter, size: int, reps: int) -> dict[str, Any]:
    proxy = CountingProxy(raw, counter)
    seeded = populate(raw, size)
    ordered = sorted(seeded, key=lambda e: (-e["created_at"].timestamp(), e["RowKey"]))
    off = AzureTableThreadStore.from_table_client(proxy)
    on = AzureTableThreadStore.from_table_client(proxy, status_index=True)
    result: dict[str, Any] = {
        "size": size,
        "status_counts": {
            s: sum(e["status"] == s for e in seeded)
            for s in ("idle", "busy", "interrupted", "error")
        },
    }
    result["absent"] = sample(proxy, lambda: len(on.search(status="idle")))
    result["rebuild"] = sample(proxy, lambda: asdict(on.rebuild_status_index()))
    result["building"] = sample(proxy, lambda: len(on.search(status="idle")))
    result["audit"] = sample(proxy, lambda: asdict(on.audit_status_index(mark_ready=True)))
    reads = []
    for options in read_cases(size):
        want = [
            e["RowKey"]
            for e in ordered
            if (options.get("status") is None or e["status"] == options["status"])
            and all(
                json.loads(e["metadata_json"]).get(k) == v
                for k, v in options.get("metadata", {}).items()
            )
        ]
        offset, limit = options.get("offset", 0), options["limit"]
        for enabled, store in ((False, off), (True, on)):
            runs = []

            def search(_: AzureTableThreadStore) -> list[Any]:
                found = store.search(**options)
                if [t.thread_id for t in found] != want[offset : offset + limit]:
                    raise RuntimeError(f"seeded search oracle mismatch: {options}")
                runs.append(snapshot(proxy))
                return found

            measured = measure(store, proxy, search, reps)
            reads.append({"index": enabled, "options": options, **measured, "wire": runs})
    for enabled, store in ((False, off), (True, on)):
        runs = []

        def count(_: AzureTableThreadStore) -> int:
            found = store.count(status="idle")
            if found != result["status_counts"]["idle"]:
                raise RuntimeError("seeded count oracle mismatch")
            runs.append(snapshot(proxy))
            return found

        reads.append(
            {
                "index": enabled,
                "options": {"count_status": "idle"},
                **measure(store, proxy, count, reps),
                "wire": runs,
            }
        )
    result["reads"] = reads
    writes = []
    for enabled, store in ((False, off), (True, on)):
        for rep in range(reps):
            thread = None
            lock = None
            for operation in (
                "create",
                "acquire",
                "release_idle",
                "update_error",
                "metadata",
                "delete",
            ):

                def write() -> None:
                    nonlocal thread, lock
                    match operation:
                        case "create":
                            thread = store.create(metadata={"bench": True})
                        case "acquire":
                            if thread is None:
                                raise RuntimeError("create must precede acquire")
                            lock = store.try_acquire_run_lock(thread.thread_id)
                            if lock is None:
                                raise RuntimeError("benchmark lock acquisition failed")
                        case "release_idle":
                            if thread is None or lock is None:
                                raise RuntimeError("acquire must precede release")
                            store.release_run_lock(
                                thread.thread_id, status="idle", owner_token=lock.lock_token
                            )
                        case "update_error":
                            if thread is None:
                                raise RuntimeError("create must precede status update")
                            store.update(thread.thread_id, status="error")
                        case "metadata":
                            if thread is None:
                                raise RuntimeError("create must precede metadata update")
                            store.merge_metadata(thread.thread_id, {"m": rep})
                        case "delete":
                            if thread is None:
                                raise RuntimeError("create must precede delete")
                            store.delete(thread.thread_id)
                        case _:
                            raise AssertionError(operation)

                writes.append(
                    {
                        "index": enabled,
                        "rep": rep + 1,
                        "operation": operation,
                        **sample(proxy, write),
                    }
                )
    result["writes"] = writes
    result["stale_audit"] = sample(proxy, lambda: asdict(on.audit_status_index()))
    # The existing rehearsal's O(hints/14) repair is deliberately bounded to 1k here.
    result["repair"] = (
        sample(proxy, lambda: on.repair_status_index(0))
        if size == 1000
        else {"not_proven": "scale repair not run; O(hints/14) verification requests"}
    )
    result["disable"] = sample(proxy, lambda: on.disable_status_index())
    result["disabled_search"] = sample(proxy, lambda: len(on.search(status="idle")))
    return result


def main() -> None:
    from azure.data.tables import TableServiceClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-string-env", required=True)
    parser.add_argument("--sizes", default="1000,10000,100000")
    parser.add_argument("--reps", type=int, default=3)
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]
    if not sizes or any(s < 1 or s > 100000 for s in sizes) or args.reps < 1:
        parser.error("sizes must be 1..100000 and reps positive")
    conn = os.environ.get(args.connection_string_env)
    if not conn:
        parser.error("named connection-string environment variable is empty")
    counter = RequestCounter()
    reports = []
    with TableServiceClient.from_connection_string(
        conn, transport=counting_transport(counter)
    ) as service:
        for size in sizes:
            name = f"bench{size}r{uuid.uuid4().hex[:8]}"
            raw = service.create_table(name)
            try:
                reports.append(run_size(raw, counter, size, args.reps))
                print(json.dumps({"completed_size": size}), file=sys.stderr, flush=True)
            finally:
                service.delete_table(name)
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "sdk": {
                    p: importlib.metadata.version(p) for p in ("azure-data-tables", "azure-core")
                },
                "sizes": reports,
                "whole_run_requests": counter.total,
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
