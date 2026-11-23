"""The entrypoint-facade decision record quotes verified host texts and baseline numbers (#930)."""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = (ROOT / "docs" / "api-boundary.md").read_text()
BASELINE = (ROOT / "docs" / "onboarding-baseline.md").read_text()
HEADING = "## 8. A thin entrypoint facade (proposal, #930)"
VERIFIED_HOST_TEXTS = (
    "Worker failed to index functions",
    "ValueError: Could not find top level function app instances in function_app.py.",
    "No job functions found",
    "ValueError: More than one FunctionApp or other top level function app instances are defined.",
)


def _section() -> str:
    return BOUNDARY[BOUNDARY.index(HEADING) :]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _baseline(label: str) -> str:
    for line in BASELINE.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0] == label:
            return cells[1]
    raise AssertionError(f"row {label!r} missing from docs/onboarding-baseline.md")


def test_section_is_the_last_one_and_follows_the_decisions_list() -> None:
    # Given: the page and its headings
    headings = re.findall(r"^## (\d+)\. ", BOUNDARY, re.MULTILINE)

    # When/Then: section 8 exists once, after section 7, and nothing follows it
    assert headings == [str(number) for number in range(1, 9)]
    assert BOUNDARY.count(HEADING) == 1
    assert "## 7. Decisions needed from a maintainer" in BOUNDARY
    assert BOUNDARY.index("## 7. ") < BOUNDARY.index(HEADING)


def test_section_quotes_each_verified_host_text() -> None:
    # Given: the section
    section = _section()

    # When/Then: every host text observed for the three module shapes is quoted verbatim
    for text in VERIFIED_HOST_TEXTS:
        assert text in section


def test_section_names_three_options_recommends_none_and_lists_what_is_not_validated() -> None:
    # Given: the section
    section = _flat(_section())

    # When/Then: the options, the recommendation, the triggers and the gaps are all present
    assert "**A. No facade (proposed)**" in section
    assert "B. A function that builds and returns the function app" in section
    assert "C. `LangGraphApp` is itself the function app the worker finds" in section
    assert "**Option A: do not add a facade now.**" in section
    assert "Revisit when:" in section
    assert "### What was not validated" in section
    assert "**not validated.**" in section
    assert "no Azure-hosted behavior is claimed" in section


def test_quoted_numbers_equal_the_frozen_baseline() -> None:
    # Given: the section's numbers and the baseline page's frozen Baseline column
    section = _flat(_section())

    # When: they are extracted
    lines_and_statements = re.search(r"(\d+) code lines and (\d+) top-level statements", section)
    first_run = re.search(
        r"(\d+) command lines to the first local response, (\d+) files and (\d+) requirements",
        section,
    )
    exports = re.search(r"which exports (\d+) names today", section)
    parameters = re.search(r"it forwards only some of the (\d+) constructor parameters", section)

    # Then: each equals the Baseline cell it claims to quote
    assert lines_and_statements and first_run and exports and parameters
    assert lines_and_statements.group(1) == _baseline("Integration code lines")
    assert lines_and_statements.group(2) == _baseline("Integration top-level statements")
    assert first_run.group(1) == _baseline("Command lines to the first local response")
    assert first_run.group(2) == _baseline("Minimal app files")
    assert first_run.group(3) == _baseline("Minimal app requirements")
    assert exports.group(1) == _baseline("Package exported names (snapshot)")
    assert parameters.group(1) == _baseline("Public LangGraphApp parameters (snapshot)")


def test_decision_is_listed_for_the_maintainer_and_the_banner_names_the_issue() -> None:
    # Given: the page
    decisions = _flat(BOUNDARY.split("## 7. Decisions needed from a maintainer", 1)[1])
    banner = _flat(BOUNDARY[BOUNDARY.index("> **Status") :].split("\n\n", 1)[0])

    # When/Then: the facade is a numbered decision pointing at section 8, and #930 is named
    assert "4. Decide on the entrypoint facade of section 8." in decisions
    assert "issues #878 and #930" in banner
