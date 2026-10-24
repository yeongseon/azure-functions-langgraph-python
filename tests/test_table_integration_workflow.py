"""Regression tests for the required Azurite and Cosmos integration lanes.

``tools/check_junit.py`` is the single JUnit validator behind both workflows. These tests run it
with the ``--area`` arguments exactly as each workflow passes them, and check that each workflow
is wired to validate the report its own pytest run wrote.
"""

from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TABLE_WORKFLOW = ROOT / ".github" / "workflows" / "ci-table-integration.yml"
COSMOS_WORKFLOW = ROOT / ".github" / "workflows" / "ci-cosmos-integration.yml"
CHECK_JUNIT = ROOT / "tools" / "check_junit.py"


def _workflow(path: Path) -> dict[Any, Any]:
    # PyYAML follows YAML 1.1, so the workflow's `on:` key loads as the boolean True.
    return cast(dict[Any, Any], yaml.safe_load(path.read_text()))


def _validation_step(workflow: Path) -> dict[str, Any]:
    steps = [
        step
        for job in _workflow(workflow)["jobs"].values()
        for step in job["steps"]
        if "tools/check_junit.py" in step.get("run", "")
    ]
    assert len(steps) == 1
    return cast(dict[str, Any], steps[0])


def _validator_arguments(workflow: Path) -> list[str]:
    """The arguments the workflow passes to tools/check_junit.py: the report, then the areas."""
    script = _validation_step(workflow)["run"].replace("\\\n", " ")
    command = next(line for line in script.splitlines() if "tools/check_junit.py" in line)
    words = shlex.split(command)
    return words[words.index("tools/check_junit.py") + 1 :]


def _check_junit(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK_JUNIT), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _validate_junit(
    report: Path, workflow: Path = TABLE_WORKFLOW
) -> subprocess.CompletedProcess[str]:
    _, *areas = _validator_arguments(workflow)
    return _check_junit(str(report), *areas)


def test_table_workflow_covers_all_service_integration_inputs() -> None:
    workflow = TABLE_WORKFLOW.read_text()

    for required_path in (
        '"src/azure_functions_langgraph/locks/**"',
        '"src/azure_functions_langgraph/app.py"',
        '"tests/integration/**"',
        '"tests/storage_service.py"',
        '"tests/test_production_persistent_agent_example.py"',
    ):
        assert required_path in workflow

    assert workflow.count('"tests/storage_service.py"') == 2


def test_table_workflow_requires_azurite_tests_and_accepts_new_sdk_versions() -> None:
    workflow = TABLE_WORKFLOW.read_text()

    assert "--skipApiVersionCheck" in workflow
    assert "AZURITE_REQUIRED: true" in workflow
    assert "--strict-markers" in workflow


def test_junit_validator_accepts_complete_no_skip_report(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        """<testsuites tests="3" failures="0" errors="0" skipped="0">
        <testsuite tests="3" failures="0" errors="0" skipped="0">
          <testcase classname="tests.integration.test_table_store_integration" />
          <testcase classname="tests.integration.test_checkpoint_conformance" />
          <testcase classname="tests.test_production_persistent_agent_example" />
        </testsuite>
        </testsuites>"""
    )

    result = _validate_junit(report)

    assert result.returncode == 0, result.stderr


def test_junit_validator_accepts_pytest_child_suite_totals(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        """<testsuites>
        <testsuite tests="3" failures="0" errors="0" skipped="0">
          <testcase classname="tests.integration.test_table_store_integration" />
          <testcase classname="tests.integration.test_checkpoint_conformance" />
          <testcase classname="tests.test_production_persistent_agent_example" />
        </testsuite>
        </testsuites>"""
    )

    result = _validate_junit(report)

    assert result.returncode == 0, result.stderr


def test_junit_validator_rejects_declared_test_count_mismatch(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        """<testsuite tests="99" failures="0" errors="0" skipped="0">
        <testcase classname="tests.integration.test_table_store_integration" />
        <testcase classname="tests.integration.test_checkpoint_conformance" />
        <testcase classname="tests.test_production_persistent_agent_example" />
        </testsuite>"""
    )

    result = _validate_junit(report)

    assert result.returncode != 0
    assert "JUnit testsuite test count does not match testcase descendants" in result.stderr


