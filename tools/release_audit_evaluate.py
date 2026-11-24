"""Pure version-by-version release consistency decisions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import quote

from tools.release_audit_gates import covered_gate, gate_errors, select_run
from tools.release_audit_model import (
    AuditError,
    Finding,
    Registry,
    Snapshot,
    VersionAudit,
    VersionFacts,
    timestamp,
    version_key,
)


def audit_version(facts: VersionFacts, registry: Registry, now: datetime) -> VersionAudit:
    facts = replace(facts, run=select_run(facts, registry.gate_policy))
    release, tag, run = facts.release, facts.tag, facts.run
    page = f"https://pypi.org/project/{quote(registry.project, safe='')}/{facts.version}/#files"
    release_url = release.url if release else ""
    run_url = run.html_url if run else ""
    links = tuple(url for url in (release_url, run_url, page) if url)
    findings: list[Finding] = []

    def add(code: str, message: str) -> None:
        findings.append(Finding(code, message, links))

    pending = run is not None and run.status != "completed"
    if run is None:
        dates = ([tag.committed_at] if tag else []) + (
            [release.published_at] if release and release.published_at else []
        )
        pending = any(
            timedelta(0) <= now - timestamp(d) < timedelta(hours=registry.pending_grace_hours)
            for d in dates
        )
    if tag:
        if release is None:
            add("tag-without-release", "Tag has no GitHub Release; inspect release creation.")
        if tag.source_version != facts.version:
            add("tag-version-mismatch", "Source __version__ differs from the tag version.")
        if not tag.ancestor:
            add("tag-not-on-main", "Tag commit is not an ancestor of the recorded main commit.")
    if release and not tag:
        add("release-without-tag", "Published GitHub Release has no tag in the snapshot.")
    if release and release.prerelease:
        add("release-prerelease", "Numeric GitHub Release is marked prerelease.")
    if run is None:
        add("no-publish-run", "No publish workflow run exists for this tag.")
    else:
        if run.status == "completed" and run.conclusion != "success":
            add("publish-run-failed", "Publish workflow did not conclude success.")
        findings.extend(
            replace(error, links=links) for error in gate_errors(facts, registry.gate_policy)
        )
        if tag and run.head_sha != tag.commit:
            add("gate-not-successful", "Publish run head SHA differs from the tag commit.")
    if release and not facts.files:
        add("no-pypi-files", "GitHub Release exists but PyPI has no distribution files.")
    if facts.files:
        if release is None:
            add("pypi-without-release", "PyPI files have no GitHub Release.")
        if tag is None or run is None or run.conclusion != "success" or pending:
            add(
                "pypi-without-successful-run",
                "PyPI files lack a successful publish run for the tag.",
            )
        if {f.packagetype for f in facts.files} != {"sdist", "bdist_wheel"}:
            add("pypi-incomplete", "PyPI must contain both wheel and source distributions.")
        for file in facts.files:
            if file.yanked:
                add("yanked", f"PyPI file {file.filename} is yanked.")
            if version_key(facts.version) >= version_key(registry.provenance_since):
                if not file.attestations:
                    add("provenance-missing", f"PyPI file {file.filename} has no provenance.")
                if any(
                    p.workflow != registry.publish_workflow
                    or p.repository not in (registry.repository, *registry.repository_aliases)
                    or p.kind != "GitHub"
                    or p.environment != "pypi"
                    for p in (*file.publishers, *(a.publisher for a in file.attestations))
                ):
                    add(
                        "provenance-publisher-mismatch",
                        f"Unexpected publisher for {file.filename}.",
                    )
                for attestation in file.attestations:
                    if (attestation.subject_name, attestation.subject_sha256) != (
                        file.filename,
                        file.sha256,
                    ):
                        add(
                            "provenance-subject-mismatch",
                            f"Attestation subject differs from {file.filename}.",
                        )
                    if (
                        attestation.source_repository
                        not in tuple(
                            "https://github.com/" + r
                            for r in (registry.repository, *registry.repository_aliases)
                        )
                        or tag is None
                        or attestation.source_digest != tag.commit
                        or attestation.source_ref != "refs/tags/" + tag.name
                        or run is None
                        or attestation.run_id != run.id
                    ):
                        add(
                            "provenance-source-mismatch",
                            f"Attestation source/run differs for {file.filename}.",
                        )
    entry = next((e for e in registry.exceptions if e.tag == "v" + facts.version), None)
    if entry:
        if tag is None:
            add("exception-unknown-tag", "Registered exception tag is absent from the snapshot.")
        if facts.files:
            add("exception-stale", "Registered unpublished candidate now has PyPI files.")
        if run is None or (run.id, run.conclusion) != (entry.run_id, entry.run_conclusion):
            add("exception-evidence-mismatch", "Exception run id/conclusion differs from evidence.")
        expected = tuple((name, "failure") for name in entry.failed_jobs) + tuple(
            (name, "skipped") for name in entry.skipped_jobs
        )
        if run is None or any(
            len([j for j in run.jobs if j.name == name and j.conclusion == conclusion]) != 1
            or len([j for j in run.jobs if j.name == name]) != 1
            for name, conclusion in expected
        ):
            add(
                "exception-evidence-mismatch", "Exception failed/skipped jobs differ from evidence."
            )
        allowed = {"no-pypi-files", "publish-run-failed"}
        if entry.reason == "tag-version-gate":
            allowed.add("tag-version-mismatch")
        findings = [
            replace(f, covered=True) if f.code in allowed or covered_gate(f, entry) else f
            for f in findings
        ]
    uncovered = [f for f in findings if not f.covered]
    waiting = {"no-publish-run", "no-pypi-files"}
    if uncovered and not (
        pending and not facts.files and all(f.code in waiting for f in uncovered)
    ):
        verdict = "DEFECT"
    elif pending:
        verdict = "PENDING"
    else:
        verdict = "EXCEPTED" if entry else "PUBLISHED"
    return VersionAudit(
        facts.version,
        release_url,
        tuple(page for _ in facts.files),
        run_url,
        (run.conclusion or run.status) if run else "absent",
        verdict,
        tuple(findings),
        tuple(
            f"Later run {r.id} ({r.event}): {r.conclusion or r.status} {r.html_url}"
            for r in facts.runs
            if run and r.id > run.id
        ),
    )


def evaluate(snapshot: Snapshot, registry: Registry, now: datetime) -> list[VersionAudit]:
    if (snapshot.repository, snapshot.project) != (registry.repository, registry.project):
        raise AuditError("snapshot repository/project differs from registry")
    recorded = {v.version: v for v in snapshot.versions}
    for registered in registry.exceptions:
        recorded.setdefault(
            registered.tag[1:], VersionFacts(registered.tag[1:], None, None, None, ())
        )
    audits = [audit_version(recorded[v], registry, now) for v in sorted(recorded, key=version_key)]
    result: list[VersionAudit] = []
    for audit in audits:
        entry = next((e for e in registry.exceptions if e.tag == "v" + audit.version), None)
        if entry and (entry.successor not in recorded or not recorded[entry.successor].files):
            audit = replace(
                audit,
                verdict="DEFECT",
                findings=(
                    *audit.findings,
                    Finding(
                        "exception-evidence-mismatch",
                        "Recorded successor has no PyPI files in snapshot.",
                        (
                            f"https://pypi.org/project/{quote(registry.project, safe='')}/"
                            f"{entry.successor}/",
                        ),
                    ),
                ),
            )
        result.append(audit)
    return result
