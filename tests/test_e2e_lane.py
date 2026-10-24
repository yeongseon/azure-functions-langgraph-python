"""Regression tests for how the real-Azure e2e lane is started from Hatch and Make (#686)."""

from __future__ import annotations

from pathlib import Path
import shlex
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_hatch_scripts_run_the_dedicated_selector_without_coverage() -> None:
    # Coverage (fail_under = 95) fails any run that does not import src/, so even an all-green
    # e2e run would exit non-zero without --no-cov.
    hatch = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]
    scripts = hatch["envs"]["default"]["scripts"]
    for name, path in (
        ("e2e-azure", "tests/e2e/"),
        ("e2e-local", "tests/e2e/test_langgraph_e2e.py"),
    ):
        words = shlex.split(scripts[name])
        assert words[0] == "pytest"
        assert words[words.index("-m") + 1] == "e2e"
        assert {"--no-cov", path} <= set(words)