def test_junit_validator_counts_nested_suite_cases_once(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        """<testsuites>
        <testsuite tests="3" failures="0" errors="0" skipped="0">
          <testsuite tests="3" failures="0" errors="0" skipped="0">
            <testcase classname="tests.integration.test_table_store_integration" />
            <testcase classname="tests.integration.test_checkpoint_conformance" />
            <testcase classname="tests.test_production_persistent_agent_example" />
          </testsuite>
        </testsuite>
        </testsuites>"""
    )

    result = _validate_junit(report)

    assert result.returncode == 0, result.stderr
    assert "Validated 3 no-skip integration tests" in result.stdout


@pytest.mark.parametrize(
    ("xml", "expected_error"),
    [
        ("<not-closed", "ParseError"),
        (
            """<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0" />
            </testsuites>""",
            "{'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}",
        ),
        (
            """<testsuites><testsuite tests="1" failures="1" errors="0" skipped="0">
            <testcase classname="tests.integration.test_table_store_integration">
              <failure />
            </testcase>
            </testsuite></testsuites>""",
            "{'tests': 1, 'failures': 1, 'errors': 0, 'skipped': 0}",
        ),
        (
            """<testsuites><testsuite tests="1" failures="0" errors="1" skipped="0">
            <testcase classname="tests.integration.test_table_store_integration">
              <error />
            </testcase>
            </testsuite></testsuites>""",
            "{'tests': 1, 'failures': 0, 'errors': 1, 'skipped': 0}",
        ),
        (
            """<testsuites><testsuite tests="1" failures="0" errors="0" skipped="1">
            <testcase classname="tests.integration.test_table_store_integration">
              <skipped />
            </testcase>
            </testsuite></testsuites>""",
            "{'tests': 1, 'failures': 0, 'errors': 0, 'skipped': 1}",
        ),
        (
            """<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">
            <testcase classname="tests.integration.test_table_store_integration" />
            <testcase classname="tests.integration.test_checkpoint_conformance" />
            </testsuite></testsuites>""",
            "missing expected integration areas: ['production persistent agent']",
        ),
        (
            """<testsuites><testsuite tests="3" failures="0" errors="0" skipped="0">
            <testcase classname="tests.integration.test_table_store_integration">
              <failure>boom</failure>
            </testcase>
            <testcase classname="tests.integration.test_checkpoint_conformance" />
            <testcase classname="tests.test_production_persistent_agent_example" />
            </testsuite></testsuites>""",
            "JUnit summary does not match testcase outcomes",
        ),
        (
            """<testsuites><testsuite tests="3" failures="0" errors="0" skipped="0">
            <testcase classname="tests.integration.test_table_store_integration">
              <skipped />
            </testcase>
            <testcase classname="tests.integration.test_checkpoint_conformance" />
            <testcase classname="tests.test_production_persistent_agent_example" />
            </testsuite></testsuites>""",
            "JUnit summary does not match testcase outcomes",
        ),
    ],
)
def test_junit_validator_rejects_invalid_or_incomplete_reports(
    tmp_path: Path,
    xml: str,
    expected_error: str,
) -> None:
    report = tmp_path / "results.xml"
    report.write_text(xml)

    result = _validate_junit(report)

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_junit_validator_rejects_missing_report(tmp_path: Path) -> None:
    result = _validate_junit(tmp_path / "missing.xml")

    assert result.returncode != 0


COSMOS_CLASS = "tests.integration.test_cosmos_integration"


def _suite(classname: str, outcome: str = "", *, count: int = 6, **declared: int) -> str:
    """A pytest-shaped JUnit report of ``count`` identical testcases.

    The suite counters default to "everything passed", so a test states only what it forges.
    """
    counters = {"tests": count, "failures": 0, "errors": 0, "skipped": 0, **declared}
    attributes = " ".join(f'{name}="{value}"' for name, value in counters.items())
    cases = "".join(
        f'<testcase classname="{classname}" name="test_{number}">{outcome}</testcase>'
        for number in range(count)
    )
    return f"<testsuites><testsuite {attributes}>{cases}</testsuite></testsuites>"


