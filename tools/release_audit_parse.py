"""Strict stdlib JSON boundary primitives (never echo external payloads)."""

from __future__ import annotations

import json

from tools.release_audit_model import JSON, AuditError


def load(raw: str) -> JSON:
    value: JSON = json.loads(raw)
    return value


def mapping(value: JSON) -> dict[str, JSON]:
    if not isinstance(value, dict):
        raise AuditError("expected JSON object")
    return value


def sequence(value: JSON) -> list[JSON]:
    if not isinstance(value, list):
        raise AuditError("expected JSON array")
    return value


def text(value: JSON) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise AuditError("expected nonempty text without control characters")
    return value


def integer(value: JSON) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AuditError("expected integer")
    return value


def boolean(value: JSON) -> bool:
    if not isinstance(value, bool):
        raise AuditError("expected boolean")
    return value


def optional_text(value: JSON) -> str | None:
    return None if value is None else text(value)


def texts(value: JSON) -> tuple[str, ...]:
    return tuple(text(item) for item in sequence(value))


def run_status(value: JSON) -> str:
    status = text(value)
    if status not in {"completed", "queued", "in_progress", "waiting", "pending", "requested"}:
        raise AuditError("unknown workflow run status")
    return status


def keys(data: dict[str, JSON], required: str, optional: str = "") -> None:
    expected = set(required.split())
    missing = expected - data.keys()
    unknown = data.keys() - expected - set(optional.split())
    if missing or unknown:
        raise AuditError(f"keys: missing={sorted(missing)}, unknown={sorted(unknown)}")
