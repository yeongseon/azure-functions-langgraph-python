"""Hostile evidence must fail before it reaches policy or a human terminal."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from tests.test_release_audit import NOW, ROOT, facts, snapshot
from tests.test_release_audit_io import registry_file
from tools.audit_releases import main
from tools.release_audit_evaluate import evaluate
from tools.release_audit_model import JSON, AuditError
from tools.release_audit_parse import mapping, sequence, text
from tools.release_audit_registry import load_registry
from tools.release_audit_render import render


@pytest.mark.parametrize(
    "field,value",
    [("sha256", "garbage"), ("upload_time", "2026-11-10T00:00:00")],
)
def test_hostile_file_exits_two(
    field: str, value: JSON, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: a syntactically valid snapshot with invalid file evidence.
    raw: JSON = json.loads(json.dumps(asdict(snapshot(facts()))))
    version = mapping(sequence(mapping(raw)["versions"])[0])
    mapping(sequence(version["files"])[0])[field] = value
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(raw))
    # When: replaying through the CLI boundary.
    code = main(["--snapshot-in", str(path), "--registry", str(registry_file(tmp_path))])
    # Then: malformed evidence is not a verdict.
    assert code == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    "status,conclusion,job_conclusion,run_id",
    [
        ("completed", None, "success", 123),
        ("in_progress", "success", "success", 123),
        ("unknown", None, "success", 123),
        ("completed", "garbage", "success", 123),
        ("completed", "success", "garbage", 123),
        ("completed", "success", "success", 0),
    ],
)
def test_hostile_run_exits_two(
    status: str,
    conclusion: str | None,
    job_conclusion: str,
    run_id: int,
    tmp_path: Path,
) -> None:
    # Given: inconsistent run semantics in otherwise valid recorded evidence.
    original = facts()
    assert original.run
    run = replace(
        original.run,
        status=status,
        conclusion=conclusion,
        id=run_id,
        jobs=(replace(original.run.jobs[0], conclusion=job_conclusion),),
    )
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(asdict(snapshot(replace(original, run=run)))))
    # When: replaying the evidence.
    code = main(["--snapshot-in", str(path), "--registry", str(registry_file(tmp_path))])
    # Then: invalid states never reach evaluation.
    assert code == 2


@pytest.mark.parametrize("control", ["\x7f", "\x80", "\x9b2J", "\x9f", "\u2028", "\u2029"])
def test_output_controls_are_rejected(control: str) -> None:
    # Given: externally supplied terminal or line controls.
    # When / Then: every text boundary rejects the unsafe value.
    with pytest.raises(AuditError):
        text("prefix" + control + "suffix")


def test_markdown_escapes_external_text_and_unsafe_urls() -> None:
    # Given: hostile notes, job names, filenames and release links.
    original = facts()
    assert original.run and original.release
    hostile = "[x](https://evil.example)"
    candidate = replace(
        original,
        release=replace(original.release, url="https://good.example/) <script>"),
        files=(replace(original.files[0], filename=hostile, yanked=True),),
        run=replace(
            original.run, jobs=(replace(original.run.jobs[0], name=hostile, conclusion="failure"),)
        ),
    )
    recorded = replace(snapshot(candidate), notes=("*note* `code` | <html> # ! ~ _ \\",))
    audits = evaluate(recorded, replace(load_registry(ROOT), exceptions=()), NOW)
    # When: rendering Markdown and the plain aligned table.
    markdown = render(recorded, audits, "markdown")
    table = render(recorded, audits, "table")
    # Then: external syntax remains literal, while the plain view is unchanged.
    assert hostile not in markdown
    assert "\\[x\\]" in markdown
    assert "[exists](https://good.example/)" not in markdown
    assert "<script>" not in markdown
    assert "note: \\*note\\* \\`code\\` \\| \\<html\\> \\# \\! \\~ \\_ \\\\" in markdown
    assert hostile in table
