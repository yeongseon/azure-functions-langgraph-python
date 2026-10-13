from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
import sys
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp, LoggingRunObserver

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "examples" / "observability_app_insights"
REQUEST_ID = "demo-request-001"
THREAD_ID = "demo-thread"


def _load_graph() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_observability_example_graph", EXAMPLE_DIR / "graph.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.compiled_graph


def test_observability_transcript_matches_live_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: the shipped graph with LoggingRunObserver and fixed safe correlation IDs
    logger_name = "tests.observability.transcript"
    app = LangGraphApp(observer=LoggingRunObserver(logging.getLogger(logger_name)))
    app.register(graph=_load_graph(), name="observability_app_insights")
    app.function_app.functions_bindings = {}
    handler = next(
        fn.get_user_function()
        for fn in app.function_app.get_functions()
        if fn.get_function_name() == "aflg_observability_app_insights_invoke"
    )

    # When: the real registered invoke handler processes the documented request
    with caplog.at_level(logging.INFO, logger=logger_name):
        response = handler(
            func.HttpRequest(
                method="POST",
                url="/api/graphs/observability_app_insights/invoke",
                body=json.dumps(
                    {
                        "input": {"user_text": "Ada"},
                        "config": {"configurable": {"thread_id": THREAD_ID}},
                    }
                ).encode(),
                headers={"Content-Type": "application/json", "X-Request-ID": REQUEST_ID},
                route_params={"name": "observability_app_insights"},
            )
        )
    records = [
        record.langgraph_run for record in caplog.records if hasattr(record, "langgraph_run")
    ]
    completed = {**records[1], "duration_ms": "<duration_ms>"}
    transcript = (EXAMPLE_DIR / "README.md").read_text().split("## Captured local records", 1)[1]

    # Then: documented fields and response correlation are exact live values
    assert json.dumps(records[0], separators=(",", ":")) in transcript
    assert json.dumps(completed, separators=(",", ":")) in transcript
    assert response.headers["X-Request-ID"] == REQUEST_ID
    assert f"X-Request-ID: {REQUEST_ID}" in transcript
    assert (
        set(records[0])
        == set(records[1])
        == {
            "graph_name",
            "endpoint",
            "run_id",
            "thread_id",
            "assistant_id",
            "invocation_id",
            "stream_mode",
            "transport",
            "has_checkpointer",
            "lock_backend",
            "trigger_type",
            "duration_ms",
            "status",
        }
    )
    assert "Operator guidance" in transcript
