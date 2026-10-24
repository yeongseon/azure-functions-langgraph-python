"""Regression tests for the real-Azure e2e certification workflow (#686).

``e2e-azure.yml`` is the Azure gate of every release, and pytest exiting 0 does not prove that its
tests ran: each e2e module skips itself when its input is unset. Both pytest runs of the workflow
must therefore be followed by ``tools/check_junit.py`` on the report they wrote, nothing may mask
that validator, and the certification must not be recorded without it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "e2e-azure.yml"
CHECK_JUNIT = ROOT / "tools" / "check_junit.py"
E2E_MODULES = {path.stem for path in (ROOT / "tests" / "e2e").glob("test_*.py")}

# What each job certifies: the 1.x job every e2e module, the 2.x job only the native routes (it
# deploys no second app and no streaming app, so any other module would skip there).
SELECTED = {
    "deploy_and_test": E2E_MODULES,
    "deploy_and_test_2x": {"test_langgraph_e2e"},
}


def _job(name: str) -> dict[str, Any]:
    workflow = cast(dict[str, Any], yaml.safe_load(WORKFLOW.read_text()))
    return cast(dict[str, Any], workflow["jobs"][name])


def _lines(step: dict[str, Any]) -> list[str]:
    """The commands of a step's ``run`` script: comments dropped, continuation lines joined."""
    script = step.get("run", "").replace("\\\n", " ")
    lines = (line.strip() for line in script.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _e2e_step(job: str) -> tuple[int, dict[str, Any]]:
    steps = [
        (index, step)
        for index, step in enumerate(_job(job)["steps"])
        if any(line.startswith("pytest ") for line in _lines(step))
    ]
    assert len(steps) == 1
    return steps[0]


def _pytest_and_validator(job: str) -> tuple[list[str], list[str]]:
    """The words of the step's pytest command and of its tools/check_junit.py command."""
    lines = _lines(_e2e_step(job)[1])
    [pytest_line] = [line for line in lines if line.startswith("pytest ")]
    [validator_line] = [line for line in lines if line.startswith("python tools/check_junit.py ")]
    assert lines.index(pytest_line) < lines.index(validator_line)
    return shlex.split(pytest_line), shlex.split(validator_line)


@pytest.mark.parametrize("job", SELECTED)
def test_validator_runs_after_pytest_on_the_report_pytest_wrote(job: str) -> None:
    pytest_run, validator = _pytest_and_validator(job)

    assert [word for word in pytest_run if word.startswith("--junitxml=")] == [
        f"--junitxml={validator[2]}"
    ]


@pytest.mark.parametrize(("job", "modules"), SELECTED.items())
def test_validator_expects_exactly_the_modules_the_job_selects(job: str, modules: set[str]) -> None:
    pytest_run, validator = _pytest_and_validator(job)

    paths = [ROOT / word for word in pytest_run if word.startswith("tests/")]
    selected = {m.stem for p in paths for m in (p.glob("test_*.py") if p.is_dir() else [p])}
    areas = validator[3:]
    assert areas[::2] == ["--area"] * (len(areas) // 2)
    assert {area.partition("=")[2] for area in areas[1::2]} == selected == modules
    # Nothing may narrow the selection after the validator's areas were chosen.
    assert not {"-k", "--deselect", "--ignore"} & set(pytest_run)


@pytest.mark.parametrize("job", SELECTED)
def test_nothing_masks_the_validator_or_records_certification_without_it(job: str) -> None:
    steps = _job(job)["steps"]
    index, step = _e2e_step(job)
    record = next(
        i for i, other in enumerate(steps) if "certification.json" in other.get("run", "")
    )

    assert index < record
    assert "continue-on-error" not in _job(job)
    assert not {"if", "continue-on-error", "shell"} & set(step)
    assert not {"if", "continue-on-error"} & set(steps[record])
    assert "||" not in step["run"]
    assert "set +e" not in step["run"]


def _report(modules: set[str], outcome: str = "", **declared: int) -> str:
    """A pytest-shaped JUnit report with two testcases per module; counters default to honest."""
    cases = [
        f'<testcase classname="tests.e2e.{module}" name="test_{number}">{outcome}</testcase>'
        for module in sorted(modules)
        for number in range(2)
    ]
    counters = {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0, **declared}
    attributes = " ".join(f'{name}="{value}"' for name, value in counters.items())
    return f"<testsuites><testsuite {attributes}>{''.join(cases)}</testsuite></testsuites>"


def _validate(job: str, report: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the validator on ``report`` with the areas the job passes to it."""
    (tmp_path / "results.xml").write_text(report)
    _, validator = _pytest_and_validator(job)
    return subprocess.run(
        [sys.executable, str(CHECK_JUNIT), str(tmp_path / "results.xml"), *validator[3:]],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(("job", "modules"), SELECTED.items())
def test_validator_accepts_a_report_where_every_selected_test_passed(
    tmp_path: Path, job: str, modules: set[str]
) -> None:
    result = _validate(job, _report(modules), tmp_path)

    assert result.returncode == 0, result.stderr
    assert f"Validated {2 * len(modules)} no-skip" in result.stdout


@pytest.mark.parametrize(("job", "modules"), SELECTED.items())
@pytest.mark.parametrize(
    ("make_report", "expected_error"),
    [
        pytest.param(
            lambda modules: _report(modules, "<skipped/>", skipped=2 * len(modules)),
            "tests failed, errored or were skipped",
            id="every-test-skipped-as-with-no-input",
        ),
        pytest.param(
            lambda modules: _report(modules, "<skipped/>"),
            "counter=skipped, reported=0",
            id="skips-hidden-by-forged-counter",
        ),
        pytest.param(
            lambda modules: _report({"test_other_module"}),
            "missing expected integration areas",
            id="selected-module-never-ran",
        ),
    ],
)
def test_validator_rejects_reports_that_do_not_prove_the_tests_ran(
    tmp_path: Path,
    job: str,
    modules: set[str],
    make_report: Callable[[set[str]], str],
    expected_error: str,
) -> None:
    result = _validate(job, make_report(modules), tmp_path)

    assert result.returncode == 1
    assert expected_error in result.stderr
