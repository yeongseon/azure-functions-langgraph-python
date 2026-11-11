"""Behavioural test for the platform_thread_isolation example (offline, no model key)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from azure_functions_langgraph import LangGraphApp
from tests.example_imports import load_example_file

pytest.importorskip("langgraph_sdk")

from tests.test_sdk_compat import _make_sdk_client  # noqa: E402

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "platform_thread_isolation"


def _client() -> Any:
    graph = load_example_file(EXAMPLE_DIR / "graph.py", "_thread_isolation_graph").compiled_graph
    app = LangGraphApp(platform_compat=True)
    app.register(graph=graph, name="notebook_agent")
    return _make_sdk_client(app.function_app)


def test_same_thread_continues_and_distinct_thread_is_isolated() -> None:
    demo = load_example_file(EXAMPLE_DIR / "sdk_client.py", "_thread_isolation_client")

    values = demo.run_demo(_client())

    # Same thread repeated: its state continues across runs.
    assert values["thread_a"]["notes"] == ["alpha-1", "alpha-2"]
    assert values["thread_a"]["turns"] == 2
    # Different thread: sees none of thread A's state.
    assert values["thread_b"]["notes"] == ["beta-1"]
    assert values["thread_b"]["turns"] == 1


def test_readme_documents_limits_and_commands() -> None:
    readme = (EXAMPLE_DIR / "README.md").read_text(encoding="utf-8")
    assert "not authenticated" in readme
    assert "shared" in readme and "cross-worker" in readme
    assert "pytest tests/test_platform_thread_isolation_example.py" in readme
