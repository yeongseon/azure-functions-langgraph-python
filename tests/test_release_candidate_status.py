"""Offline publication reporting, not event delivery or live certification."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import TypedDict

import pytest
import yaml

from tests.test_release_audit import NOW, ROOT, facts, snapshot
from tools.audit_releases import evaluate
from tools.release_audit_model import Job
from tools.release_audit_registry import load_registry


class PublishTriggers(TypedDict):
    push: dict[str, list[str]]
    workflow_dispatch: str


class PublishWorkflow(TypedDict):
    on: PublishTriggers


def test_candidate_keeps_tag_push_and_input_free_recovery_triggers() -> None:
    # Given: the actual certification/publishing workflow, not a simulated dispatcher.
    path = ROOT / ".github/workflows/publish-pypi.yml"
    # When: reading the trigger configuration without YAML 1.1 boolean coercion.
    workflow: PublishWorkflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    # Then: certification starts on tag pushes, not Release publication/promotion.
    assert workflow["on"] == {"push": {"tags": ["v*"]}, "workflow_dispatch": ""}


def test_release_please_has_no_draft_configuration() -> None:
    # Given: the existing release authority configuration.
    path = ROOT / "release-please-config.json"
    # When: inspecting all serialized configuration keys.
    keys: list[str] = []
    json.loads(path.read_text(), object_pairs_hook=lambda pairs: keys.extend(dict(pairs)))
    # Then: this status-only change does not enable draft Release creation.
    assert "draft" not in keys
    assert "draft-pull-request" not in keys


@pytest.mark.parametrize(
    ("gate", "upload", "file_count", "verdict", "finding"),
    [
        ("success", "success", 2, "PUBLISHED", None),
        ("failure", "skipped", 0, "DEFECT", "gate-not-successful"),
        ("success", "failure", 0, "DEFECT", "publish-run-failed"),
        ("success", "success", 0, "DEFECT", "no-pypi-files"),
        ("success", "failure", 1, "DEFECT", "pypi-incomplete"),
    ],
    ids=["success", "gate-failure", "upload-failure", "files-not-visible", "partial-upload"],
)
def test_candidate_publication_requires_served_files_and_successful_evidence(
    gate: str, upload: str, file_count: int, verdict: str, finding: str | None
) -> None:
    # Given: synthetic tag/file/provenance facts and the complete current audit matrix.
    candidate = facts("0.14.2")
    assert candidate.run is not None
    registry = replace(load_registry(ROOT), exceptions=())
    jobs = (
        tuple(
            replace(job, conclusion=gate)
            for job in candidate.run.jobs
            if not job.name.startswith("Candidate runtime dependency audit")
            and job.name != "Publish to PyPI"
        )
        + tuple(
            Job(f"Candidate runtime dependency audit ({profile})", "success")
            for profile in (
                "base",
                "azure-blob",
                "azure-table",
                "azure-identity",
                "postgres",
                "sqlite",
                "cosmos",
                "otel",
                "streaming",
                "durable",
                "openapi",
            )
        )
        + (Job("Publish to PyPI", upload),)
    )
    recorded = replace(
        candidate,
        files=candidate.files[:file_count],
        run=replace(
            candidate.run,
            jobs=jobs,
            conclusion="success" if gate == upload == "success" else "failure",
        ),
    )
    # When: the real read-only evaluator receives the recorded outcome.
    [result] = evaluate(snapshot(recorded), registry, NOW)
    # Then: a tag or successful upload alone cannot establish publication.
    assert result.verdict == verdict
    assert {item.code for item in result.findings} >= ({finding} if finding else set())
    if finding is None:
        assert result.findings == ()


def test_same_tag_recovery_uses_successful_attempt_without_erasing_failed_attempt() -> None:
    # Given: an unchanged tag with a failed first upload and successful recovery.
    candidate = facts()
    assert candidate.run is not None and candidate.tag is not None
    first = replace(candidate.run, conclusion="failure", jobs=(Job("Publish to PyPI", "failure"),))
    recovered_id = first.id + 1
    recovered = replace(
        candidate.run,
        id=recovered_id,
        event="workflow_dispatch",
        html_url=f"https://github.com/run/{recovered_id}",
    )
    recorded = replace(
        candidate,
        run=first,
        runs=(first, recovered),
        files=tuple(
            replace(
                file,
                attestations=tuple(replace(a, run_id=recovered_id) for a in file.attestations),
            )
            for file in candidate.files
        ),
    )
    # When: selecting publication evidence from both immutable attempts.
    [result] = evaluate(snapshot(recorded), replace(load_registry(ROOT), exceptions=()), NOW)
    # Then: the successful run binds the same tag and served files; failure remains recorded.
    assert result.verdict == "PUBLISHED" and result.findings == ()
    assert result.run_url == recovered.html_url
    assert recorded.runs == (first, recovered)
    assert all(run.head_sha == candidate.tag.commit for run in recorded.runs)
