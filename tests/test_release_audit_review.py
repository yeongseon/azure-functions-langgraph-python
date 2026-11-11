from __future__ import annotations

from dataclasses import replace

import pytest

from tests.test_release_audit import NOW, ROOT, facts, failed, snapshot
from tests.test_release_audit_collect import recorded_fetcher, response
from tools.release_audit_collect import collect
from tools.release_audit_evaluate import evaluate
from tools.release_audit_gates import select_run
from tools.release_audit_model import Job
from tools.release_audit_registry import load_registry


def test_numbering_gaps_do_not_invent_versions() -> None:
    # Given: failed patches, their published successor and a later minor without 0.6.x.
    registry = load_registry(ROOT)
    registry = replace(
        registry, exceptions=tuple(e for e in registry.exceptions if e.tag in {"v0.5.2", "v0.5.3"})
    )
    # When: evaluating only versions actually recorded.
    result = evaluate(
        snapshot(
            failed("0.5.2", 24605716212),
            failed("0.5.3", 24958757167),
            facts("0.5.4"),
            facts("0.7.0"),
        ),
        registry,
        NOW,
    )
    # Then: both failures are excepted and no synthetic numbering defect appears.
    assert [r.verdict for r in result] == ["EXCEPTED", "EXCEPTED", "PUBLISHED", "PUBLISHED"]


def test_latest_run_is_used_when_no_publish_job_succeeded() -> None:
    # Given: two failed candidates with no successful publish job.
    original = failed()
    assert original.run
    later = replace(original.run, id=original.run.id + 1)
    candidate = replace(original, runs=(original.run, later))
    # When: selecting fallback evidence.
    selected = select_run(candidate, load_registry(ROOT).gate_policy)
    # Then: the highest id is evaluated rather than hiding a later failure.
    assert selected == later


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "failure")])
def test_later_unsuccessful_run_does_not_replace_publish_evidence(
    status: str, conclusion: str | None
) -> None:
    # Given: a successful publication followed by an unsuccessful later run.
    original = facts()
    assert original.run
    later = replace(
        original.run,
        id=124,
        status=status,
        conclusion=conclusion,
        jobs=(Job("Publish to PyPI", conclusion),),
    )
    candidate = replace(original, run=later, runs=(original.run, later))
    # When: selecting the run that actually published.
    result = evaluate(snapshot(candidate), replace(load_registry(ROOT), exceptions=()), NOW)[0]
    # Then: later failures are informational, not replacement publication proof.
    assert result.verdict == "PUBLISHED"
    assert result.run_url == original.run.html_url
    assert any("124" in note for note in result.notes)


@pytest.mark.parametrize(
    "damage", ["failed-job", "cancelled-optional", "head", "tag", "prerelease"]
)
def test_inflight_does_not_hide_observed_defect(damage: str) -> None:
    # Given: a candidate in flight with an already-observed defect.
    original = facts()
    assert original.run and original.release
    run = replace(original.run, status="in_progress", conclusion=None)
    candidate = replace(original, files=(), run=run)
    if damage == "failed-job":
        candidate = replace(
            candidate, run=replace(run, jobs=(Job("Wheel packaging tests", "failure"),))
        )
    if damage == "cancelled-optional":
        candidate = replace(
            candidate, run=replace(run, jobs=(*run.jobs, Job("optional", "cancelled")))
        )
    if damage == "head":
        candidate = replace(candidate, run=replace(run, head_sha="other"))
    if damage == "tag":
        candidate = replace(candidate, tag=None)
    if damage == "prerelease":
        candidate = replace(candidate, release=replace(original.release, prerelease=True))
    # When: evaluating without historical exceptions.
    result = evaluate(snapshot(candidate), replace(load_registry(ROOT), exceptions=()), NOW)[0]
    # Then: pending grace does not cover real failures.
    assert result.verdict == "DEFECT"


@pytest.mark.parametrize("damage", ["missing", "extra", "skip-succeeded"])
def test_exception_requires_exact_structured_jobs(damage: str) -> None:
    # Given: a registered failed run with altered job evidence.
    original = failed()
    assert original.run
    jobs = original.run.jobs
    if damage == "missing":
        jobs = tuple(j for j in jobs if j.name != "Wheel packaging tests")
    if damage == "extra":
        jobs = (*jobs, Job("new optional job", "failure"))
    if damage == "skip-succeeded":
        jobs = tuple(
            replace(j, conclusion="success") if j.name == "Publish to PyPI" else j for j in jobs
        )
    registry = load_registry(ROOT)
    registry = replace(
        registry, exceptions=tuple(e for e in registry.exceptions if e.tag == "v0.12.1")
    )
    # When: evaluating the same registered run id.
    result = evaluate(
        snapshot(replace(original, run=replace(original.run, jobs=jobs)), facts()), registry, NOW
    )[0]
    # Then: missing or changed exception evidence is not waived.
    assert result.verdict == "DEFECT"
    assert any(not f.covered for f in result.findings)


@pytest.mark.parametrize(
    "damage", ["absent", "failed", "optional-failed", "optional-cancelled", "matrix"]
)
def test_runtime_matrix_and_optional_failures(damage: str) -> None:
    # Given: all eleven v0.13.0 runtime audit members and an optional job.
    original = facts("0.13.0")
    assert original.run
    jobs = tuple(
        j for j in original.run.jobs if not j.name.startswith("Candidate runtime dependency audit")
    )
    matrix = tuple(Job(f"Candidate runtime dependency audit ({i})", "success") for i in range(11))
    if damage == "absent":
        matrix = ()
    elif damage == "failed":
        matrix = (replace(matrix[0], conclusion="failure"), *matrix[1:])
    elif damage == "matrix":
        matrix = matrix[:1]
    optional = Job(
        "optional",
        "failure"
        if damage == "optional-failed"
        else "cancelled"
        if damage == "optional-cancelled"
        else "success",
    )
    candidate = replace(original, run=replace(original.run, jobs=(*jobs, *matrix, optional)))
    # When: evaluating required prefixes and every observed failure.
    result = evaluate(snapshot(candidate), replace(load_registry(ROOT), exceptions=()), NOW)[0]
    # Then: incomplete runtime proof or a failed optional job blocks publication.
    assert result.verdict == "DEFECT"


def test_draft_release_is_ignored_with_note() -> None:
    # Given: a numeric draft returned by the authenticated Releases API.
    fetcher = recorded_fetcher()
    path = "/repos/yeongseon/azure-functions-langgraph-python/releases?per_page=100&page=1"
    fetcher.replies[path] = response([{"tag_name": "v0.5.0", "draft": True}])
    # When: collecting publication evidence.
    result = collect(load_registry(ROOT), fetcher, NOW)
    # Then: a draft cannot act as a published Release.
    assert result.versions[0].release is None
    assert "Draft release v0.5.0 not audited" in result.notes


def test_tag_listing_already_contains_peeled_commit() -> None:
    # Given: the tags API commit.sha is the peeled target even for annotated tags.
    fetcher = recorded_fetcher()
    # When: collecting the supplied commit without dereferencing a git tag object.
    result = collect(load_registry(ROOT), fetcher, NOW)
    # Then: source and ancestry reads use that exact commit.
    assert result.versions[0].tag and result.versions[0].tag.commit == "a" * 40
    assert not any("/git/tags/" in path for _, path, _ in fetcher.calls)
