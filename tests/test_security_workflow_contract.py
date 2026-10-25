from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest
import yaml


def test_collectors_always_use_make_entry_points() -> None:
    # Given the security jobs, when inspecting collectors, then failures still retain evidence.
    workflow = yaml.safe_load(Path(".github/workflows/security.yml").read_text())
    for name, target in (
        ("bandit", "security-bandit-evidence"),
        ("semgrep", "security-semgrep-evidence"),
    ):
        steps = workflow["jobs"][name]["steps"]
        collectors = [step for step in steps if target in step.get("run", "")]
        assert len(collectors) == 1
        assert collectors[0]["if"] == "always()"
        assert collectors[0]["run"].startswith("make ")


@pytest.mark.parametrize(("outcome", "success"), [("success", True), ("failure", False)])
def test_semgrep_make_tolerates_only_incomplete(
    tmp_path: Path, outcome: str, success: bool
) -> None:
    # Given action-only output, when collecting via Make, then scan failure remains blocking.
    result = subprocess.run(
        [
            "make",
            "security-semgrep-evidence",
            f"SECURITY_PYTHON={sys.executable}",
            f"SECURITY_OUTPUT={tmp_path}",
            f"SCAN_OUTCOME={outcome}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == success


def test_runtime_audit_make_requires_one_wheel() -> None:
    result = subprocess.run(
        ["make", "security-runtime-audit", "WHEEL=", "SECURITY_PYTHON=false"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "one existing candidate wheel" in result.stdout + result.stderr
