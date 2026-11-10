"""Doc-drift checks for the release-status page.

Issue: #834. A git tag or a GitHub Release is not proof that a version was distributed, and the
page records the tagged versions that never reached PyPI. Deterministic, no network.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "docs" / "release-status.md").read_text()
# The page wraps its prose, so phrase checks run on the text with its whitespace collapsed.
PROSE = " ".join(PAGE.split())
TABLE_HEADING = "## Tagged versions without a PyPI distribution"
RUN_CELL = re.compile(
    r"\[(\d+)\]\(https://github\.com/yeongseon/azure-functions-langgraph-python/actions/runs/\1\)"
)
# The tagged versions whose "Publish to PyPI" run failed and that have no file on PyPI, as checked
# against PyPI, the GitHub Releases API and the Actions run records on 2026-11-10.
RECORDED = (
    "v0.2.0",
    "v0.3.0",
    "v0.4.0",
    "v0.5.2",
    "v0.5.3",
    "v0.11.0",
    "v0.11.1",
    "v0.11.2",
    "v0.11.3",
    "v0.12.1",
)


def _version(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.removeprefix("v").split("."))


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


def test_page_says_a_tag_or_github_release_is_not_a_distribution() -> None:
    # Given: the opening of the page, before its first section
    head = PROSE.split(" ## ", 1)[0]

    # Then: it says that a tag or a GitHub Release does not mean installable, and names PyPI
    assert "does **not** mean that a version can be installed" in head
    assert "PyPI" in head


def test_table_records_exactly_the_verified_versions_in_order() -> None:
    # Given: the version column of the table
    versions = tuple(row[0] for row in _rows())

    # Then: it is the verified list, ascending, so a new case is added on purpose
    assert versions == RECORDED
    assert list(versions) == sorted(versions, key=_version)


def test_each_row_cites_its_failed_publish_run_and_names_a_later_published_successor() -> None:
    for row in _rows():
        # Given: one row of the table
        tag, released, run, reason, successor = row

        # Then: it has a release date, a run link of this repository, a reason and a successor
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", released), tag
        assert RUN_CELL.fullmatch(run), tag
        assert reason, tag
        assert re.fullmatch(r"\d+\.\d+\.\d+", successor), tag
        # And: the successor is newer than the tag and is not itself an unpublished version
        assert _version(f"v{successor}") > _version(tag), tag
        assert f"v{successor}" not in RECORDED, tag


def test_recovery_statement_matches_the_release_process() -> None:
    # Given: the release process that the page defers to
    process = (ROOT / "docs" / "release_process.md").read_text()

    # Then: the page links its recovery section, which exists and forbids moving a tag
    assert "(release_process.md#recovery)" in PAGE
    assert re.search(r"^## Recovery$", process, re.M)
    assert "Never move or reuse a tag" in " ".join(process.split())
    assert "does not move or reuse a tag" in PROSE
