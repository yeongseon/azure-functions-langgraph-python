from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci-test.yml"


def _gate_program() -> str:
    workflow = CI_WORKFLOW.read_text()
    match = re.search(r"python3 - <<'EVAL'\n(?P<body>.*?)\n\s+EVAL", workflow, re.DOTALL)
    assert match is not None
    lines = match.group("body").splitlines()
    indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indent:] for line in lines)


def _run_gate(
    *,
    full_required: str,
    docs_changed: str,
    results: dict[str, dict[str, str]],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-c", _gate_program()],
        env={
            "FULL_REQUIRED": full_required,
            "DOCS_CHANGED": docs_changed,
            "RESULTS": json.dumps(results),
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("value", ["", "yes", "TRUE"])
def test_gate_rejects_invalid_classifier_outputs(value: str) -> None:
    result = _run_gate(
        full_required=value,
        docs_changed="true",
        results={"changes": {"result": "success"}, "docs-check": {"result": "success"}},
    )

    assert result.returncode != 0


def test_gate_rejects_impossible_classifier_outputs() -> None:
    result = _run_gate(
        full_required="false",
        docs_changed="false",
        results={"changes": {"result": "success"}, "docs-check": {"result": "skipped"}},
    )

    assert result.returncode != 0


@pytest.mark.parametrize("unexpected_result", ["failure", "skipped"])
def test_gate_rejects_unknown_unsuccessful_jobs(unexpected_result: str) -> None:
    result = _run_gate(
        full_required="true",
        docs_changed="false",
        results={
            "changes": {"result": "success"},
            "docs-check": {"result": "skipped"},
            "future-job": {"result": unexpected_result},
        },
    )

    assert result.returncode != 0


def test_gate_requires_unknown_jobs_to_succeed_on_docs_only_changes() -> None:
    result = _run_gate(
        full_required="false",
        docs_changed="true",
        results={
            "changes": {"result": "success"},
            "docs-check": {"result": "success"},
            "future-job": {"result": "skipped"},
        },
    )

    assert result.returncode != 0


def test_compatibility_lanes_do_not_deselect_mocked_integration_tests() -> None:
    workflow = CI_WORKFLOW.read_text()

    assert '-m "not e2e and not integration"' not in workflow


def test_setup_python_cache_inputs_are_checked_out_first() -> None:
    workflow = CI_WORKFLOW.read_text()
    jobs = re.split(r"^  [a-z][a-z0-9-]+:\n", workflow, flags=re.MULTILINE)[1:]

    for job in jobs:
        if "cache: pip" not in job:
            continue
        assert job.index("actions/checkout@") < job.index("cache: pip")
        assert "cache-dependency-path:" in job
