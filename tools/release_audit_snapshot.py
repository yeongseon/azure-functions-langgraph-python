"""Parse persisted audit snapshots into immutable evidence."""

from __future__ import annotations

from tools.release_audit_model import (
    JSON,
    AuditError,
    File,
    Job,
    Publisher,
    Release,
    Run,
    Snapshot,
    Tag,
    VersionFacts,
    timestamp,
    version_key,
)
from tools.release_audit_parse import (
    boolean,
    integer,
    keys,
    mapping,
    optional_text,
    sequence,
    text,
    texts,
)


def parse_file(value: JSON) -> File:
    data = mapping(value)
    keys(data, "filename packagetype sha256 upload_time yanked publishers")
    publishers: list[Publisher] = []
    for raw in sequence(data["publishers"]):
        p = mapping(raw)
        keys(p, "kind repository workflow environment")
        publishers.append(
            Publisher(
                text(p["kind"]), text(p["repository"]), text(p["workflow"]), text(p["environment"])
            )
        )
    timestamp(text(data["upload_time"]))
    return File(
        text(data["filename"]),
        text(data["packagetype"]),
        text(data["sha256"]),
        text(data["upload_time"]),
        boolean(data["yanked"]),
        tuple(publishers),
    )


def parse_run(value: JSON) -> Run:
    data = mapping(value)
    keys(data, "id html_url status conclusion head_sha created_at jobs")
    jobs: list[Job] = []
    for raw in sequence(data["jobs"]):
        job = mapping(raw)
        keys(job, "name conclusion")
        jobs.append(Job(text(job["name"]), optional_text(job["conclusion"])))
    timestamp(text(data["created_at"]))
    return Run(
        integer(data["id"]),
        text(data["html_url"]),
        text(data["status"]),
        optional_text(data["conclusion"]),
        text(data["head_sha"]),
        text(data["created_at"]),
        tuple(jobs),
    )


def parse_version(value: JSON) -> VersionFacts:
    data = mapping(value)
    keys(data, "version tag release run files")
    version = text(data["version"])
    if version.startswith("v"):
        raise AuditError("snapshot version must not have v prefix")
    version_key(version)
    tag = None
    if data["tag"] is not None:
        t = mapping(data["tag"])
        keys(t, "name commit source_version ancestor committed_at")
        if text(t["name"]) != "v" + version:
            raise AuditError("snapshot tag name differs from version")
        timestamp(text(t["committed_at"]))
        tag = Tag(
            text(t["name"]),
            text(t["commit"]),
            text(t["source_version"]),
            boolean(t["ancestor"]),
            text(t["committed_at"]),
        )
    release = None
    if data["release"] is not None:
        r = mapping(data["release"])
        keys(r, "url published_at draft prerelease")
        published = optional_text(r["published_at"])
        if published:
            timestamp(published)
        release = Release(text(r["url"]), published, boolean(r["draft"]), boolean(r["prerelease"]))
    return VersionFacts(
        version,
        tag,
        release,
        parse_run(data["run"]) if data["run"] is not None else None,
        tuple(parse_file(f) for f in sequence(data["files"])),
    )


def parse_snapshot(value: JSON) -> Snapshot:
    data = mapping(value)
    keys(data, "collected_at repository project main_sha versions notes")
    timestamp(text(data["collected_at"]))
    versions = tuple(parse_version(v) for v in sequence(data["versions"]))
    if len({v.version for v in versions}) != len(versions):
        raise AuditError("duplicate snapshot version")
    return Snapshot(
        text(data["collected_at"]),
        text(data["repository"]),
        text(data["project"]),
        text(data["main_sha"]),
        versions,
        texts(data["notes"]),
    )
