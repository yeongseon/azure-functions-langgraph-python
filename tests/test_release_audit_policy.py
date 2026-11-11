from __future__ import annotations

from dataclasses import asdict, replace
import json

import pytest

from tests.test_release_audit import NOW, ROOT, facts, failed, snapshot
from tools.audit_releases import evaluate
from tools.release_audit_model import AuditError
from tools.release_audit_parse import load
from tools.release_audit_registry import load_registry
from tools.release_audit_snapshot import parse_snapshot


@pytest.mark.parametrize("status", ["unknown", "complete", ""])
def test_unknown_run_status_is_collection_error(status: str) -> None:
    # Given: a malformed run state that must not receive unlimited pending grace.
    candidate = facts()
    assert candidate.run
    raw = json.dumps(
        asdict(snapshot(replace(candidate, run=replace(candidate.run, status=status))))
    )
    # When / Then: replay rejects unknown workflow states at the boundary.
    with pytest.raises(AuditError):
        parse_snapshot(load(raw))


def test_exception_does_not_cover_new_gate_failure() -> None:
    # Given: a historical wheel failure now also has a failed library gate.
    registry = load_registry(ROOT)
    registry = replace(
        registry, exceptions=tuple(e for e in registry.exceptions if e.tag == "v0.12.1")
    )
    candidate = failed()
    assert candidate.run
    jobs = tuple(
        replace(j, conclusion="failure") if j.name == "Library tests" else j
        for j in candidate.run.jobs
    )
    # When: evaluating the changed gate evidence.
    result = evaluate(
        snapshot(replace(candidate, run=replace(candidate.run, jobs=jobs)), facts()), registry, NOW
    )[0]
    # Then: a historical exception cannot suppress the new failed gate.
    assert result.verdict == "DEFECT"
    assert any(f.code == "gate-not-successful" and not f.covered for f in result.findings)


@pytest.mark.parametrize(
    "state", ["failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale"]
)
def test_exception_does_not_hide_new_optional_job_failure(state: str) -> None:
    # Given: the registered v0.12.1 run gains an optional job that did not complete cleanly.
    registry = load_registry(ROOT)
    registry = replace(
        registry, exceptions=tuple(e for e in registry.exceptions if e.tag == "v0.12.1")
    )
    candidate = failed()
    assert candidate.run
    extra = replace(candidate.run.jobs[0], name="Optional extra", conclusion=state)
    run = replace(candidate.run, jobs=(*candidate.run.jobs, extra))
    # When: evaluating the changed evidence.
    result = evaluate(
        snapshot(replace(candidate, run=replace(candidate.run, jobs=run.jobs)), facts()),
        registry,
        NOW,
    )[0]
    # Then: the historical exception must not hide it.
    assert result.verdict == "DEFECT"
    assert any(f.job_name == "Optional extra" and not f.covered for f in result.findings)


def test_exception_requires_published_successor() -> None:
    # Given: the documented successor no longer has files in the evidence.
    registry = load_registry(ROOT)
    registry = replace(
        registry, exceptions=tuple(e for e in registry.exceptions if e.tag == "v0.12.1")
    )
    # When: evaluating the historical candidate without its published successor.
    result = evaluate(snapshot(failed(), replace(facts(), files=())), registry, NOW)[0]
    # Then: the recorded disposition is not silently trusted.
    assert result.verdict == "DEFECT"
    assert "exception-evidence-mismatch" in {f.code for f in result.findings}


@pytest.mark.parametrize(
    "field,value",
    [
        ("workflow", "wrong.yml"),
        ("repository", "other/repo"),
        ("environment", "other"),
        ("kind", "Other"),
    ],
)
def test_each_publisher_identity_field_is_checked(field: str, value: str) -> None:
    # Given: one mismatched publisher identity field.
    candidate = facts()
    publisher = replace(candidate.files[0].publishers[0], **{field: value})
    candidate = replace(
        candidate, files=(replace(candidate.files[0], publishers=(publisher,)), candidate.files[1])
    )
    # When: checking provenance independently of run success.
    result = evaluate(snapshot(candidate), replace(load_registry(ROOT), exceptions=()), NOW)[0]
    # Then: the unexpected publisher is a defect.
    assert "provenance-publisher-mismatch" in {f.code for f in result.findings}
