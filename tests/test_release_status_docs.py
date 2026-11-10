"""Doc-drift checks for the release-status page.

Issue: #834. A git tag or a GitHub Release is not proof that a version was published, and the page
records the tagged versions that have no files on PyPI. Deterministic, no network.

The recorded versions are a snapshot: this test cannot find a later release that fails to publish,
and #835 tracks the audit that can.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "docs" / "release-status.md").read_text()
# The page wraps its prose, so phrase checks run on the text with its whitespace collapsed.
PROSE = " ".join(PAGE.split())
TABLE_HEADING = "## Tagged versions without files on PyPI"
RUN_CELL = re.compile(
    r"\[(\d+)\]\(https://github\.com/yeongseon/azure-functions-langgraph-python/actions/runs/\1\)"
)
# Tag -> (GitHub Release date, failed "Publish to PyPI" run id, first published successor), as
# checked against the GitHub Releases API, the Actions run records, PyPI and git ancestry on
# 2026-11-10.
RECORDED = {
    "v0.2.0": ("2026-04-04", "23981822989", "0.5.0"),
    "v0.3.0": ("2026-04-04", "23984989252", "0.5.0"),
    "v0.4.0": ("2026-04-05", "23993056592", "0.5.0"),
    "v0.5.2": ("2026-04-18", "24605716212", "0.5.4"),
    "v0.5.3": ("2026-04-26", "24958757167", "0.5.4"),
    "v0.11.0": ("2026-10-11", "38141259504", "0.11.4"),
    "v0.11.1": ("2026-10-11", "38142818146", "0.11.4"),
    "v0.11.2": ("2026-10-11", "38144369700", "0.11.4"),
    "v0.11.3": ("2026-10-11", "38145575218", "0.11.4"),
    "v0.12.1": ("2026-10-16", "38891888298", "0.12.2"),
}
# Statements that were wrong or that the release process does not make, kept out on purpose.
FORBIDDEN = (
    "Only versions published on PyPI can be installed",
    "Only versions that PyPI lists can be installed",
    "could not succeed",
    "does not upload a distribution",
    "are not backfilled",
)


def _version(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.removeprefix("v").split("."))


def _run_id(cell: str) -> str:
    match = RUN_CELL.fullmatch(cell)
    return match.group(1) if match else ""


def _rows() -> list[list[str]]:
    section = PAGE.split(TABLE_HEADING, 1)[1].split("\n## ", 1)[0]
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("| v")
    ]


def test_page_is_in_the_navigation_and_linked_from_the_pages_readers_start_from() -> None:
    # Given: the nav and the pages that talk about installing and about releases
    nav = (ROOT / "mkdocs.yml").read_text()
    entry_points = {
        "README.md": ROOT / "README.md",
        "docs/installation.md": ROOT / "docs" / "installation.md",
        "docs/changelog.md": ROOT / "docs" / "changelog.md",
        "docs/release_process.md": ROOT / "docs" / "release_process.md",
    }

    # Then: the page is reachable from the nav and from each of them
    assert "release-status.md" in nav
    for name, path in entry_points.items():
        assert "release-status.md" in path.read_text(), name


def test_release_process_links_the_page_where_a_reader_decides_what_to_do() -> None:
    # Given: the step that creates the GitHub Release, and the recovery row for an unpublished tag
    process = (ROOT / "docs" / "release_process.md").read_text()
    step_two = process.split("## Step 2: Merge the Release PR", 1)[1].split("\n## ", 1)[0]
    recovery_row = next(
        line for line in process.splitlines() if line.startswith("| Tag exists but was never")
    )

    # Then: both point at the page, so a reader is not told only that a state is valid
    assert "release-status.md" in step_two
    assert "release-status.md" in recovery_row


def test_page_says_a_tag_or_github_release_does_not_mean_a_pypi_distribution() -> None:
    # Given: the opening of the page, before its first section
    head = PROSE.split(" ## ", 1)[0]

    # Then: it says that a tag or a GitHub Release does not mean available on PyPI
    assert "does **not** mean that a version is available on PyPI" in head


def test_table_rows_match_the_verified_facts_in_order() -> None:
    # Given: the table of the page, reduced to the facts that were verified
    rows = _rows()
    found = {row[0]: (row[1], _run_id(row[2]), row[4]) for row in rows}

    # Then: it is the verified mapping, ascending, so a new case is added on purpose
    assert found == RECORDED
    assert [row[0] for row in rows] == sorted(RECORDED, key=_version)


def test_each_row_has_a_reason_and_a_later_successor_that_is_not_itself_unpublished() -> None:
    for row in _rows():
        # Given: one row of the table
        tag, _released, _run, reason, successor = row

        # Then: it states what the records show, and the successor is newer and not recorded here
        assert reason, tag
        assert _version(f"v{successor}") > _version(tag), tag
        assert f"v{successor}" not in RECORDED, tag


def test_recovery_section_restates_the_release_process_without_new_promises() -> None:
    # Given: the release process that the page defers to, and the page's own recovery section
    process = " ".join((ROOT / "docs" / "release_process.md").read_text().split())

    # Then: the page links the recovery section that exists and states only what the process says
    assert "(release_process.md#recovery)" in PAGE
    assert re.search(r"^## Recovery$", (ROOT / "docs" / "release_process.md").read_text(), re.M)
    for sentence, source in (
        ("A tag that was never published is a valid resting state", "A valid resting state"),
        (
            "may be re-run on the unchanged tag after a transient or external failure",
            "after a transient or external failure",
        ),
        (
            "needs a new commit and a new release",
            "requires a new commit on `main` and a new Release",
        ),
        ("A tag is never moved or reused", "Never move or reuse a tag"),
    ):
        assert sentence in PROSE, sentence
        assert source in process, source


def test_page_and_entry_points_avoid_statements_that_were_wrong_or_are_not_policy() -> None:
    # Given: every file this change touches in prose
    texts = [
        PROSE,
        (ROOT / "README.md").read_text(),
        (ROOT / "docs" / "installation.md").read_text(),
        (ROOT / "docs" / "changelog.md").read_text(),
        (ROOT / "docs" / "release_process.md").read_text(),
    ]

    # Then: none of them contains a statement that review found false or that is not policy
    for phrase in FORBIDDEN:
        for text in texts:
            assert phrase not in text, phrase
