from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import re

import pytest

from tests.test_release_audit import NOW, ROOT, facts, snapshot
from tools.audit_releases import main
from tools.release_audit_model import JSON, AuditError
from tools.release_audit_parse import load, mapping, sequence
from tools.release_audit_registry import parse_registry
from tools.release_audit_snapshot import parse_snapshot


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("schema", 2, "schema"),
        ("surprise", True, "unknown"),
        ("pending_grace_hours", -1, "nonnegative"),
        ("repository", "wrong", "owner/name"),
        ("provenance_since", "0.5.0", "tag"),
        ("exceptions", {}, "array"),
        ("gate_policy", [], "gate_policy"),
        ("publish_workflow", "other.yml", "publish_workflow"),
    ],
)
def test_registry_rejects_bad_top_level(field: str, value: JSON, message: str) -> None:
    # Given: malformed maintainer configuration.
    data = mapping(load((ROOT / ".github/release-audit-exceptions.json").read_text()))
    data[field] = value
    # When / Then: parsing fails with a precise diagnostic.
    with pytest.raises(AuditError, match=message):
        parse_registry(data)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("tag", "0.2.0", "tag"),
        ("reason", "unknown", "reason"),
        ("run_id", True, "integer"),
        ("run_conclusion", "success", "failure"),
        ("confirmed", "true", "boolean"),
        ("successor", "0.1.0", "newer"),
        ("successor", "0.3.0", "itself registered"),
        ("successor", "v0.5.0", "version"),
        ("extra", None, "unknown"),
    ],
)
def test_registry_rejects_bad_exception(field: str, value: JSON, message: str) -> None:
    # Given: an invalid exception field.
    data = mapping(load((ROOT / ".github/release-audit-exceptions.json").read_text()))
    mapping(sequence(data["exceptions"])[0])[field] = value
    # When / Then: configuration is not silently accepted.
    with pytest.raises(AuditError, match=message):
        parse_registry(data)


def test_duplicate_exceptions_rejected() -> None:
    # Given: a repeated exception tag.
    data = mapping(load((ROOT / ".github/release-audit-exceptions.json").read_text()))
    entries = sequence(data["exceptions"])
    entries.append(entries[0])
    # When / Then: the registry fails closed.
    with pytest.raises(AuditError, match="duplicate"):
        parse_registry(data)


def registry_file(tmp_path: Path) -> Path:
    data = asdict(
        parse_registry(load((ROOT / ".github/release-audit-exceptions.json").read_text()))
    )
    data["exceptions"] = []
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("format_name", ["table", "markdown", "json"])
def test_replay_is_byte_deterministic(
    format_name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: a saved immutable snapshot and a fixed clock.
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(asdict(snapshot(facts()))))
    args = [
        "--snapshot-in",
        str(path),
        "--registry",
        str(registry_file(tmp_path)),
        "--format",
        format_name,
        "--now",
        NOW.isoformat(),
    ]
    # When: replaying the same evidence twice.
    first_code = main(args)
    first = capsys.readouterr().out
    second_code = main(args)
    second = capsys.readouterr().out
    # Then: neither rendering nor exit code depends on wall clock or network.
    assert first_code == second_code == 0
    assert first == second
    assert "PUBLISHED" in first


@pytest.mark.parametrize("bad", [False, True])
def test_cli_exit_code_tracks_defects(
    bad: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: either valid or defective evidence.
    candidate = facts()
    if bad:
        candidate = replace(candidate, files=())
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(asdict(snapshot(candidate))))
    # When: using the real offline CLI surface.
    code = main(["--snapshot-in", str(path), "--registry", str(registry_file(tmp_path))])
    # Then: defects alone determine failure.
    assert code == int(bad)
    assert f"exit={code}" in capsys.readouterr().out


def test_invalid_registry_cli_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Given: malformed configuration.
    path = tmp_path / "bad.json"
    path.write_text('{"schema": 1}')
    # When: invoking the CLI before any collection.
    code = main(["--registry", str(path)])
    # Then: no implicit pass or network access occurs.
    assert code == 2
    assert "missing" in capsys.readouterr().err


def test_snapshot_round_trip() -> None:
    # Given: plain recorded facts.
    original = snapshot(facts())
    # When: serializing and parsing at the trust boundary.
    replayed = parse_snapshot(load(json.dumps(asdict(original))))
    # Then: every fact survives without mutable containers.
    assert replayed == original


def test_release_status_table_matches_registry() -> None:
    # Given: the human-readable history and the machine-readable twin.
    page = (ROOT / "docs/release-status.md").read_text()
    registry = parse_registry(load((ROOT / ".github/release-audit-exceptions.json").read_text()))
    # When: extracting table facts, not pinning explanatory prose.
    rows = {}
    for line in page.splitlines():
        match = re.match(r"\| (v\d+\.\d+\.\d+) \|.*?\[(\d+)\].*\| ([\d.]+) \|$", line)
        if match:
            rows[match[1]] = (int(match[2]), match[3], "likely" not in line)
    # Then: tags, run evidence, successors and uncertainty cannot drift independently.
    assert rows == {e.tag: (e.run_id, e.successor, e.confirmed) for e in registry.exceptions}
