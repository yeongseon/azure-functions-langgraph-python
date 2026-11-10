"""Collect GitHub and PyPI GET responses as a replayable snapshot."""

from __future__ import annotations

import base64
from datetime import datetime
import re
from urllib.parse import quote

from tools.release_audit_http import Fetcher, get_json, pages
from tools.release_audit_model import (
    JSON,
    AuditError,
    File,
    Job,
    Publisher,
    Registry,
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
    mapping,
    optional_text,
    run_status,
    sequence,
    text,
)


def segment(value: str) -> str:
    return quote(value, safe="")


def collect_tag(fetcher: Fetcher, base: str, refs: tuple[str, str, str]) -> Tag:
    name, sha, main_sha = refs
    commit = mapping(get_json(fetcher, "api.github.com", base + "/commits/" + segment(sha)))
    date = text(mapping(mapping(commit["commit"])["committer"])["date"])
    timestamp(date)
    compare = mapping(
        get_json(
            fetcher, "api.github.com", base + "/compare/" + segment(sha) + "..." + segment(main_sha)
        )
    )
    status = text(compare["status"])
    if status not in {"identical", "ahead", "behind", "diverged"}:
        raise AuditError("unknown GitHub compare status")
    contents = mapping(
        get_json(
            fetcher,
            "api.github.com",
            base + "/contents/src/azure_functions_langgraph/__init__.py?ref=" + segment(sha),
        )
    )
    if text(contents["encoding"]) != "base64":
        raise AuditError("source contents must be base64 encoded")
    encoded = contents["content"]
    if not isinstance(encoded, str):
        raise AuditError("source contents must be base64 text")
    source = base64.b64decode(encoded.replace("\n", ""), validate=True).decode()
    matches = re.findall(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]\s*$', source, re.MULTILINE)
    if len(matches) != 1:
        raise AuditError("tag source must define exactly one __version__")
    return Tag(name, sha, matches[0], status in {"identical", "ahead"}, date)


def collect_run(fetcher: Fetcher, base: str, value: JSON) -> Run:
    data = mapping(value)
    run_id = integer(data["id"])
    jobs = tuple(
        Job(text(j["name"]), optional_text(j["conclusion"]))
        for j in (
            mapping(raw) for raw in pages(fetcher, base + f"/actions/runs/{run_id}/jobs", "jobs")
        )
    )
    created = text(data["created_at"])
    timestamp(created)
    return Run(
        run_id,
        text(data["html_url"]),
        run_status(data["status"]),
        optional_text(data["conclusion"]),
        text(data["head_sha"]),
        created,
        jobs,
    )


def collect_file(fetcher: Fetcher, identity: tuple[str, str], value: JSON) -> File:
    project, version = identity
    data = mapping(value)
    filename = text(data["filename"])
    provenance = get_json(
        fetcher,
        "pypi.org",
        "/integrity/" + "/".join(segment(s) for s in (project, version, filename)) + "/provenance",
        provenance=True,
    )
    publishers: list[Publisher] = []
    if provenance is not None:
        for bundle in sequence(mapping(provenance)["attestation_bundles"]):
            p = mapping(mapping(bundle)["publisher"])
            publishers.append(
                Publisher(
                    text(p["kind"]),
                    text(p["repository"]),
                    text(p["workflow"]),
                    text(p["environment"]),
                )
            )
    digest = text(mapping(data["digests"])["sha256"])
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise AuditError("PyPI sha256 must have 64 hexadecimal digits")
    uploaded = text(data["upload_time_iso_8601"])
    timestamp(uploaded)
    return File(
        filename,
        text(data["packagetype"]),
        digest,
        uploaded,
        boolean(data["yanked"]),
        tuple(publishers),
    )


def collect(registry: Registry, fetcher: Fetcher, now: datetime) -> Snapshot:
    base = "/repos/" + "/".join(segment(s) for s in registry.repository.split("/"))
    main = mapping(
        get_json(fetcher, "api.github.com", base + "/commits/" + segment(registry.main_branch))
    )
    main_sha = text(main["sha"])
    notes: list[str] = []
    tags: dict[str, tuple[str, str]] = {}
    releases: dict[str, Release] = {}
    runs: dict[str, JSON] = {}
    files: dict[str, tuple[File, ...]] = {}
    for raw in pages(fetcher, base + "/tags"):
        data = mapping(raw)
        name = text(data["name"])
        if not re.fullmatch(r"v\d+\.\d+\.\d+", name):
            notes.append(f"Not audited tag: {name}")
            continue
        tags[name[1:]] = (name, text(mapping(data["commit"])["sha"]))
    for raw in pages(fetcher, base + "/releases"):
        data = mapping(raw)
        name = text(data["tag_name"])
        if not re.fullmatch(r"v\d+\.\d+\.\d+", name):
            notes.append(f"Not audited release: {name}")
            continue
        published = optional_text(data["published_at"])
        if published:
            timestamp(published)
        releases[name[1:]] = Release(
            text(data["html_url"]), published, boolean(data["draft"]), boolean(data["prerelease"])
        )
    for raw in pages(
        fetcher,
        base + "/actions/workflows/" + segment(registry.publish_workflow) + "/runs",
        "workflow_runs",
    ):
        data = mapping(raw)
        branch = optional_text(data["head_branch"])
        if branch and re.fullmatch(r"v\d+\.\d+\.\d+", branch):
            previous = runs.get(branch[1:])
            if previous is None or integer(data["id"]) > integer(mapping(previous)["id"]):
                runs[branch[1:]] = raw
    pypi = mapping(get_json(fetcher, "pypi.org", "/pypi/" + segment(registry.project) + "/json"))
    for version, raw in mapping(pypi["releases"]).items():
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            notes.append(f"Not audited PyPI version: {version}")
            continue
        files[version] = tuple(
            sorted(
                (collect_file(fetcher, (registry.project, version), f) for f in sequence(raw)),
                key=lambda f: f.filename,
            )
        )
    versions: list[VersionFacts] = []
    for version in sorted(tags.keys() | releases.keys() | files.keys(), key=version_key):
        tag = None
        if version in tags:
            name, sha = tags[version]
            tag = collect_tag(fetcher, base, (name, sha, main_sha))
        versions.append(
            VersionFacts(
                version,
                tag,
                releases.get(version),
                collect_run(fetcher, base, runs[version]) if version in runs else None,
                files.get(version, ()),
            )
        )
    return Snapshot(
        now.isoformat(),
        registry.repository,
        registry.project,
        main_sha,
        tuple(versions),
        tuple(sorted(notes)),
    )
