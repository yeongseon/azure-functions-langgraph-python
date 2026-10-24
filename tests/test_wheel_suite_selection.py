from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def test_wheel_config_collects_runtime_but_not_repository_tooling(tmp_path: Path) -> None:
    # Given runtime tests and repo-tooling tests with unavailable source-only imports.
    config = Path(__file__).resolve().parents[1] / "tools/wheel-tests.ini"
    (tmp_path / "test_app.py").write_text("def test_runtime():\n    assert True\n")
    for name in (
        "test_audit_candidate.py",
        "test_candidate_inventory.py",
        "test_security_workflow_contract.py",
        "test_audit_exception_policy.py",
        "test_security_evidence.py",
        "test_host_smoke_contract.py",
    ):
        (tmp_path / name).write_text("raise RuntimeError('repository tooling collected')\n")
    # When collecting from an isolated directory with the installed-wheel configuration.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(config), "--collect-only", str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    # Then the runtime test survives and source tooling is never imported.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 test collected" in result.stdout
