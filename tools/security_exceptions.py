"""Exact static exceptions, adapted from the reviewed sibling evidence contract."""

from dataclasses import dataclass
from datetime import date
import json

from tools.security_report import JSON, EvidenceError, identifier, integer, mapping, sequence, text


@dataclass(frozen=True, slots=True)
class ExceptionRule:
    id: str
    file: str
    line: int


def parse_exceptions(raw: str, today: date) -> tuple[ExceptionRule, ...]:
    parsed: JSON = json.loads(raw)
    rules: list[ExceptionRule] = []
    for entry in sequence(parsed):
        item = mapping(entry)
        text(item["reason"])
        text(item["owner"])
        added = date.fromisoformat(text(item["added"]))
        expires = date.fromisoformat(text(item["expires"]))
        if not 0 < (expires - added).days <= 90 or added > today or expires <= today:
            raise EvidenceError("invalid exception validity window")
        if item["kind"] != "reviewed-static":
            raise EvidenceError("invalid exception kind")
        rule = ExceptionRule(
            identifier(item["id"]), identifier(item["file"]), integer(item["line"])
        )
        if rule in rules or rule.line == 0:
            raise EvidenceError("duplicate or invalid exception")
        rules.append(rule)
    return tuple(rules)
