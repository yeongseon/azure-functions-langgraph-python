from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

TABLE_WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-table-integration.yml"
)


def _junit_validator() -> str:
    workflow = TABLE_WORKFLOW.read_text()
    match = re.search(r"python - <<'VALIDATE'\n(?P<body>.*?)\n\s+VALIDATE", workflow, re.DOTALL)
    assert match is not None
    lines = match.group("body").splitlines()
    indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indent:] for line in lines)


def _validate_junit(report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _junit_validator()],
        env={**os.environ, "JUNIT_XML": str(report)},
        capture_output=True,
        text=True,
        check=False,
    )


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
