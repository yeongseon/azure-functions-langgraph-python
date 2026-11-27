"""Exercise the Make/Hatch caller with real lints, without provisioning dependencies."""

from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def lint_repo(tmp_path: Path) -> Path:
    pytest.importorskip("hatch", reason="requires the dev extra for the local Hatch caller")
    shutil.copy(ROOT / "Makefile", tmp_path)
    shutil.copytree(ROOT / "tools", tmp_path / "tools")
    shutil.copytree(ROOT / ".github/workflows", tmp_path / ".github/workflows")
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    commands = config["tool"]["hatch"]["envs"]["default"]["scripts"]["lint-workflows"]
    # A detached environment runs the actual scripts without installing the library.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "lint-entrypoint-test"\nversion = "0.0.0"\n'
        "[tool.hatch.envs.default]\ndetached = true\n"
        f"python = {sys.executable!r}\n"
        '[tool.hatch.envs.pinned]\npython = "3.11"\n'
        "[tool.hatch.envs.default.scripts]\n"
        f"lint-workflows = {commands!r}\n"
    )
    return tmp_path


def run_lints(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "lint-workflows", f"HATCH={shutil.which('hatch')}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_lint_entrypoint_runs_all_checks_when_clean(lint_repo: Path) -> None:
    result = run_lints(lint_repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Release-gate workflows:" in result.stdout
    assert "Workflow pin-hygiene:" in result.stdout
    assert "Hatch/matrix hygiene:" in result.stdout


@pytest.mark.parametrize(
    ("workflow", "content", "completed"),
    [
        (
            "publish-pypi.yml",
            "jobs:\n  publish:\n    needs: [build]\n",
            0,
        ),
        (
            "pilot.yml",
            "steps:\n  - uses: example/action@main\n",
            1,
        ),
        (
            "pilot.yml",
            'python-version: ["3.11", "3.12"]\nrun: hatch run pytest\n',
            2,
        ),
    ],
)
def test_lint_entrypoint_fails_when_any_check_rejects(
    lint_repo: Path, workflow: str, content: str, completed: int
) -> None:
    (lint_repo / ".github/workflows" / workflow).write_text(content)

    result = run_lints(lint_repo)

    assert result.returncode != 0
    assert workflow in result.stderr
    successes = ("Release-gate workflows:", "Workflow pin-hygiene:", "Hatch/matrix hygiene:")
    assert all(message in result.stdout for message in successes[:completed])
    assert all(message not in result.stdout for message in successes[completed:])
