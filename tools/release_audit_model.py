"""Immutable release evidence shared by collection, replay and evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import TypeAlias

JSON: TypeAlias = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]


class AuditError(ValueError):
    """Evidence, registry or collection cannot support an audit."""


def version_key(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise AuditError(f"invalid version: {value}")
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise AuditError("timestamp must include timezone")
    return result


@dataclass(frozen=True, slots=True)
class Publisher:
    kind: str
    repository: str
    workflow: str
    environment: str


@dataclass(frozen=True, slots=True)
class Attestation:
    subject_name: str
    subject_sha256: str
    source_repository: str
    source_digest: str
    source_ref: str
    run_id: int
    publisher: Publisher


@dataclass(frozen=True, slots=True)
class File:
    filename: str
    packagetype: str
    sha256: str
    upload_time: str
    yanked: bool
    publishers: tuple[Publisher, ...]
    attestations: tuple[Attestation, ...] = ()


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    conclusion: str | None


@dataclass(frozen=True, slots=True)
class Run:
    id: int
    html_url: str
    status: str
    conclusion: str | None
    head_sha: str
    created_at: str
    jobs: tuple[Job, ...]
    event: str = "push"


@dataclass(frozen=True, slots=True)
class Release:
    url: str
    published_at: str | None
    draft: bool
    prerelease: bool


@dataclass(frozen=True, slots=True)
class Tag:
    name: str
    commit: str
    source_version: str
    ancestor: bool
    committed_at: str


@dataclass(frozen=True, slots=True)
class VersionFacts:
    version: str
    tag: Tag | None
    release: Release | None
    run: Run | None
    files: tuple[File, ...]
    runs: tuple[Run, ...] = ()


@dataclass(frozen=True, slots=True)
class Snapshot:
    collected_at: str
    repository: str
    project: str
    main_sha: str
    versions: tuple[VersionFacts, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GatePolicy:
    since: str
    required_jobs: tuple[str, ...]
    required_job_prefixes: tuple[str, ...] = ()
    required_job_prefix_minimums: tuple[tuple[str, int], ...] = ()
    publish_job: str = "Publish to PyPI"


@dataclass(frozen=True, slots=True)
class ExceptionEntry:
    tag: str
    reason: str
    run_id: int
    run_conclusion: str
    summary: str
    successor: str
    confirmed: bool
    failed_jobs: tuple[str, ...]
    skipped_jobs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Registry:
    schema: int
    project: str
    repository: str
    repository_aliases: tuple[str, ...]
    main_branch: str
    publish_workflow: str
    pending_grace_hours: int
    provenance_since: str
    gate_policy: tuple[GatePolicy, ...]
    exceptions: tuple[ExceptionEntry, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    message: str
    links: tuple[str, ...]
    covered: bool = False
    job_name: str | None = None
    conclusion: str | None = None


@dataclass(frozen=True, slots=True)
class VersionAudit:
    version: str
    release_url: str
    file_urls: tuple[str, ...]
    run_url: str
    run_conclusion: str
    verdict: str
    findings: tuple[Finding, ...]
    notes: tuple[str, ...] = ()
