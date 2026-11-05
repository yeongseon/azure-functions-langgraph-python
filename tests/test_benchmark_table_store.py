"""Offline tests for the real-Azure options of ``tools/benchmark_table_store.py``."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tools" / "benchmark_table_store.py"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark_table_store_under_test", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_counting_transport_counts_each_send_and_proxy_resets_it(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a proxy wired to a counter whose transport never touches the network
    transport_mod = pytest.importorskip("azure.core.pipeline.transport")
    RequestsTransport = transport_mod.RequestsTransport

    monkeypatch.setattr(RequestsTransport, "send", lambda self, request, **kw: "response")
    counter = bench.RequestCounter()
    transport = bench.counting_transport(counter)
    proxy = bench.CountingProxy(object(), counter)
    assert proxy.requests_measured == 0

    # When the transport sends three requests (retries are sends too)
    assert [transport.send(object()) for _ in range(3)] == ["response"] * 3

    # Then they are all counted, and reset_counters zeroes them
    assert proxy.requests_measured == 3
    proxy.reset_counters()
    assert proxy.requests_measured == 0


def test_proxy_without_counter_reports_no_measured_requests(bench: ModuleType) -> None:
    assert bench.CountingProxy(object()).requests_measured is None


def test_run_azure_requires_the_environment_variable(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BENCH_TEST_CONN", raising=False)
    with pytest.raises(SystemExit, match="BENCH_TEST_CONN"):
        bench.run_azure([10], 1, "BENCH_TEST_CONN")


def test_report_labels_real_azure_and_marks_missing_measured_counts(bench: ModuleType) -> None:
    row = {
        "size": 10,
        "workload": "w",
        "entities_read": 10,
        "requests_modeled": 1,
        "requests_measured": 2,
        "peak_mem_mib": 0.1,
        "latency_ms_median": 5.0,
        "latency_ms_max": 6.0,
        "result": 3,
    }
    azure = bench.format_report("azure", [row])
    fake = bench.format_report("fake", [{**row, "requests_measured": None}])
    assert azure.splitlines()[0].startswith("# backend=azure: REAL Azure")
    assert "| 10 | w | 10 | 1 | 2 | 0.1 | 5.0 | 6.0 | 3 |" in azure
    assert "NOT Azure Storage" in fake
    assert "| 1 | n/a |" in fake


class _FakeService:
    """TableServiceClient double: records creates/deletes, can fail seeding."""

    def __init__(self, existing: set[str], fail_on_create: bool = False) -> None:
        self.tables = set(existing)
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.fail_on_create = fail_on_create

    def create_table(self, name: str) -> object:
        if name in self.tables:
            raise RuntimeError("exists")
        self.tables.add(name)
        self.created.append(name)
        if self.fail_on_create:
            raise RuntimeError("seeding failed")
        return self

    def delete_table(self, name: str) -> None:
        self.tables.discard(name)
        self.deleted.append(name)


def test_run_tables_deletes_only_tables_it_created_after_a_seeding_failure(
    bench: ModuleType,
) -> None:
    # Given a pre-existing user table named like another requested size
    service = _FakeService({"bench10000"})
    # When seeding the first size fails (submit_transaction is missing on the double)
    with pytest.raises(AttributeError):
        bench.run_tables(service, "azure", [1000, 10000], 1)
    # Then only the table created in this run is deleted and the user's table survives
    assert len(service.created) == 1 and service.created[0].startswith("bench1000r")
    assert service.deleted == service.created
    assert "bench10000" in service.tables


def test_run_tables_uses_unique_names_per_run_and_never_reuses_tables(bench: ModuleType) -> None:
    first, second = _FakeService(set()), _FakeService(set())
    for service in (first, second):
        with pytest.raises(AttributeError):
            bench.run_tables(service, "azure", [1000], 1)
    assert first.created != second.created
