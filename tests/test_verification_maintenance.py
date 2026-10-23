from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("target", ["doctor", "report", "clean"])
def test_maintenance_does_not_create_hatch_environment(tmp_path: Path, target: str) -> None:
    # Given a fresh Hatch location, when maintaining a checkout, then no environment is created.
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(
        root / "tools", tmp_path / "tools", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copyfile(root / "Makefile", tmp_path / "Makefile")
    shutil.copyfile(root / "pyproject.toml", tmp_path / "pyproject.toml")
    data = tmp_path / "hatch-data"
    env = {
        **os.environ,
        "HATCH_DATA_DIR": str(data),
        "HATCH_ENV_TYPE_VIRTUAL_PATH": str(data / "envs"),
    }
    env.pop("CI", None)
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(["make", target], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode in (0, 2), result.stderr
    assert not list(data.rglob("pyvenv.cfg"))
