"""Request-count budgets for the Blob saver via ``tools/benchmark_blob_checkpointer.py`` (#860).

The benchmark drives the real saver against a counting in-memory container. These tests
pin the *number of requests the saver issues* (not time, not Azure behavior) so a change
that adds a round trip fails here. The counting containers are built on the same
stand-ins the other Blob tests use, so no Azure SDK or network is needed.

The ``azurite`` backend counts the same requests at the HTTP transport of the real SDK clients.
Its classification, its counting transports (against stand-in transports) and its command line
are tested here; that it agrees with the fake on a real SDK is asserted against Azurite in
``tests/integration/test_checkpoint_conformance.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tools" / "benchmark_blob_checkpointer.py"


class _SyncBase:
    """Stand-in ``ContainerClient`` that records how it was built."""

    @classmethod
    def from_connection_string(cls, conn_str: str, container_name: str, **kwargs: Any) -> Any:
        client = cls()
        client.built_with = (conn_str, container_name, kwargs)  # type: ignore[attr-defined]
        client.calls = []  # type: ignore[attr-defined]
        return client

    def create_container(self) -> None:
        self.calls.append("create")  # type: ignore[attr-defined]

    def delete_container(self) -> None:
        self.calls.append("delete")  # type: ignore[attr-defined]

    def close(self) -> None:
        self.calls.append("close")  # type: ignore[attr-defined]


class _AsyncBase:
    @classmethod
    def from_connection_string(cls, conn_str: str, container_name: str, **kwargs: Any) -> Any:
        client = cls()
        client.built_with = (conn_str, container_name, kwargs)  # type: ignore[attr-defined]
        return client


class _RequestsTransport:
    def send(self, request: Any, **kwargs: Any) -> Any:
        return ("sent", request)


class _AioHttpTransport:
    async def send(self, request: Any, **kwargs: Any) -> Any:
        return ("sent", request)


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
    transport_mod = types.ModuleType("azure.core.pipeline.transport")
    setattr(transport_mod, "RequestsTransport", _RequestsTransport)
    setattr(transport_mod, "AioHttpTransport", _AioHttpTransport)
    for name, mod in (
        ("azure", types.ModuleType("azure")),
        ("azure.storage", types.ModuleType("azure.storage")),
        ("azure.storage.blob", blob_mod),
        ("azure.storage.blob.aio", aio_mod),
        ("azure.core", types.ModuleType("azure.core")),
        ("azure.core.exceptions", exc_mod),
        ("azure.core.pipeline", types.ModuleType("azure.core.pipeline")),
        ("azure.core.pipeline.transport", transport_mod),
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


CONTAINER_URL = "http://127.0.0.1:10000/devstoreaccount1/bench-1"


@pytest.mark.parametrize(
    ("method", "url", "headers", "kind"),
    [
        pytest.param("GET", f"{CONTAINER_URL}/blob-a", {}, "GET", id="download"),
        pytest.param(
            "GET",
            f"{CONTAINER_URL}?restype=container&comp=list&prefix=t/",
            {},
            "LIST",
            id="list-page",
        ),
        pytest.param("HEAD", f"{CONTAINER_URL}/blob-a", {}, "HEAD", id="properties"),
        pytest.param("PUT", f"{CONTAINER_URL}/blob-a", {}, "PUT", id="overwrite-upload"),
        pytest.param(
            "PUT",
            f"{CONTAINER_URL}/blob-a",
            {"If-None-Match": "*"},
            "PUT*",
            id="create-only-upload",
        ),
        pytest.param(
            "PUT", f"{CONTAINER_URL}/blob-a", {"If-None-Match": "etag"}, "PUT", id="other-condition"
        ),
        pytest.param("DELETE", f"{CONTAINER_URL}/blob-a", {}, "DEL", id="delete-blob"),
        pytest.param("put", f"{CONTAINER_URL}/blob-a", {}, "PUT", id="method-case"),
        pytest.param(
            "PUT", f"{CONTAINER_URL}?restype=container", {}, "OTHER", id="create-container"
        ),
        pytest.param(
            "DELETE", f"{CONTAINER_URL}?restype=container", {}, "OTHER", id="delete-container"
        ),
        pytest.param(
            "GET", f"{CONTAINER_URL}?restype=container", {}, "OTHER", id="container-properties"
        ),
        pytest.param("POST", f"{CONTAINER_URL}?comp=batch", {}, "OTHER", id="unmodeled"),
    ],
)
def test_classify_reads_the_counter_kind_off_the_request(
    bench: types.ModuleType, method: str, url: str, headers: dict[str, str], kind: str
) -> None:
    assert bench.classify(method, url, headers) == kind


def test_unmodeled_traffic_gets_its_own_column_only_when_it_exists(
    bench: types.ModuleType,
) -> None:
    counters = bench.Counters()
    counters.record("GET")
    clean = bench._row("get", False, counters, 1.0, 0.0)
    assert "OTHER" not in clean and clean["total"] == 1
    counters.record("OTHER")
    noisy = bench._row("get", False, counters, 1.0, 0.0)
    assert noisy["OTHER"] == 1 and noisy["total"] == 2
    assert "OTHER" not in bench.render([clean], 1, 1, 1)
    assert "OTHER" in bench.render([noisy], 1, 1, 1, "azurite").splitlines()[1]


def test_each_backend_labels_what_its_numbers_are(bench: types.ModuleType) -> None:
    row = {"scenario": "x", "api": "sync", "total": 0, **dict.fromkeys(bench.KINDS, 0)}

    assert "counting fake; not Azure" in bench.render([row], 1, 1, 1)
    azurite = bench.render([row], 1, 1, 1, "azurite")
    assert "counted at the transport" in azurite and "Azurite is not Azure Storage" in azurite
    assert "counting fake" not in azurite


def _environment(bench: types.ModuleType, latency_ms: float = 0.0) -> Any:
    counters = bench.Counters(latency_ms)
    return counters, bench._azurite_environment("CONN", counters)


def test_the_azurite_environment_creates_a_private_container_and_removes_it(
    bench: types.ModuleType,
) -> None:
    _, env = _environment(bench)

    connection, container, kwargs = env.sync.built_with
    assert connection == "CONN" and container.startswith("bench-")
    assert env.sync.calls == ["create"] and "transport" in kwargs
    aio = env.make_async()
    assert aio.built_with[:2] == ("CONN", container)
    env.cleanup()
    assert env.sync.calls == ["create", "delete", "close"]


def test_the_sync_transport_counts_each_attempt_by_kind_and_passes_the_response_through(
    bench: types.ModuleType,
) -> None:
    counters, env = _environment(bench)
    transport = env.sync.built_with[2]["transport"]
    request = types.SimpleNamespace(
        method="PUT", url=f"{CONTAINER_URL}/b", headers={"If-None-Match": "*"}
    )

    assert transport.send(request, timeout=3) == ("sent", request)
    transport.send(types.SimpleNamespace(method="GET", url=f"{CONTAINER_URL}/b", headers={}))

    assert counters.snapshot() == {"GET": 1, "HEAD": 0, "PUT": 0, "PUT*": 1, "LIST": 0, "DEL": 0}


def test_the_async_transport_counts_and_never_blocks_the_loop_for_latency(
    bench: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    counters, env = _environment(bench, latency_ms=250.0)
    transport = env.make_async().built_with[2]["transport"]
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(bench.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(
        bench.time, "sleep", lambda seconds: pytest.fail("the async transport slept synchronously")
    )
    request = types.SimpleNamespace(method="HEAD", url=f"{CONTAINER_URL}/b", headers={})

    assert asyncio.run(transport.send(request)) == ("sent", request)
    assert slept == [0.25] and counters.counts["HEAD"] == 1


def test_the_sync_transport_sleeps_per_request_only_when_latency_is_set(
    bench: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(bench.time, "sleep", slept.append)
    request = types.SimpleNamespace(method="GET", url=f"{CONTAINER_URL}/b", headers={})

    _, idle = _environment(bench)
    idle.sync.built_with[2]["transport"].send(request)
    _, slow = _environment(bench, latency_ms=20.0)
    slow.sync.built_with[2]["transport"].send(request)

    assert slept == [0.02]


def test_the_cli_runs_the_azurite_backend_through_both_apis_and_labels_it(
    bench: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_run_azurite(*args: Any) -> list[dict[str, Any]]:
        calls.append(args)
        return [
            {
                "scenario": "s",
                "api": "async" if args[4] else "sync",
                "total": 0,
                **dict.fromkeys(bench.KINDS, 0),
            }
        ]

    monkeypatch.setattr(bench, "run_azurite", fake_run_azurite)
    monkeypatch.setattr(
        bench, "run", lambda *a, **k: pytest.fail("the fake ran for --backend azurite")
    )

    assert (
        bench.main(["--backend", "azurite", "--channels", "2", "--writes", "1", "--history", "1"])
        == 0
    )

    assert calls == [
        (2, 1, 1, 0.0, False, bench.AZURITE_CONNECTION_STRING),
        (2, 1, 1, 0.0, True, bench.AZURITE_CONNECTION_STRING),
    ]
    assert "Azurite is not Azure Storage" in capsys.readouterr().out


def test_the_cli_passes_a_custom_connection_string_and_defaults_to_the_fake(
    bench: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        bench,
        "run_azurite",
        lambda *args: seen.append(args[5]) or [],  # type: ignore[func-returns-value]
    )

    assert bench.main(["--backend", "azurite", "--connection-string", "CUSTOM", "--json"]) == 0
    assert seen == ["CUSTOM", "CUSTOM"]
    capsys.readouterr()
    assert bench.main(["--channels", "1", "--writes", "1", "--history", "1"]) == 0
    assert "counting fake" in capsys.readouterr().out
