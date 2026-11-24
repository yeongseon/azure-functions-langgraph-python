"""Offline behavior checks for the real-SDK index benchmark."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any

import pytest

from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore
from tests.table_status_index_support import (
    FakeMatchConditions,
    FakeResourceModifiedError,
    FakeResourceNotFoundError,
    FakeTable,
)

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_SPEC = importlib.util.spec_from_file_location(
    "tools", _TOOLS / "__init__.py", submodule_search_locations=[str(_TOOLS)]
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load repository benchmark tools")
if "tools" not in sys.modules:
    _PACKAGE = importlib.util.module_from_spec(_SPEC)
    sys.modules["tools"] = _PACKAGE
    _SPEC.loader.exec_module(_PACKAGE)

from tools import benchmark_table_status_index as bench  # noqa: E402
from tools.benchmark_table_store import RequestCounter  # noqa: E402


def test_index_benchmark_compares_with_seeded_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a fake service that supports seed transactions and the shipped index operations.
    class SeedTable(FakeTable):
        def submit_transaction(self, operations: list[tuple[Any, ...]]) -> list[Any]:
            if operations[0][0] == "create":
                for _, entity in operations:
                    self.create_entity(entity)
                return [None] * len(operations)
            return super().submit_transaction(operations)

    monkeypatch.setattr(
        AzureTableThreadStore,
        "_load_azure_core_symbols",
        staticmethod(
            lambda: (
                FakeResourceNotFoundError,
                FakeResourceModifiedError,
                FakeMatchConditions,
            )
        ),
    )
    # When the benchmark drives all index/off workloads and writes.
    report = bench.run_size(SeedTable(), RequestCounter(), 60, 1)
    # Then every read passed its independent oracle, and rollback reached a scan.
    assert report["audit"]["result"]["marked_ready"] is True
    assert len(report["reads"]) == 24
    assert {r["index"] for r in report["reads"]} == {False, True}
    assert len(report["writes"]) == 12
    assert report["stale_audit"]["result"]["stale"] == 2
    assert report["disabled_search"]["result"] == min(10, report["status_counts"]["idle"])


def test_named_real_connection_never_falls_back_to_azurite(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.integration.test_table_store_integration import azurite_table_client

    # Given an explicit real-endpoint selector with a missing credential variable.
    monkeypatch.setenv("TABLE_TEST_CONNECTION_STRING_ENV", "MISSING_BENCH_CS")
    monkeypatch.delenv("MISSING_BENCH_CS", raising=False)
    # When the existing fixture is requested, then it fails rather than silently using Azurite.
    with pytest.raises(pytest.fail.Exception, match="environment variable is empty"):
        next(inspect.unwrap(azurite_table_client)())
