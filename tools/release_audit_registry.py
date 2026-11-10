"""Parse the maintainer-owned exception and gate policy registry."""

from __future__ import annotations

from pathlib import Path
import re

from tools.release_audit_model import (
    JSON,
    AuditError,
    ExceptionEntry,
    GatePolicy,
    Registry,
    version_key,
)
from tools.release_audit_parse import boolean, integer, keys, load, mapping, sequence, text, texts


def tag(value: JSON) -> str:
    result = text(value)
    if not re.fullmatch(r"v\d+\.\d+\.\d+", result):
        raise AuditError(f"invalid tag: {result}")
    return result


def parse_registry(value: JSON) -> Registry:
    data = mapping(value)
    keys(
        data,
        "schema project repository repository_aliases main_branch publish_workflow "
        "pending_grace_hours provenance_since gate_policy exceptions",
    )
    if integer(data["schema"]) != 1:
        raise AuditError("registry schema must be 1")
    grace = integer(data["pending_grace_hours"])
    if grace < 0:
        raise AuditError("pending_grace_hours must be nonnegative")
    policies: list[GatePolicy] = []
    for raw in sequence(data["gate_policy"]):
        item = mapping(raw)
        keys(item, "since required_jobs", "required_job_prefixes")
        jobs = texts(item["required_jobs"])
        prefixes = texts(item.get("required_job_prefixes", []))
        if not jobs or len(set(jobs)) != len(jobs) or len(set(prefixes)) != len(prefixes):
            raise AuditError(
                "gate policy requires unique nonempty required_jobs and unique prefixes"
            )
        policies.append(GatePolicy(tag(item["since"]), jobs, prefixes))
    starts = [version_key(p.since) for p in policies]
    if not starts or starts != sorted(set(starts)):
        raise AuditError("gate_policy must be nonempty and ordered with unique since tags")
    exceptions: list[ExceptionEntry] = []
    for raw in sequence(data["exceptions"]):
        item = mapping(raw)
        keys(item, "tag reason run_id run_conclusion summary successor confirmed")
        reason = text(item["reason"])
        if reason not in {
            "publish-config",
            "tag-version-gate",
            "publish-failed",
            "wheel-tests",
            "azure-certification",
        }:
            raise AuditError(f"unknown exception reason: {reason}")
        run_id = integer(item["run_id"])
        if run_id <= 0 or text(item["run_conclusion"]) != "failure":
            raise AuditError("exception requires positive run_id and run_conclusion failure")
        successor = text(item["successor"])
        if not re.fullmatch(r"\d+\.\d+\.\d+", successor):
            raise AuditError(f"invalid successor version: {successor}")
        entry = ExceptionEntry(
            tag(item["tag"]),
            reason,
            run_id,
            "failure",
            text(item["summary"]),
            successor,
            boolean(item["confirmed"]),
        )
        if version_key(entry.successor) <= version_key(entry.tag):
            raise AuditError(f"successor must be newer than {entry.tag}")
        exceptions.append(entry)
    tags = [e.tag for e in exceptions]
    if len(set(tags)) != len(tags):
        raise AuditError("duplicate exception tag")
    if any("v" + e.successor in tags for e in exceptions):
        raise AuditError("successor is itself registered as an exception")
    repository = text(data["repository"])
    aliases = texts(data["repository_aliases"])
    if any(not re.fullmatch(r"[\w.-]+/[\w.-]+", r) for r in (repository, *aliases)):
        raise AuditError("repository and aliases must be owner/name")
    workflow = text(data["publish_workflow"])
    if workflow != "publish-pypi.yml":
        raise AuditError("publish_workflow must be publish-pypi.yml")
    return Registry(
        1,
        text(data["project"]),
        repository,
        aliases,
        text(data["main_branch"]),
        workflow,
        grace,
        tag(data["provenance_since"]),
        tuple(policies),
        tuple(exceptions),
    )


def load_registry(root: Path) -> Registry:
    return parse_registry(load((root / ".github/release-audit-exceptions.json").read_text("utf-8")))
