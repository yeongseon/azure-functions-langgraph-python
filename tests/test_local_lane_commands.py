"""Exercise the local Makefile contract without provisioning infrastructure."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_doctor_fails_without_hatch_and_does_not_install(tmp_path: Path) -> None:
    # Given: a checkout with no bootstrapped environment.
    (tmp_path / "Makefile").write_bytes((ROOT / "Makefile").read_bytes())
    # When: read-only diagnostics run with a missing Hatch executable.
    result = subprocess.run(
        ["make", "doctor", "HATCH=/missing/hatch"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    # Then: missing test/check prerequisites cannot report success or create an environment.
    assert result.returncode != 0
    assert not (tmp_path / ".venv").exists()


def test_routine_commands_do_not_bootstrap_or_duplicate_typechecking() -> None:
    # Given: Make's execution plan for the quality gate.
    result = subprocess.run(
        ["make", "-n", "check"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    # When: inspecting the commands that will run.
    commands = result.stdout
    # Then: setup is explicit and typechecking is scheduled once.
    assert "install --upgrade pip" not in commands
    assert "install hatch" not in commands
    assert commands.count("run typecheck") == 1
    assert (
        'lint = "hatch run style && hatch run typecheck"'
        not in (ROOT / "pyproject.toml").read_text()
    )


def test_required_local_lanes_use_shared_junit_validator() -> None:
    # Given: each required local lane's execution plan.
    for target, area in (
        ("test-table", "table_store_integration"),
        ("test-cosmos", "test_cosmos_integration"),
        ("e2e-azure", "test_true_streaming_e2e"),
    ):
        # When: Make expands the target without running infrastructure.
        result = subprocess.run(
            ["make", "-n", target], cwd=ROOT, capture_output=True, text=True, check=True
        )
        # Then: pytest produces a fresh report consumed by the same fail-closed validator as CI.
        assert "mktemp" in result.stdout
        assert "--junitxml=" in result.stdout
        assert "tools/check_junit.py" in result.stdout
        assert area in result.stdout
