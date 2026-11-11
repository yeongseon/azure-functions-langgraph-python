"""Request-count budgets for the Blob saver via ``tools/benchmark_blob_checkpointer.py`` (#860).

The benchmark drives the real saver against a counting in-memory container. These tests
pin the *number of requests the saver issues* (not time, not Azure behavior) so a change
that adds a round trip fails here. The counting containers are built on the same
stand-ins the other Blob tests use, so no Azure SDK or network is needed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tools" / "benchmark_blob_checkpointer.py"


class _SyncBase:
    pass


class _AsyncBase:
    pass


class _NotFound(Exception):
    pass


class _Exists(Exception):
    pass


@pytest.fixture
def bench(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    blob_mod = types.ModuleType("azure.storage.blob")
    aio_mod = types.ModuleType("azure.storage.blob.aio")
    setattr(blob_mod, "ContainerClient", _SyncBase)
    setattr(aio_mod, "ContainerClient", _AsyncBase)
    setattr(blob_mod, "aio", aio_mod)
    exc_mod = types.ModuleType("azure.core.exceptions")
    setattr(exc_mod, "ResourceNotFoundError", _NotFound)
    setattr(exc_mod, "ResourceExistsError", _Exists)
    for name, mod in (
        ("azure", types.ModuleType("azure")),
        ("azure.storage", types.ModuleType("azure.storage")),
        ("azure.storage.blob", blob_mod),
        ("azure.storage.blob.aio", aio_mod),
        ("azure.core", types.ModuleType("azure.core")),
        ("azure.core.exceptions", exc_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("benchmark_blob_checkpointer_under_test", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _by_scenario(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["scenario"]: r for r in rows}


@pytest.mark.parametrize("async_api", [False, True])
def test_request_budgets(bench: types.ModuleType, async_api: bool) -> None:
    channels, writes, history = 6, 3, 2
    rows = _by_scenario(bench.run(channels, writes, history, 0.0, async_api))

    # Reading a checkpoint: hint + checkpoint + metadata + channels + pending writes, one
    # GET each; the blob metadata travels with the body so there is no HEAD at all.
    read = 1 + 1 + channels + writes
    latest = rows["get_tuple(latest)"]
    assert (latest["GET"], latest["HEAD"], latest["LIST"]) == (1 + read, 0, 1)
    by_id = rows["get_tuple(by id)"]
    assert (by_id["GET"], by_id["HEAD"]) == (read, 0)
    # Metadata-first filtering: a non-matching filter downloads only the metadata blobs.
    assert rows["list(filter, no match)"]["GET"] == history
    assert rows["list(filter, no match)"]["HEAD"] == 0

    # Writing: first writes and retries are both one create-only request per key.
    first = rows["put_writes(first write)"]
    retry = rows["put_writes(retry, already written)"]
    for row in (first, retry):
        assert (row["PUT*"], row["PUT"], row["HEAD"], row["GET"]) == (writes, 0, 0, 0)

    # put(): 1 hint read + 1 existence probe of the hinted checkpoint, then every blob once.
    put = rows["put(new checkpoint)"]
    assert (put["GET"], put["HEAD"]) == (1, 1)
    assert put["PUT"] == 1 + channels + 1 + 1 + 1  # dirty, values, metadata, marker, clean

    # No scenario uses an existence-check HEAD for writes or a properties HEAD for reads.
    assert all(r["total"] == sum(r[k] for k in bench.KINDS) for r in rows.values())


def test_counting_fake_mirrors_sdk_download_properties_and_conditional_create(
    bench: types.ModuleType,
) -> None:
    counters = bench.Counters()
    sync_cls, _ = bench.make_counting_containers(
        _SyncBase, _AsyncBase, _NotFound, _Exists, counters
    )
    blobs: dict[str, tuple[bytes, dict[str, str]]] = {}
    client = sync_cls(blobs).get_blob_client("a")
    client.upload_blob(b"x", {"k": "v"}, overwrite=False)
    with pytest.raises(_Exists):
        client.upload_blob(b"y", {"k": "w"}, overwrite=False)
    stream = client.download_blob()
    assert stream.readall() == b"x" and stream.properties.metadata == {"k": "v"}
    assert counters.snapshot() == {"GET": 1, "HEAD": 0, "PUT": 0, "PUT*": 2, "LIST": 0, "DEL": 0}
    with pytest.raises(_NotFound):
        sync_cls(blobs).get_blob_client("missing").download_blob()


def test_simulated_latency_is_reported_only_when_requested_and_labelled(
    bench: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bench.main(["--channels", "2", "--writes", "1", "--history", "1"]) == 0
    out = capsys.readouterr().out
    assert "not Azure" in out and "simulated_ms" not in out
    assert bench.main(["--channels", "2", "--writes", "1", "--history", "1", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["api"] for r in rows} == {"sync", "async"}
    assert all("simulated_ms" not in r for r in rows)
    rows = bench.run(2, 1, 1, 1.0, async_api=True)
    assert all("simulated_ms" in r for r in rows)
