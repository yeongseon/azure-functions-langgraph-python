import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from tools.security_evidence import run_bandit
from tools.security_report import Status


@pytest.mark.parametrize("scope", ["src", "examples", "scripts", "tools", "tests/host_smoke"])
@pytest.mark.parametrize("missing", [False, True])
def test_each_required_surface_must_contain_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, missing: bool
) -> None:
    # Given: aggregate inventory exceeds the floor but one required surface is empty or absent.
    monkeypatch.chdir(tmp_path)
    metrics = {}
    for surface in ("src", "examples", "scripts", "tools", "tests/host_smoke"):
        directory = tmp_path / surface
        if surface == scope and missing:
            continue
        directory.mkdir(parents=True)
        if surface != scope:
            for index in range(26):
                path = directory / f"module{index}.py"
                path.write_text("value = 1\n")
                metrics[path.relative_to(tmp_path).as_posix()] = {"loc": 1}
    (tmp_path / "security").mkdir()
    (tmp_path / "security/bandit-policy.json").write_text(
        '{"exceptions": [], "inline_suppressions": []}'
    )
    raw = json.dumps({"metrics": {**metrics, "_totals": {"loc": 104}}, "errors": [], "results": []})
    # When: the scanner reports exactly the nonempty on-disk files.
    with patch(
        "tools.security_evidence.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, raw),
    ):
        result = run_bandit()
    # Then: aggregate coverage cannot hide a missing execution surface.
    assert result.status == Status.INCOMPLETE


def test_exception_processing_preserves_incomplete_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: every surface exists but the scan falls below the file floor.
    monkeypatch.chdir(tmp_path)
    metrics = {}
    for surface in ("src", "examples", "scripts", "tools", "tests/host_smoke"):
        directory = tmp_path / surface
        directory.mkdir(parents=True)
        (directory / "one.py").write_text("value = 1\n")
        metrics[f"{surface}/one.py"] = {"loc": 1}
    (tmp_path / "security").mkdir()
    (tmp_path / "security/bandit-policy.json").write_text(
        '{"exceptions": [], "inline_suppressions": []}'
    )
    raw = json.dumps({"metrics": {**metrics, "_totals": {"loc": 5}}, "errors": [], "results": []})
    # When: an otherwise clean scan enters the exception processor.
    with patch(
        "tools.security_evidence.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, raw),
    ):
        result = run_bandit()
    # Then: no policy can convert incomplete scope into PASS.
    assert result.status == Status.INCOMPLETE
