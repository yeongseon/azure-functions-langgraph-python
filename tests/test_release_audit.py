"""Recorded facts exercise the audit without contacting either service."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools.audit_releases import evaluate
from tools.release_audit_model import (
    Attestation,
    File,
    Job,
    Publisher,
    Release,
    Run,
    Snapshot,
    Tag,
    VersionFacts,
)
from tools.release_audit_registry import load_registry

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 10, tzinfo=UTC)
OLD = "2026-10-16T00:00:00Z"


def facts(version: str = "0.12.2") -> VersionFacts:
    publisher = Publisher(
        "GitHub", "yeongseon/azure-functions-langgraph", "publish-pypi.yml", "pypi"
    )
    jobs = tuple(
        Job(name, "success")
        for name in (
            "Build & verify version",
            "Library tests",
            "Wheel packaging tests",
            "Azure release certification / Deploy & Test",
            "Publish to PyPI",
            "Candidate runtime dependency audit (streaming)",
        )
    )
    if tuple(int(p) for p in version.split(".")) < (0, 8, 2):
        jobs = (Job("build-and-publish", "success"),)
    return VersionFacts(
        version,
        Tag("v" + version, "a" * 40, version, True, OLD),
        Release("https://github.com/release", OLD, False, False),
        Run(123, "https://github.com/run/123", "completed", "success", "a" * 40, OLD, jobs),
        tuple(
            File(
                name,
                kind,
                "a" * 64,
                OLD,
                False,
                (publisher,),
                (
                    Attestation(
                        name,
                        "a" * 64,
                        "https://github.com/yeongseon/azure-functions-langgraph",
                        "a" * 40,
                        "refs/tags/v" + version,
                        123,
                        publisher,
                    ),
                ),
            )
            for name, kind in (
                ("package.whl", "bdist_wheel"),
                ("package.tar.gz", "sdist"),
            )
        ),
    )


def snapshot(*versions: VersionFacts) -> Snapshot:
    return Snapshot(
        OLD,
        "yeongseon/azure-functions-langgraph-python",
        "azure-functions-langgraph",
        "main-sha",
        tuple(versions),
        (),
    )


def test_published_accepts_repository_alias() -> None:
    # Given: a complete release with a historical publisher repository name.
    recorded = snapshot(facts())
    # When: evaluating immutable facts.
    result = evaluate(recorded, replace(load_registry(ROOT), exceptions=()), NOW)
    # Then: publication is consistent.
    assert result[0].verdict == "PUBLISHED"
    assert result[0].findings == ()


@pytest.mark.parametrize(
    "code",
    [
        "tag-without-release",
        "tag-version-mismatch",
        "tag-not-on-main",
        "no-publish-run",
        "publish-run-failed",
        "gate-not-successful",
        "no-pypi-files",
        "pypi-without-release",
        "pypi-without-successful-run",
        "pypi-incomplete",
        "yanked",
        "provenance-missing",
        "provenance-publisher-mismatch",
    ],
)
def test_defects_are_actionable(code: str) -> None:
    # Given: one corrupted aspect of otherwise consistent published facts.
    original = facts()
    assert original.tag and original.run
    damaged = {
        "tag-without-release": replace(original, release=None),
        "tag-version-mismatch": replace(original, tag=replace(original.tag, source_version="0.12")),
        "tag-not-on-main": replace(original, tag=replace(original.tag, ancestor=False)),
        "no-publish-run": replace(original, run=None),
        "publish-run-failed": replace(original, run=replace(original.run, conclusion="failure")),
        "gate-not-successful": replace(
            original,
            run=replace(
                original.run,
                jobs=(
                    Job("Publish to PyPI", "success"),
                    Job("Wheel packaging tests", "failure"),
                ),
            ),
        ),
        "no-pypi-files": replace(original, files=()),
        "pypi-without-release": replace(original, release=None),
        "pypi-without-successful-run": replace(original, run=None),
        "pypi-incomplete": replace(original, files=original.files[:1]),
        "yanked": replace(original, files=(replace(original.files[0], yanked=True),)),
        "provenance-missing": replace(
            original, files=(replace(original.files[0], publishers=(), attestations=()),)
        ),
        "provenance-publisher-mismatch": replace(
            original,
            files=(
                replace(
                    original.files[0],
                    publishers=(Publisher("GitHub", "other/repo", "wrong.yml", "pypi"),),
                ),
            ),
        ),
    }[code]
    # When: evaluating without historical exceptions.
    result = evaluate(snapshot(damaged), replace(load_registry(ROOT), exceptions=()), NOW)[0]
    # Then: the defect retains machine-readable code and human-facing evidence links.
    assert result.verdict == "DEFECT"
    finding = next(item for item in result.findings if item.code == code)
    assert finding.message
    assert finding.links
    assert not finding.covered


def failed(version: str = "0.12.1", run_id: int = 38891888298) -> VersionFacts:
    original = facts(version)
    assert original.run
    return replace(
        original,
        files=(),
        run=replace(
            original.run,
            id=run_id,
            conclusion="failure",
            jobs=tuple(
                replace(j, conclusion="failure")
                if j.name in {"Wheel packaging tests", "build-and-publish"}
                else replace(j, conclusion="skipped")
                if j.name == "Publish to PyPI"
                else j
                for j in original.run.jobs
            )
            + (
                (
                    Job(
                        "Azure release certification / Deploy & Test "
                        "(azure-functions 2.x / Flex / Py 3.13)",
                        "skipped",
                    ),
                    Job("Trigger cookbook e2e re-verification", "skipped"),
                )
                if tuple(int(p) for p in version.split(".")) >= (0, 11, 0)
                else ()
            ),
        ),
    )


def test_historical_wheel_failure_is_visible_and_only_explicitly_covered() -> None:
    # Given: the same retained failed candidate with a later published successor.
    recorded = snapshot(failed(), facts())
    registry = load_registry(ROOT)
    registry = replace(
        registry, exceptions=tuple(e for e in registry.exceptions if e.tag == "v0.12.1")
    )
    # When: comparing evaluation with and without its registration.
    covered = evaluate(recorded, registry, NOW)[0]
    uncovered = evaluate(recorded, replace(registry, exceptions=()), NOW)[0]
    # Then: an exception reports rather than hides its covered findings.
    assert covered.verdict == "EXCEPTED"
    assert all(f.covered for f in covered.findings)
    assert {f.code for f in uncovered.findings} >= {"no-pypi-files", "publish-run-failed"}
    assert uncovered.verdict == "DEFECT"


@pytest.mark.parametrize(
    "code", ["exception-stale", "exception-evidence-mismatch", "exception-unknown-tag"]
)
def test_exception_drift_fails(code: str) -> None:
    # Given: the registered candidate has changed since verification.
    registry = load_registry(ROOT)
    registry = replace(
        registry, exceptions=tuple(e for e in registry.exceptions if e.tag == "v0.12.1")
    )
    original = failed()
    assert original.run
    changed = {
        "exception-stale": replace(original, files=facts().files),
        "exception-evidence-mismatch": replace(original, run=replace(original.run, id=999)),
        "exception-unknown-tag": replace(original, tag=None),
    }[code]
    # When: evaluating changed facts.
    result = evaluate(snapshot(changed, facts()), registry, NOW)[0]
    # Then: registration cannot hide new evidence.
    assert result.verdict == "DEFECT"
    assert code in {f.code for f in result.findings if not f.covered}


@pytest.mark.parametrize(
    "status,recent,verdict",
    [
        ("in_progress", False, "PENDING"),
        ("queued", False, "PENDING"),
        (None, True, "PENDING"),
        (None, False, "DEFECT"),
    ],
)
def test_pending_window(status: str | None, recent: bool, verdict: str) -> None:
    # Given: a candidate that has not finished publishing.
    original = facts()
    assert original.tag and original.run
    run = replace(original.run, status=status, conclusion=None) if status else None
    candidate = replace(
        original,
        files=(),
        run=run,
        tag=replace(original.tag, committed_at="2026-11-09T23:00:00Z" if recent else OLD),
    )
    # When: using a fixed evaluation clock.
    result = evaluate(snapshot(candidate), replace(load_registry(ROOT), exceptions=()), NOW)[0]
    # Then: only work in flight or within grace is non-failing.
    assert result.verdict == verdict


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", None])
def test_required_gate_must_succeed(conclusion: str | None) -> None:
    # Given: publish succeeded despite a required job not succeeding.
    original = facts()
    assert original.run
    jobs = tuple(
        replace(j, conclusion=conclusion) if j.name == "Library tests" else j
        for j in original.run.jobs
    )
    # When: evaluating the job evidence.
    result = evaluate(
        snapshot(replace(original, run=replace(original.run, jobs=jobs))),
        replace(load_registry(ROOT), exceptions=()),
        NOW,
    )[0]
    # Then: success of the outer run does not override its gates.
    assert "gate-not-successful" in {f.code for f in result.findings}


def test_publish_run_must_build_the_tagged_commit() -> None:
    # Given: a successful publish run whose head commit is not the tag's commit.
    original = facts()
    assert original.run
    # When: evaluating the run evidence.
    result = evaluate(
        snapshot(replace(original, run=replace(original.run, head_sha="other"))),
        replace(load_registry(ROOT), exceptions=()),
        NOW,
    )[0]
    # Then: a run of different source cannot vouch for the tag.
    assert result.verdict == "DEFECT"
    assert any(f.code == "gate-not-successful" and "head SHA" in f.message for f in result.findings)


def test_unknown_gate_era_fails_closed() -> None:
    # Given: policy starts after the candidate.
    registry = replace(load_registry(ROOT), exceptions=())
    registry = replace(
        registry, gate_policy=tuple(p for p in registry.gate_policy if p.since == "v0.11.0")
    )
    # When: evaluating the older candidate.
    result = evaluate(snapshot(facts("0.5.0")), registry, NOW)[0]
    # Then: no known policy is not proof of successful gates.
    assert result.verdict == "DEFECT"
    assert "gate-not-successful" in {f.code for f in result.findings}