def test_cosmos_lane_accepts_a_report_where_every_cosmos_test_passed(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(_suite(COSMOS_CLASS))

    result = _validate_junit(report, COSMOS_WORKFLOW)

    assert result.returncode == 0, result.stderr
    assert "Validated 6 no-skip integration tests" in result.stdout


@pytest.mark.parametrize(
    ("xml", "expected_error"),
    [
        pytest.param(
            _suite(COSMOS_CLASS, '<skipped message="emulator unavailable"/>', skipped=6),
            "{'tests': 6, 'failures': 0, 'errors': 0, 'skipped': 6}",
            id="every-test-skipped",
        ),
        pytest.param(
            _suite(COSMOS_CLASS, '<skipped message="emulator unavailable"/>'),
            "JUnit summary does not match testcase outcomes: counter=skipped, reported=0, actual=6",
            id="skips-hidden-by-forged-counter",
        ),
        pytest.param(
            _suite(COSMOS_CLASS, '<error message="emulator unavailable"/>', errors=6),
            "{'tests': 6, 'failures': 0, 'errors': 6, 'skipped': 0}",
            id="fixture-errors",
        ),
        pytest.param(
            _suite(COSMOS_CLASS, count=0),
            "no tests ran: {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}",
            id="zero-tests",
        ),
        pytest.param(
            _suite("tests.integration.test_table_store_integration"),
            "missing expected integration areas: ['Cosmos integration']",
            id="cosmos-tests-not-collected",
        ),
    ],
)
def test_cosmos_lane_rejects_reports_that_do_not_prove_the_tests_ran(
    tmp_path: Path, xml: str, expected_error: str
) -> None:
    report = tmp_path / "results.xml"
    report.write_text(xml)

    result = _validate_junit(report, COSMOS_WORKFLOW)

    assert result.returncode == 1
    assert expected_error in result.stderr


@pytest.mark.parametrize("area_arguments", [[], ["--area", ""], ["--area", "Cosmos="]])
def test_check_junit_refuses_to_run_without_a_usable_area(
    tmp_path: Path, area_arguments: list[str]
) -> None:
    report = tmp_path / "results.xml"
    report.write_text(_suite(COSMOS_CLASS))

    result = _check_junit(str(report), *area_arguments)

    assert result.returncode == 2
    assert "--area" in result.stderr


def test_check_junit_matches_a_bare_area_marker_case_insensitively(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(_suite(COSMOS_CLASS))

    result = _check_junit(str(report), "--area", "TEST_Cosmos_Integration")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("workflow", "required_env", "markers"),
    [
        pytest.param(
            TABLE_WORKFLOW,
            "AZURITE_REQUIRED",
            {"table_store_integration", "checkpoint_conformance", "production_persistent_agent"},
            id="table",
        ),
        pytest.param(COSMOS_WORKFLOW, "COSMOS_REQUIRED", {"test_cosmos_integration"}, id="cosmos"),
    ],
)
def test_required_lane_validates_the_report_its_pytest_run_wrote(
    workflow: Path, required_env: str, markers: set[str]
) -> None:
    step = _validation_step(workflow)
    report, *areas = _validator_arguments(workflow)

    assert f"--junitxml={report}" in step["run"]
    assert step["run"].index("pytest") < step["run"].index("tools/check_junit.py")
    assert step["env"][required_env] is True
    assert set(areas[::2]) == {"--area"}
    assert {area.partition("=")[2] for area in areas[1::2]} == markers
    assert "<<" not in step["run"]
    jobs = _workflow(workflow)["jobs"].values()
    assert not [
        item for job in jobs for item in (job, *job["steps"]) if "continue-on-error" in item
    ]


def test_cosmos_lane_stays_nightly_and_non_gating() -> None:
    workflow = _workflow(COSMOS_WORKFLOW)
    triggers = workflow[True] if True in workflow else workflow["on"]

    assert set(triggers) == {"schedule", "workflow_dispatch"}


def test_table_workflow_reruns_when_the_validator_changes() -> None:
    assert TABLE_WORKFLOW.read_text().count('"tools/check_junit.py"') == 2
