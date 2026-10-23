from datetime import date
import json
from pathlib import Path

import pytest

from tools.runtime_audit import classify_audit
from tools.security_evidence import classify_bandit, semgrep_evidence
from tools.security_report import Status, write_report


@pytest.mark.parametrize("raw", ["", "{}", "{", '{"dependencies": []}'])
def test_unusable_audit_is_not_clean_or_a_vulnerability(raw: str) -> None:
    # Given: absent, malformed or empty scanner evidence.
    # When: a nominally successful tool result is classified.
    result = classify_audit(raw, 0)
    # Then: the release fails distinctly from an advisory finding.
    assert result.status in {Status.TOOL_ERROR, Status.INCOMPLETE}
    assert result.returncode in {2, 3}


@pytest.mark.parametrize("code", [2, -9, 127])
def test_scanner_crash_is_tool_error(code: int) -> None:
    # Given: even valid-looking output cannot override a crashed tool.
    raw = json.dumps({"dependencies": [{"name": "pydantic", "version": "2", "vulns": []}]})
    # When: the scanner failed operationally.
    result = classify_audit(raw, code)
    # Then: it fails as tool error, not a vulnerability.
    assert result.status == Status.TOOL_ERROR


@pytest.mark.parametrize("severity", [None, "UNKNOWN", "LOW", "HIGH"])
def test_every_unexcepted_advisory_blocks_even_unknown_severity(severity: str | None) -> None:
    # Given: pip-audit does not reliably supply severity; unknown is never safe.
    raw = json.dumps(
        {
            "dependencies": [
                {
                    "name": "pkg",
                    "version": "1",
                    "vulns": [
                        {
                            "id": "GHSA-abcd-1234-efgh",
                            "severity": severity,
                            "description": "secret-canary",
                        }
                    ],
                }
            ]
        }
    )
    # When: an advisory is reported.
    result = classify_audit(raw, 1)
    # Then: all severities block, and raw diagnostic prose is discarded.
    assert result.status == Status.FAIL
    assert "secret-canary" not in repr(result)


def test_clean_nonempty_audit_passes() -> None:
    # Given: one actually audited package.
    raw = '{"dependencies":[{"name":"pkg","version":"1","vulns":[]}]}'
    # When: scanner output and exit agree.
    result = classify_audit(raw, 0)
    # Then: nonzero scope is recorded.
    assert result.status == Status.PASS
    assert result.counts["packages"] == 1


def test_semgrep_success_is_explicit_action_outcome_only() -> None:
    result = semgrep_evidence("success")
    assert result.status == Status.INCOMPLETE
    assert "action outcome only" in result.message


@pytest.mark.parametrize(
    "raw", ["", "{}", '{"metrics":{"_totals":{"loc":0}},"results":[],"errors":[]}']
)
def test_bandit_missing_or_zero_scope_blocks(raw: str) -> None:
    # Given: a scanner that analyzed no relevant product files.
    # When: its result is classified.
    result = classify_bandit(raw, 0)
    # Then: missing evidence is not a clean pass.
    assert result.status in {Status.TOOL_ERROR, Status.INCOMPLETE}


def test_bandit_evidence_drops_source_and_error_text(tmp_path: Path) -> None:
    # Given: raw source and diagnostics may carry credentials.
    raw = json.dumps(
        {
            "metrics": {"src/a.py": {"loc": 1}, "_totals": {"loc": 1}},
            "errors": [],
            "results": [
                {
                    "test_id": "B105",
                    "filename": "src/a.py",
                    "line_number": 1,
                    "issue_severity": "LOW",
                    "issue_confidence": "HIGH",
                    "code": "secret-canary",
                    "issue_text": "secret-canary",
                }
            ],
        }
    )
    result = classify_bandit(raw, 1, floor=1)
    # When: allow-listed evidence is persisted.
    code = write_report([result], tmp_path)
    # Then: only structural finding metadata survives.
    assert code == 1
    assert "secret-canary" not in (tmp_path / "summary.json").read_text()
    assert result.findings[0].id == "B105"


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", ""])
def test_semgrep_action_failure_or_absence_never_passes(outcome: str) -> None:
    # Given: action-only evidence has no reliable file/finding counts.
    # When: the action does not succeed.
    result = semgrep_evidence(outcome)
    # Then: it remains a distinct non-pass, not an invented clean scan.
    assert result.returncode != 0


def test_expired_static_exception_blocks() -> None:
    from tools.security_exceptions import parse_exceptions

    # Given: a reviewed exception whose deadline has elapsed.
    policy = [
        {
            "id": "B105",
            "file": "src/a.py",
            "line": 1,
            "kind": "reviewed-static",
            "reason": "non-secret",
            "owner": "maintainer",
            "added": "2026-10-01",
            "expires": "2026-10-20",
        }
    ]
    # When / Then: expired review cannot bypass a finding.
    with pytest.raises(ValueError):
        parse_exceptions(json.dumps(policy), date(2026, 10, 23))
