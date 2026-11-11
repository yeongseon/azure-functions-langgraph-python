from __future__ import annotations

import argparse
from collections.abc import Callable
from functools import partial
import json
from pathlib import Path
import signal

import pytest

from tests.test_release_notes import CFG, RP_BODY, FakeGitHub, commit
from tools import release_notes as rn


def _within(seconds: float, action: Callable[[], object]) -> None:
    def expired(signum: int, frame: object) -> None:
        raise AssertionError(f"did not finish within {seconds} seconds")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        action()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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
    def refuse() -> None:
        # When / Then: a clear size error precedes regex work
        with pytest.raises(ValueError, match="20.?000|line.*limit"):
            rn.parse_item("fix: x" + " " * size + "y", None)

    _within(2, refuse)


@pytest.mark.parametrize(
    ("line", "repeat"),
    [
        pytest.param("<!--" * 4_997, 24, id="comment-openers"),
        pytest.param("## a" + " " * 19_900 + "b", 24, id="heading-whitespace-run"),
        pytest.param("## v1.0.0" + "(" * 19_900 + " x", 24, id="version-heading-parentheses"),
        pytest.param("## v1.0." + "1" * 19_900 + " x", 24, id="version-heading-digits"),
        pytest.param("**Full Changelog**: " + "compare/" * 2_400, 24, id="compare-link-chain"),
        pytest.param("([#12](https://github.com/" * 760, 2, id="reference-wrapper-openers"),
        pytest.param("[" * 19_990, 8, id="open-brackets"),
        pytest.param("[a](" * 4_997, 2, id="link-openers"),
        pytest.param("[x](" + "(" * 19_980, 8, id="unclosed-destination"),
        pytest.param("[a]" * 6_600, 8, id="label-without-destination"),
        pytest.param("[a](b)" * 3_300, 8, id="adjacent-links"),
        pytest.param("\\" * 19_990, 8, id="backslashes"),
        pytest.param("`" * 19_990, 8, id="backtick-run"),
        pytest.param("`a" * 9_995, 8, id="backtick-pairs"),
        pytest.param("`" + "``".join(["a"] * 6_000), 8, id="backtick-runs-without-closers"),
        pytest.param(
            "".join("`" * n + "x" for n in range(1, 199)), 24, id="increasing-backtick-runs"
        ),
        pytest.param("[`" * 9_995, 8, id="label-code-span-openers"),
        pytest.param('[a](b "' * 2_850, 2, id="quoted-title-openers"),
        pytest.param("[a](<" * 3_990, 2, id="angle-destination-openers"),
        pytest.param("[a](" + "b" * 19_900, 8, id="long-destination"),
        pytest.param("[a](" + "(" * 19_980, 8, id="nested-parenthesis-openers"),
        pytest.param("![a [b](c)](d) " * 1_300, 8, id="links-inside-images"),
        pytest.param("[" * 9_990 + "]" * 9_990, 8, id="deeply-nested-labels"),
    ],
)
def test_bodies_near_the_size_limits_are_parsed_and_verified_in_linear_time(
    line: str, repeat: int
) -> None:
    # Given: the largest accepted body, one adversarial line shape repeated, bare and as an item
    bodies = [
        "\n".join([line] * repeat),
        "\n".join(["- " + line[: rn.MAX_LINE - 2]] * repeat),
    ]
    assert all(len(body) <= rn.MAX_BODY for body in bodies)
    for body in bodies:
        # When / Then: neither parsing nor verifying rescans the same text quadratically
        _within(1, partial(rn.parse_body, body, CFG))
        _within(1, partial(rn.verify, body, body, "v1.0.1", CFG))


def test_reference_wrapper_pattern_does_not_rescan_the_wrappers_that_follow() -> None:
    # Given: one line of wrapper openers, none of them closed
    text = "([#12](https://github.com/" * 760
    # When / Then: each search fails at every opener in constant time, not by reading to the end
    _within(1, lambda: [rn._PARSE_TOKENS.search(text) for _ in range(24)])


def test_a_block_with_hundreds_of_thousands_of_blank_lines_is_trimmed_in_linear_time() -> None:
    # Given: blank lines on both sides of one line of text inside a Notes block
    body = "### Notes\n" + "\n" * 200_000 + "text\n" + "\n" * 200_000
    # When / Then: trimming the block does not shift the list once per blank line
    _within(2, lambda: rn.parse_body(body, CFG))
    assert rn.parse_body(body, CFG).notes == ["text"]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("-" * 72, id="dash-rule"),
        pytest.param("<" * 19_900, id="unclosed-tag-openers"),
        pytest.param("<!--" * 4_997, id="comment-openers"),
    ],
)
def test_commit_message_lines_near_the_size_limits_are_reconstructed_in_linear_time(
    line: str,
) -> None:
    # Given: an adversarial line repeated through a large breaking footer
    footer = "\n".join(["BREAKING CHANGE: x", *[line] * 23])
    gh = FakeGitHub(commits={("v1.0.0", "v1.0.1"): [commit("a", "fix: x", "maint", footer)]})
    # When / Then: classifying and neutralizing each line takes time linear in its length
    _within(1, lambda: rn.build_notes("v1.0.1", "", gh, CFG))


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


def test_a_release_without_a_body_is_not_mistaken_for_a_concurrent_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: GitHub answers null for a release that has no body, and its build snapshot is ""
    client = rn.GitHubApi("acme/pkg")
    calls: list[str] = []

    def request(method: str, path: str, payload: dict[str, object] | None = None) -> object:
        calls.append(method)
        return {"id": 101, "body": None}

    monkeypatch.setattr(client, "_request", request)
    # When: the notes are written
    client.set_body("v1.0.1", "new notes", "")
    # Then: the write goes through instead of being refused as a concurrent edit
    assert calls == ["GET", "PATCH"]


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
