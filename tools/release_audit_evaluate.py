"""Pure version-by-version release consistency decisions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import quote

from tools.release_audit_model import (
    AuditError,
    ExceptionEntry,
    Finding,
    GatePolicy,
    Registry,
    Snapshot,
    VersionAudit,
    VersionFacts,
    timestamp,
    version_key,
)


def covered_gate_messages(entry: ExceptionEntry) -> set[str]:
    failed = {"build-and-publish"}
    if entry.reason == "wheel-tests":
        failed = {"Wheel packaging tests"}
        if entry.tag == "v0.11.0":
            failed.add("Azure release certification / Deploy & Test")
    if entry.reason == "azure-certification":
        failed = {"Azure release certification / Deploy & Test"}
    return {f"Job {name} concluded failure." for name in failed} | {
        f"Required job {name} did not succeed." for name in failed | {"Publish to PyPI"}
    }


def gate_errors(facts: VersionFacts, policies: tuple[GatePolicy, ...]) -> tuple[str, ...]:
    eligible = [p for p in policies if version_key(p.since) <= version_key(facts.version)]
    if not eligible:
        return ("No gate policy covers this version.",)
    if facts.run is None:
        return ()
    policy = max(eligible, key=lambda p: version_key(p.since))
    jobs = facts.run.jobs
    errors = [
        f"Job {j.name} concluded {j.conclusion}."
        for j in jobs
        if j.conclusion in {"failure", "cancelled"}
    ]
    for name in policy.required_jobs:
        matches = [j for j in jobs if j.name == name]
        if not matches or any(j.conclusion != "success" for j in matches):
            errors.append(f"Required job {name} did not succeed.")
    for prefix in policy.required_job_prefixes:
        matches = [j for j in jobs if j.name.startswith(prefix)]
        if not matches or any(j.conclusion != "success" for j in matches):
            errors.append(f"Required job prefix {prefix} did not succeed.")
    return tuple(errors)


def audit_version(facts: VersionFacts, registry: Registry, now: datetime) -> VersionAudit:
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
    if run is None:
        add("no-publish-run", "No publish workflow run exists for this tag.")
    elif not pending:
        if run.conclusion != "success":
            add("publish-run-failed", "Publish workflow did not conclude success.")
        for error in gate_errors(facts, registry.gate_policy):
            add("gate-not-successful", error)
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
                if not file.publishers:
                    add("provenance-missing", f"PyPI file {file.filename} has no provenance.")
                elif any(
                    p.workflow != registry.publish_workflow
                    or p.repository not in (registry.repository, *registry.repository_aliases)
                    or p.kind != "GitHub"
                    or p.environment != "pypi"
                    for p in file.publishers
                ):
                    add(
                        "provenance-publisher-mismatch",
                        f"Unexpected publisher for {file.filename}.",
                    )
    entry = next((e for e in registry.exceptions if e.tag == "v" + facts.version), None)
    if entry:
        if tag is None:
            add("exception-unknown-tag", "Registered exception tag is absent from the snapshot.")
        if facts.files:
            add("exception-stale", "Registered unpublished candidate now has PyPI files.")
        if run is None or (run.id, run.conclusion) != (entry.run_id, entry.run_conclusion):
            add("exception-evidence-mismatch", "Exception run id/conclusion differs from evidence.")
        allowed = {"no-pypi-files", "publish-run-failed"}
        if entry.reason == "tag-version-gate":
            allowed.add("tag-version-mismatch")
        findings = [
            replace(f, covered=True)
            if f.code in allowed
            or (f.code == "gate-not-successful" and f.message in covered_gate_messages(entry))
            else f
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
