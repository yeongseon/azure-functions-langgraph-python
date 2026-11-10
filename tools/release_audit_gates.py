"""Structured workflow job evidence and publication-run selection."""

from __future__ import annotations

from tools.release_audit_model import (
    ExceptionEntry,
    Finding,
    GatePolicy,
    Run,
    VersionFacts,
    version_key,
)


def select_run(facts: VersionFacts, policies: tuple[GatePolicy, ...]) -> Run | None:
    runs = facts.runs or ((facts.run,) if facts.run else ())
    eligible = [p for p in policies if version_key(p.since) <= version_key(facts.version)]
    if not runs:
        return None
    publish_job = max(eligible, key=lambda p: version_key(p.since)).publish_job if eligible else ""
    successful = [
        r for r in runs if any(j.name == publish_job and j.conclusion == "success" for j in r.jobs)
    ]
    return max(successful or list(runs), key=lambda r: r.id)


def covered_gate(finding: Finding, entry: ExceptionEntry) -> bool:
    return finding.code == "gate-not-successful" and (
        (finding.job_name in entry.failed_jobs and finding.conclusion == "failure")
        or (finding.job_name in entry.skipped_jobs and finding.conclusion == "skipped")
    )


def gate_errors(facts: VersionFacts, policies: tuple[GatePolicy, ...]) -> tuple[Finding, ...]:
    eligible = [p for p in policies if version_key(p.since) <= version_key(facts.version)]
    if not eligible:
        return (Finding("gate-not-successful", "No gate policy covers this version.", ()),)
    if facts.run is None:
        return ()
    policy = max(eligible, key=lambda p: version_key(p.since))
    jobs = facts.run.jobs
    errors = [
        Finding(
            "gate-not-successful",
            f"Job {j.name} concluded {j.conclusion}.",
            (),
            job_name=j.name,
            conclusion=j.conclusion,
        )
        for j in jobs
        if j.conclusion in {"failure", "cancelled"}
    ]
    for name in policy.required_jobs:
        matches = [j for j in jobs if j.name == name]
        if not matches and facts.run.status == "completed":
            errors.append(
                Finding(
                    "gate-not-successful", f"Required job {name} is missing.", (), job_name=name
                )
            )
        for job in matches:
            if job.conclusion != "success" and (
                job.conclusion is not None or facts.run.status == "completed"
            ):
                errors.append(
                    Finding(
                        "gate-not-successful",
                        f"Required job {name} did not succeed.",
                        (),
                        job_name=name,
                        conclusion=job.conclusion,
                    )
                )
    for prefix in policy.required_job_prefixes:
        matches = [j for j in jobs if j.name.startswith(prefix)]
        minimum = dict(policy.required_job_prefix_minimums).get(prefix, 1)
        if len(matches) < minimum and facts.run.status == "completed":
            errors.append(
                Finding(
                    "gate-not-successful",
                    f"Required job prefix {prefix} needs {minimum} jobs; found {len(matches)}.",
                    (),
                )
            )
        for job in matches:
            if job.conclusion != "success" and (
                job.conclusion is not None or facts.run.status == "completed"
            ):
                errors.append(
                    Finding(
                        "gate-not-successful",
                        f"Required prefix job {job.name} did not succeed.",
                        (),
                        job_name=job.name,
                        conclusion=job.conclusion,
                    )
                )
    return tuple(errors)
