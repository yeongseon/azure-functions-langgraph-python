from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest
import requests

from scripts import certify_primary_examples as certification
from tools import verification_host as host
from tools.verification_evidence import Summary, report, save


def test_report_skips_foreign_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Given resolver and verification schemas, when reporting, then only verification is published.
    (tmp_path / "resolver").mkdir()
    (tmp_path / "resolver/summary.json").write_text('{"lane":"candidate","examples":{}}')
    save(tmp_path, Summary(1, "test", (), 1, 1, 0, 0, "PASS", 0))
    assert report(tmp_path) == 0
    assert "Skipping foreign summary" in capsys.readouterr().out


def test_certification_venvs_stay_outside_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a certification output tree, when installing, then all environments are temporary.
    config = certification.parse_args(["published", "--output-dir", str(tmp_path / "evidence")])
    environments: list[Path] = []

    def run(command: list[str], **kwargs: object) -> str:
        if command[1] == "venv":
            environments.append(Path(command[-1]))
        return "ok"

    monkeypatch.setattr(certification, "_run", run)
    monkeypatch.setattr(certification, "_smoke", lambda *a: ("fake", "real"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Mock(returncode=1, stdout="", stderr=""))
    certification.certify(config)
    assert environments
    assert all(not path.is_relative_to(config.output_dir) for path in environments)
    assert all(not path.exists() for path in environments)


def test_host_preserves_failing_streaming_junit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an executed streaming failure, when aggregating hosts, then FAIL and its count survive.
    monkeypatch.chdir(tmp_path)
    for service in ("BLOB", "QUEUE", "TABLE"):
        monkeypatch.setenv(f"AZURITE_{service}_PORT", "12345")
    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(host, "azurite", lambda *a: nullcontext())
    monkeypatch.setattr(host, "child", lambda *a: nullcontext())
    monkeypatch.setattr(shutil, "copytree", lambda source, app, **k: app.mkdir())
    monkeypatch.setattr(requests, "get", lambda *a, **k: Mock(status_code=200))
    response = Mock()
    response.json.return_value = {
        "output": {"messages": [{"type": "ai", "content": "host smoke passed"}]}
    }
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    monkeypatch.setattr(host, "run", lambda *a: 0)

    def execute(*args: object) -> Summary:
        path = tmp_path / "test-results/streaming-host/junit.xml"
        path.parent.mkdir(parents=True)
        path.write_text(
            '<testsuite tests="1" failures="1" errors="0" skipped="0">'
            "<testcase><failure/></testcase></testsuite>"
        )
        return Summary(1, "streaming-host", (), 1, 0, 1, 0, "FAIL", 7)

    monkeypatch.setattr(host, "execute", execute)
    assert host.main() == 7
    summary = json.loads((tmp_path / "test-results/host-smoke/summary.json").read_text())
    assert summary["status"] == "FAIL"
    assert summary["failed"] >= 1


def test_host_readiness_retries_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a transient read timeout, when waiting, then the next ready response succeeds.
    get = Mock(side_effect=[TimeoutError(), Mock(status_code=200)])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    host.wait_ready("http://127.0.0.1:1", "native")
    assert get.call_count == 2


def test_check_records_failed_quality_command(tmp_path: Path) -> None:
    # Given a failed lint process, when using the check entry point, then its log and code survive.
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "tools").symlink_to(root / "tools", target_is_directory=True)
    (tmp_path / "Makefile").write_text(
        (root / "Makefile").read_text() + "\nreview-failure:\n\t@echo lint-failed; exit 7\n"
    )
    result = subprocess.run(
        [
            "make",
            "check",
            "CI=1",
            f"PYTHON={sys.executable}",
            f"RUN={sys.executable}",
            "QUALITY_TARGETS=review-failure",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    summary = json.loads((tmp_path / "test-results/check/summary.json").read_text())
    assert summary["status"] == "FAIL"
    assert "lint-failed" in (tmp_path / "test-results/check/logs/runner.log").read_text()
