"""Offline smoke test for examples/service_bus_sessions (issue #823)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.example_imports import load_example_file

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "service_bus_sessions"


@pytest.fixture
def demo() -> dict[str, Any]:
    module = load_example_file(EXAMPLE_DIR / "session_demo.py", "_examples_sb_sessions_demo")
    result: dict[str, Any] = module.run_demo()
    return result


def test_same_session_shares_thread_and_other_session_is_isolated(demo: dict[str, Any]) -> None:
    assert demo["same_session"] == {"seen": ["a", "b"], "turns": 2}
    assert demo["other_session"] == {"seen": ["c"], "turns": 1}


def test_redelivery_reruns_graph_without_dedup(demo: dict[str, Any]) -> None:
    assert demo["redelivered"] == {"seen": ["a", "b", "b"], "turns": 3}


def test_missing_session_and_factory_mismatch_rejected_before_graph(demo: dict[str, Any]) -> None:
    assert "session_id" in demo["missing_session"]
    assert demo["missing_session_ran_graph"] is False
    assert "must match" in demo["factory_mismatch"]


def test_package_lock_contention_and_exclusive_bypass(demo: dict[str, Any]) -> None:
    assert "currently in use" in demo["contention"]
    assert demo["exclusive"] == {"seen": ["e"], "turns": 1}


def test_unsupported_exclusive_topologies_are_rejected(demo: dict[str, Any]) -> None:
    assert "session-exclusive" in demo["reject_http"]
    assert "session-exclusive" in demo["reject_no_session"]
    assert "single-message cardinality" in demo["reject_batch"]


def test_function_app_registers_session_enabled_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_BUS_QUEUE_NAME", "sessions-q")
    module = load_example_file(EXAMPLE_DIR / "function_app.py", "_examples_sb_sessions_app")
    reg = module.langgraph_app._sb_registrations["service_bus_sessions"]
    assert reg.session is True
    assert reg.queue_name == "sessions-q"
    assert reg.binding_kwargs["is_sessions_enabled"] is True
