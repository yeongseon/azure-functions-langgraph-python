"""Offline tests for the real-Azure options of ``tools/benchmark_table_store.py``."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

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

    # Then they are all counted, and reset_counters zeroes the per-execution count only
    assert proxy.requests_measured == 3
    proxy.reset_counters()
    assert proxy.requests_measured == 0
    assert counter.total == 3

    # And a later send restarts the per-execution count while the whole-run total keeps growing
    transport.send(object())
    assert proxy.requests_measured == 1
    assert counter.total == 4


def test_proxy_without_counter_reports_no_measured_requests(bench: ModuleType) -> None:
    assert bench.CountingProxy(object()).requests_measured is None


def test_run_azure_requires_the_environment_variable(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BENCH_TEST_CONN", raising=False)
    with pytest.raises(SystemExit, match="BENCH_TEST_CONN"):
        bench.run_azure([10], 1, "BENCH_TEST_CONN")


def test_run_azure_returns_every_request_the_transport_sent(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a service double that receives the counting transport, and a run that sends 5 requests
    transport_mod = pytest.importorskip("azure.core.pipeline.transport")
    tables_mod = pytest.importorskip("azure.data.tables")
    monkeypatch.setattr(
        transport_mod.RequestsTransport, "send", lambda self, request, **kw: "response"
    )
    monkeypatch.setenv("BENCH_TEST_CONN", "not-a-real-connection-string")
    captured: dict[str, Any] = {}

    class _Service:
        @classmethod
        def from_connection_string(cls, conn: str, **kwargs: Any) -> _Service:
            captured["transport"] = kwargs["transport"]
            return cls()

    def fake_run_tables(
        service: object, backend: str, sizes: list[int], reps: int, counter: Any = None
    ) -> list[dict[str, int]]:
        for _ in range(5):
            captured["transport"].send(object())
        counter.sent = 0  # a per-execution reset must not lower the whole-run total
        return [{"size": 10}]

    monkeypatch.setattr(tables_mod, "TableServiceClient", _Service)
    monkeypatch.setattr(bench, "run_tables", fake_run_tables)

    # When the real-Azure entry point runs
    rows, whole_run_requests = bench.run_azure([10], 1, "BENCH_TEST_CONN")

    # Then it hands back the rows and the transport total of the whole run
    assert rows == [{"size": 10}]
    assert whole_run_requests == 5


def _counts(read: int, modeled: int, measured: int | None) -> dict[str, int | None]:
    return {"entities_read": read, "requests_modeled": modeled, "requests_measured": measured}


def _row(
    timed: list[dict[str, int | None]], memory_pass: dict[str, int | None]
) -> dict[str, object]:
    return {
        "size": 10,
        "workload": "w",
        "timed": timed,
        "memory_pass": memory_pass,
        "peak_mem_mib": 0.1,
        "latency_ms_median": 5.0,
        "latency_ms_max": 6.0,
        "result": 3,
    }


class _ScriptedRun:
    """Counter double plus a workload: each execution reports the next scripted counts."""

    def __init__(self, script: list[tuple[int, int, int | None]]) -> None:
        self._script = iter(script)
        self.entities_read = 0
        self.requests = 0
        self.requests_measured: int | None = None

    def reset_counters(self) -> None:
        self.entities_read = 0
        self.requests = 0
        self.requests_measured = None

    def call(self, store: object) -> list[int]:
        self.entities_read, self.requests, self.requests_measured = next(self._script)
        return [1, 2, 3]


def test_measure_keeps_the_counts_of_every_timed_repetition_and_the_memory_pass(
    bench: ModuleType,
) -> None:
    # Given a workload whose second timed repetition needed a retry (3 requests, not 2)
    run = _ScriptedRun([(2000, 2, 2), (2000, 2, 3), (2000, 2, 2), (2000, 2, 2)])
    # When it is measured with three timed repetitions
    result = bench.measure(object(), run, run.call, 3)
    # Then each timed repetition keeps its own counts and the memory pass is reported apart
    assert [c["requests_measured"] for c in result["timed"]] == [2, 3, 2]
    assert result["memory_pass"] == _counts(2000, 2, 2)
    assert result["result"] == 3
    # And there is no single top-level count that could stand for all of them
    assert not {"entities_read", "requests_modeled", "requests_measured"} & set(result)


def test_report_flags_a_retry_hidden_in_a_timed_repetition(bench: ModuleType) -> None:
    # Given a row whose memory pass is clean but whose second timed repetition sent 3 requests
    row = _row([_counts(2000, 2, 2), _counts(2000, 2, 3), _counts(2000, 2, 2)], _counts(2000, 2, 2))
    # When the report is formatted for real Azure
    lines = bench.format_report("azure", [row]).splitlines()
    # Then the label says real Azure and every timed repetition is shown next to the memory pass
    assert lines[0].startswith("# backend=azure: REAL Azure")
    assert "| 10 | w | 2000 | 2 | 2/3/2 | 2 | 0.1 | 5.0 | 6.0 | 3 |" in lines
    # And the totals cover all four executions and the retry is named, not averaged away
    assert any("measured 9, modeled 8" in line for line in lines)
    assert any(
        line.startswith("WARNING: measured differs from modeled in 1 of 4") for line in lines
    )
    assert "- size 10, w, timed rep 2: measured 3, modeled 2" in lines
    assert not any("Measured equals modeled" in line for line in lines)


def test_report_states_no_extra_requests_only_when_every_execution_matches(
    bench: ModuleType,
) -> None:
    # Given a row whose three timed repetitions and memory pass all sent the modeled requests
    row = _row([_counts(2000, 2, 2)] * 3, _counts(2000, 2, 2))
    # When the report is formatted
    lines = bench.format_report("azure", [row]).splitlines()
    # Then the measured total spans every execution and the no-extra-requests claim is made
    assert "| 10 | w | 2000 | 2 | 2/2/2 | 2 | 0.1 | 5.0 | 6.0 | 3 |" in lines
    assert any("measured 8, modeled 8" in line for line in lines)
    assert any(line.startswith("Measured equals modeled in every one of the 4") for line in lines)
    assert not any(line.startswith("WARNING") for line in lines)


def test_report_prints_the_whole_run_total_only_when_the_transport_counted_it(
    bench: ModuleType,
) -> None:
    # Given one clean real-Azure row (4 executions of 2 requests = 8 query requests)
    row = _row([_counts(2000, 2, 2)] * 3, _counts(2000, 2, 2))
    # When the whole-run transport total (queries plus seeding and table create/delete) is given
    with_total = bench.format_report("azure", [row], 120).splitlines()
    without_total = bench.format_report("azure", [row]).splitlines()
    # Then it is reported next to the query total, and omitted when nothing counted it
    assert any(
        line.startswith("Whole run, every request the transport sent") for line in with_total
    )
    assert any(line.endswith("retries included): 120.") for line in with_total)
    assert not any(line.startswith("Whole run") for line in without_total)


def test_report_labels_memory_as_traced_allocations_not_process_memory(bench: ModuleType) -> None:
    # Given any row
    row = _row([_counts(2000, 2, 2)] * 3, _counts(2000, 2, 2))
    # When the report is formatted
    report = bench.format_report("azure", [row])
    # Then the column says traced and a note says what it excludes
    assert "| peak traced MiB |" in report
    assert "peak traced MiB: tracemalloc peak of the additional Python allocations" in report
    assert "not process RSS" in report
    assert "| peak MiB |" not in report


def test_report_marks_unmeasured_backends_and_models_the_total(bench: ModuleType) -> None:
    # Given a fake-backend row: no transport, so nothing was measured
    row = _row([_counts(1500, 2, None)] * 2, _counts(1500, 2, None))
    # When the report is formatted
    lines = bench.format_report("fake", [row]).splitlines()
    # Then it is labelled as not Azure, shows n/a for measured counts and claims nothing measured
    assert lines[0].startswith("# backend=fake: NOT Azure Storage")
    assert "| 10 | w | 1500 | 2 | n/a | n/a | 0.1 | 5.0 | 6.0 | 3 |" in lines
    assert any(line.startswith("Requests were not measured on this backend") for line in lines)
    assert not any("Measured equals modeled" in line for line in lines)


def test_fake_backend_rows_feed_the_report_end_to_end(bench: ModuleType) -> None:
    # Given a tiny fake-backend run: 7 workloads, 2 timed repetitions plus 1 memory pass each
    rows = bench.run_fake([60], 2)
    # Then every row carries one count set per timed repetition and one for the memory pass
    assert all(len(r["timed"]) == 2 and "memory_pass" in r for r in rows)
    # And the report formats them and models the total over all 21 executions
    report = bench.format_report("fake", rows)
    assert "Modeled total over 21 executions" in report


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
