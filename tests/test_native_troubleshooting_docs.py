"""Native invocation troubleshooting section matches recorded observations (#939)."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from tests.test_docs_navigation import _anchors

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
TROUBLESHOOTING = DOCS / "troubleshooting.md"
HEADING = "## No-credentials native invocation troubleshooting"
ANCHOR = "no-credentials-native-invocation-troubleshooting"

OBSERVED_FRAGMENTS = (
    '"greeting": "Hello, World!"',
    "Hello, World! Goodbye!",
    '"loc": ["input"]',
    '"type": "missing"',
    "Only config.configurable is client configurable",
    "Unapproved config.configurable field",
    "Graph execution failed",
)

NOT_IN_SCOPE = (
    (
        "Platform (`/threads`, `/runs`, `/assistants`) and Durable"
        ' (`async_runs="durable"`) endpoints'
    ),
    "intentionally out of scope here",
)

VERSIONS = (
    "Azure Functions Core Tools 4.8.0",
    "`azure-functions-langgraph` 0.13.0",
    "`examples/simple_agent`",
)

LINKERS = (
    (DOCS / "getting-started.md", "troubleshooting.md#" + ANCHOR),
    (DOCS / "tutorial-5-min.md", "troubleshooting.md#" + ANCHOR),
    (
        ROOT / "examples" / "simple_agent" / "README.md",
        "../../docs/troubleshooting.md#" + ANCHOR,
    ),
)


def _section() -> str:
    text = TROUBLESHOOTING.read_text()
    start = text.index(HEADING)
    nxt = re.search(r"^## ", text[start + len(HEADING) :], re.MULTILINE)
    end = start + len(HEADING) + nxt.start() if nxt else len(text)
    return text[start:end]


def test_section_exists_with_exact_heading_and_anchor() -> None:
    assert HEADING + "\n" in TROUBLESHOOTING.read_text()
    assert ANCHOR in _anchors(TROUBLESHOOTING)


@pytest.mark.parametrize("fragment", OBSERVED_FRAGMENTS)
def test_section_quotes_each_observed_fragment(fragment: str) -> None:
    assert fragment in _section()


def test_section_documents_scope_limits_and_validation() -> None:
    section = _section()
    for item in NOT_IN_SCOPE:
        assert item in section
    for version in VERSIONS:
        assert version in section
    assert "Intentionally not run" in section


@pytest.mark.parametrize(("page", "link"), LINKERS)
def test_pages_link_to_the_section(page: Path, link: str) -> None:
    assert link in page.read_text()
