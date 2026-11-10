from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
from time import monotonic

import pytest

from tests.test_release_notes import CFG, RP_BODY, FakeGitHub
from tools import release_notes as rn


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"incomplete_results": True, "total_count": 0},
        {"incomplete_results": False, "total_count": -1},
        {"incomplete_results": False, "total_count": False},
        {"incomplete_results": False, "total_count": "0"},
        {"incomplete_results": 0, "total_count": 0},
        [],
        None,
    ],
)
def test_prior_search_fails_closed_on_unverified_zero(
    response: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: no prior commit and an incomplete or malformed PR search
    client = rn.GitHubApi("acme/pkg")

    def get(path: str) -> object:
        if "/search/" in path:
            return response
        if "?author=" in path:
            return []
        return {"commit": {"committer": {"date": "2026-01-01"}}}

    monkeypatch.setattr(client, "_get", get)
    # When / Then: absence cannot be inferred from unverified evidence
    assert client.had_prior_contribution("newbie", "v1.0.0") is True


def test_prior_search_accepts_complete_integer_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a complete search with a validated count
    client = rn.GitHubApi("acme/pkg")
    responses: list[object] = [
        {"commit": {"committer": {"date": "2026-01-01"}}},
        [],
        {"incomplete_results": False, "total_count": 0},
    ]
    monkeypatch.setattr(client, "_get", lambda path: responses.pop(0))
    # When / Then: confirmed absence permits a new-contributor claim
    assert client.had_prior_contribution("newbie", "v1.0.0") is False


@pytest.mark.parametrize(
    "existing",
    [
        ["Not published to PyPI elsewhere."],
        ["Not published to PyPI", "Old explanation"],
    ],
)
def test_annotation_requires_entire_exact_block(existing: list[str]) -> None:
    # Given: a partial annotation or a substring collision
    parsed = rn.Parsed(notes=existing.copy())
    lines = ["Not published to PyPI", "Missing critical explanation"]
    # When: apply twice
    rn.apply_annotations(parsed, {"notes": lines})
    rn.apply_annotations(parsed, {"notes": lines})
    # Then: the complete clarification is appended exactly once
    assert parsed.notes == [*existing, "", *lines]


@pytest.mark.parametrize("size", [20_001, 100_000])
def test_long_line_is_refused_promptly(size: int) -> None:
    # Given: a pathological whitespace run, bounded independently of parser behavior
    def timeout(signum: int, frame: object) -> None:
        raise AssertionError("parser exceeded two seconds")

    previous = signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 2)
    start = monotonic()
    try:
        # When / Then: a clear size error precedes regex work
        with pytest.raises(ValueError, match="20.?000|line.*limit"):
            rn.parse_item("fix: x" + " " * size + "y", None)
        assert monotonic() - start < 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_whole_body_limit_is_refused() -> None:
    # Given: individually short lines over the total body limit
    body = ("x" * 1000 + "\n") * 500
    # When / Then: reject before parsing
    with pytest.raises(ValueError, match="500.?000|body.*limit"):
        rn.parse_body(body)


def test_backup_keeps_release_metadata_and_preview_date(tmp_path: Path) -> None:
    # Given: an undated historical body and a stable release identifier
    gh = FakeGitHub(bodies={"v1.0.1": "- fix: thing (#12)"})
    args = argparse.Namespace(tag="v1.0.1", dry_run=False, force=True, backup=str(tmp_path))
    # When: back up and preview that snapshot
    rn.cmd_apply(args, CFG, gh)
    data = json.loads((tmp_path / "v1.0.1.json").read_text())
    out = tmp_path / "preview"
    rn.cmd_preview(argparse.Namespace(backup=str(tmp_path), out=str(out)), CFG, gh)
    # Then: rollback identity and publication date survive the offline snapshot
    assert data == {
        "tagName": "v1.0.1",
        "body": "- fix: thing (#12)",
        "publishedAt": "2026-02-02T00:00:00Z",
        "id": 101,
    }
    assert (out / "v1.0.1.md").read_text().startswith("## v1.0.1 (2026-02-02)")


def test_existing_backup_refuses_overwrite_before_release_edit(tmp_path: Path) -> None:
    # Given: an original rollback snapshot already exists
    backup = tmp_path / "v1.0.1.json"
    backup.write_text("original rollback snapshot")
    gh = FakeGitHub(bodies={"v1.0.1": RP_BODY})
    args = argparse.Namespace(tag="v1.0.1", dry_run=False, force=True, backup=str(tmp_path))
    # When / Then: exclusive creation aborts without replacing either body
    with pytest.raises((ValueError, FileExistsError), match="exist|backup"):
        rn.cmd_apply(args, CFG, gh)
    assert backup.read_text() == "original rollback snapshot"
    assert gh.bodies["v1.0.1"] == RP_BODY


def test_concurrent_body_change_aborts_without_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a fresh API read sees a maintainer edit made after the build snapshot
    client = rn.GitHubApi("acme/pkg")
    calls: list[str] = []

    def request(method: str, path: str, payload: dict[str, object] | None = None) -> object:
        calls.append(method)
        return {"id": 101, "body": "important concurrent warning"}

    monkeypatch.setattr(client, "_request", request)
    # When / Then: mismatching original body is an error, never a PATCH
    with pytest.raises(rn.GitHubError, match="changed|concurrent"):
        client.set_body("v1.0.1", "replacement", "build snapshot")
    assert calls == ["GET"]


def test_dry_run_reports_skip_for_standardized_body(capsys: pytest.CaptureFixture[str]) -> None:
    # Given: an already standardized release
    gh = FakeGitHub(bodies={"v1.0.1": rn.MARKER})
    args = argparse.Namespace(tag="v1.0.1", dry_run=True, force=False, backup=None)
    # When: use the write command's dry-run mode
    result = rn.cmd_apply(args, CFG, gh)
    # Then: it explicitly describes the skip and does not edit
    assert result == 0
    assert "would be skipped" in capsys.readouterr().out
    assert gh.bodies["v1.0.1"] == rn.MARKER


def test_apply_passes_snapshot_to_concurrency_guard() -> None:
    # Given: attribution work races with a maintainer edit
    class ChangingGitHub(FakeGitHub):
        def pr_authors(self) -> dict[int, tuple[str, bool]]:
            self.bodies["v1.0.1"] = "important concurrent warning"
            return self.prs

    gh = ChangingGitHub(bodies={"v1.0.1": RP_BODY})
    args = argparse.Namespace(tag="v1.0.1", dry_run=False, force=True, backup=None)
    # When / Then: the body read before building is the expected write snapshot
    with pytest.raises(rn.GitHubError, match="changed"):
        rn.cmd_apply(args, CFG, gh)
    assert gh.bodies["v1.0.1"] == "important concurrent warning"


def test_apply_cli_refuses_oversized_body_without_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an oversized release and an otherwise valid apply command
    original = "- " + "x" * 20_001
    gh = FakeGitHub(bodies={"v1.0.1": original})
    monkeypatch.setattr(rn, "GitHubApi", lambda repo: gh)
    backup = tmp_path / "backup"
    # When: invoke the real CLI dispatch with a fake API
    result = rn.main(["apply", "v1.0.1", "--backup", str(backup)])
    # Then: nonzero exit precedes both backup and release writes
    assert result == 1
    assert gh.bodies["v1.0.1"] == original
    assert not backup.exists()
