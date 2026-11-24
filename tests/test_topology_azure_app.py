"""Offline build/export checks; these do not claim Azure or storage execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
import warnings

import azure.functions as func
import pytest

from tests.example_imports import load_example_file
from tests.topology_truth_table import RULE_PATTERNS

pytest.importorskip("azure.storage.blob")
pytest.importorskip("azure.data.tables")

APP = Path(__file__).resolve().parents[1] / "examples/e2e_topology_app/topology_app.py"
CONNECTION = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=YWJjZA==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)


@pytest.fixture
def topology_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AzureWebJobsStorage", CONNECTION)
    monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "distributed")
    monkeypatch.setenv("AZFUNC_LANGGRAPH_THREAD_STORE", "distributed")
    monkeypatch.setenv("TOPOLOGY_MODE", "safe")
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "offline-fixture-not-Azure")


@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        ("lock", "lock-backend-declared"),
        ("store", "thread-store-declared"),
        ("checkpointer", "process-local-checkpointer-declared"),
    ],
)
def test_unsafe_final_wiring_is_rejected_before_export(
    topology_environment: None, monkeypatch: pytest.MonkeyPatch, mode: str, rule: str
) -> None:
    module = load_example_file(APP, "topology_fixture")
    monkeypatch.setenv("TOPOLOGY_MODE", mode)
    app = module.create_app(os.environ)

    with pytest.raises(RuntimeError, match=RULE_PATTERNS[rule]):
        _ = app.function_app

    assert app._function_app is None
    assert not app._optional_routes_exported


@pytest.mark.parametrize("mode", ["safe", "warnings"])
def test_export_reports_actual_build_findings_without_storage_io(
    topology_environment: None, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    module = load_example_file(APP, "topology_fixture")
    monkeypatch.setenv("TOPOLOGY_MODE", mode)
    if mode == "warnings":
        monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "")
        monkeypatch.setenv("AZFUNC_LANGGRAPH_THREAD_STORE", "")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app = module.export_app(os.environ)
    functions = app.get_functions()
    [route] = [f for f in functions if f.get_function_name() == "topology_evidence"]
    response = route.get_user_function()(func.HttpRequest("GET", "http://test", body=b""))
    evidence = json.loads(response.get_body())

    expected = (
        {"thread-store-hosted", "process-local-checkpointer-hosted", "inprocess-lock-hosted"}
        if mode == "warnings"
        else set()
    )
    assert {f["rule"] for f in evidence["findings"]} == expected
    assert len([w for w in caught if issubclass(w.category, RuntimeWarning)]) == len(expected)
    assert evidence["env"]["azure_hosted"] is True
    assert "aflg_health" in {f.get_function_name() for f in functions}
