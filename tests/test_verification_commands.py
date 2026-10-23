from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from tools import verification
from tools.verification import targets
from tools.verification_environment import clean
from tools.verification_lanes import execute, pytest_command
from tools.verification_process import run


def test_offline_selection_does_not_collect_live_fixtures() -> None:
    # Given the offline command, when selecting tests, then live trees are excluded before imports.
    command = pytest_command("test", [])
    assert "not e2e and not integration" in command
    assert "--ignore=tests/integration" in command
    assert "--ignore=tests/e2e" in command
    assert "--ignore=tests/test_production_persistent_agent_example.py" in command


def test_unconfigured_azure_fails_before_test_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given no deployed candidate, when executing Azure, then preflight fails closed.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("E2E_BASE_URL", raising=False)
    summary = execute("azure-e2e", [])
    assert summary.status == "INCOMPLETE"
    assert summary.exit_code != 0
    assert summary.selected == 0


def test_verify_does_not_reuse_previous_lane_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given stale success, when a required command produces no evidence, then it is incomplete.
    monkeypatch.chdir(tmp_path)
    old = tmp_path / "test-results/test/summary.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"status":"PASS"}')
    monkeypatch.setattr(verification, "run", lambda *a, **k: 0)
    assert targets("verify", ("test",)) != 0
    assert (
        json.loads((tmp_path / "test-results/verify/summary.json").read_text())["status"]
        == "INCOMPLETE"
    )


def test_clean_preserves_environments_and_shared_data(tmp_path: Path) -> None:
    # Given generated and external directories, when cleaning, then only artifacts go.
    for name in ("src", ".venv", "shared-azurite", "test-results", "dist", "site"):
        (tmp_path / name).mkdir()
    clean(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".venv", "shared-azurite", "src"]


def test_child_status_and_timeout_are_preserved(tmp_path: Path) -> None:
    # Given a failing command, when running, then retain its status.
    assert run([sys.executable, "-c", "raise SystemExit(7)"], tmp_path / "log") == 7


def test_child_timeout_cleans_owned_process(tmp_path: Path) -> None:
    # Given a blocked child, when its deadline expires, then reap it and report timeout.
    assert run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path / "log", 0.1) == 124
