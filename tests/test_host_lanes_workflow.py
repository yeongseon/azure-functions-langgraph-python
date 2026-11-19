"""The host-lanes workflow keeps its filter and wiring in step with what the lanes run.

``.github/workflows/ci-host-lanes.yml`` runs ``make host-lanes`` only when a path changes that the
lanes read, so a filter that misses one would let a change to it merge unexercised. This is the same
guard ``tests/test_table_integration_workflow.py`` keeps for the Azurite lane, and it reuses that
module's helpers. The lane is local evidence on an emulator: it must not become a release gate.
"""

from __future__ import annotations

from pathlib import Path
import re
import tomllib
from typing import Any, cast

import pytest

from tests.test_table_integration_workflow import (
    _covers,
    _lane_test_closure,
    _plain_path,
    _workflow,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci-host-lanes.yml"
EVENTS = ["pull_request", "push"]
LANE_TEST_MODULES = (
    "tests/e2e/test_langgraph_lock_e2e.py",
    "tests/host_lanes/test_platform_sdk.py",
)
# Files the lanes copy, import or read besides the test modules and what they import.
LANE_INPUTS = (
    "src/azure_functions_langgraph/app.py",
    "examples/e2e_app/function_app.py",
    "examples/e2e_app/graph.py",
    "examples/e2e_app/lock_graph.py",
    "examples/e2e_app/host.json",
    "tests/host_lanes/platform_app/function_app.py",
    "tests/host_lanes/platform_app/host.json",
    "tests/data/platform_compat_manifest.json",
    "tools/run_host_lanes.py",
    "tools/run_host_smoke.py",
    "tools/check_junit.py",
    "Makefile",
    "pyproject.toml",
    ".github/workflows/ci-host-lanes.yml",
)


def _triggers() -> dict[str, Any]:
    workflow = _workflow(WORKFLOW)
    # PyYAML follows YAML 1.1, so the workflow's `on:` key loads as the boolean True.
    return cast(dict[str, Any], workflow[True] if True in workflow else workflow["on"])


def _paths(event: str) -> list[str]:
    return cast(list[str], _triggers()[event]["paths"])


def _steps() -> list[dict[str, Any]]:
    jobs = cast(dict[str, Any], _workflow(WORKFLOW)["jobs"])
    return [step for job in jobs.values() for step in job["steps"]]


def _step_running(marker: str) -> dict[str, Any]:
    steps = [step for step in _steps() if marker in step.get("run", "")]
    assert len(steps) == 1, marker
    return steps[0]


def test_the_filters_of_pull_request_and_push_list_the_same_paths() -> None:
    assert sorted(_paths("push")) == sorted(_paths("pull_request"))


@pytest.mark.parametrize("event", EVENTS)
def test_every_entry_of_the_filter_names_something_that_exists(event: str) -> None:
    missing = []
    for pattern in _paths(event):
        entry, is_directory = _plain_path(pattern)
        target = ROOT / entry
        if not (target.is_dir() if is_directory else target.is_file()):
            missing.append(pattern)

    assert missing == [], f"on.{event}.paths lists paths that do not exist"


@pytest.mark.parametrize("event", EVENTS)
def test_the_filter_covers_every_input_of_the_lanes(event: str) -> None:
    entries = _paths(event)
    closure = _lane_test_closure(ROOT, LANE_TEST_MODULES)

    uncovered = sorted(
        path for path in closure | set(LANE_INPUTS) if not any(_covers(e, path) for e in entries)
    )

    assert uncovered == [], (
        f"a change to these paths would not start the host lanes on {event}: add them to "
        f"on.{event}.paths of {WORKFLOW.relative_to(ROOT).as_posix()}"
    )


def test_the_lane_test_modules_are_the_ones_the_runner_runs() -> None:
    source = (ROOT / "tools" / "run_host_lanes.py").read_text(encoding="utf-8")

    for module in LANE_TEST_MODULES:
        assert f'"{module}"' in source, module
        assert (ROOT / module).is_file(), module


def test_the_workflow_installs_the_extras_of_the_hatch_environment_the_make_target_uses() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    features = set(pyproject["tool"]["hatch"]["envs"]["table"]["features"])
    install = next(step for step in _steps() if "pip install -e" in step.get("run", ""))

    extras = re.search(r"\.\[([^\]]+)\]", install["run"])

    assert extras is not None
    assert set(extras.group(1).split(",")) == features


def test_the_workflow_runs_the_make_target_with_the_interpreter_it_installed_into() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    command = _step_running("make host-lanes")["run"]

    assert re.search(r"^host-lanes:", makefile, re.MULTILINE)
    assert "LANES_PYTHON=python" in command and "HATCH=python" in command


def test_the_workflow_holds_read_only_permissions_and_uses_no_secret() -> None:
    workflow = _workflow(WORKFLOW)
    text = WORKFLOW.read_text(encoding="utf-8")

    assert workflow["permissions"] == {"contents": "read"}
    assert not [name for name, job in workflow["jobs"].items() if "permissions" in job]
    for forbidden in ("secrets.", "pull_request_target", "workflow_run", "continue-on-error"):
        assert forbidden not in text, forbidden


def test_the_job_is_bounded_in_time() -> None:
    jobs = cast(dict[str, Any], _workflow(WORKFLOW)["jobs"])

    assert [1 <= job["timeout-minutes"] <= 30 for job in jobs.values()] == [True]


@pytest.mark.parametrize("gate", ["ci-test.yml", "publish-pypi.yml"])
def test_the_lane_is_not_wired_into_a_required_check_or_the_release_gate(gate: str) -> None:
    text = (ROOT / ".github" / "workflows" / gate).read_text(encoding="utf-8")

    assert "host-lanes" not in text
    assert "ci-host-lanes" not in text
