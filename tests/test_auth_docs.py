"""Auth documentation section matches recorded observations (#952)."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from tests.test_docs_navigation import _anchors

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONFIGURATION = DOCS / "configuration.md"
HEADING = "### Native endpoint auth levels"
ANCHOR = "native-endpoint-auth-levels"

OBSERVED_FRAGMENTS = (
    "Core Tools",
    "does not enforce function keys locally",
    "Documentation metadata is descriptive, not enforcing",
    "not per-user authorization",
    "func.AuthLevel.FUNCTION",
    "func.AuthLevel.ANONYMOUS",
    "?code=<FUNCTION_KEY>",
)

NOT_IN_SCOPE = (
    "Live Azure provisioning",
    "cloud key generation",
    "expansion of experimental Durable/Platform contracts",
)

LINKERS = (
    (DOCS / "security.md", "configuration.md#" + ANCHOR),
    (DOCS / "installation.md", "configuration.md#" + ANCHOR),
    (ROOT / "README.md", "docs/configuration.md#" + ANCHOR),
)


def _section() -> str:
    text = CONFIGURATION.read_text()
    start = text.index(HEADING)
    nxt = re.search(r"^###? ", text[start + len(HEADING) :], re.MULTILINE)
    end = start + len(HEADING) + nxt.start() if nxt else len(text)
    return text[start:end]


def _normalized_section() -> str:
    return re.sub(r"\s+", " ", _section())


def test_section_exists_with_exact_heading_and_anchor() -> None:
    assert HEADING + "\n" in CONFIGURATION.read_text()
    assert ANCHOR in _anchors(CONFIGURATION)


@pytest.mark.parametrize("fragment", OBSERVED_FRAGMENTS)
def test_section_quotes_each_observed_fragment(fragment: str) -> None:
    assert fragment in _normalized_section()


def test_section_documents_scope_limits_and_validation() -> None:
    section = _normalized_section()
    for item in NOT_IN_SCOPE:
        assert item in section
    assert "Intentionally not run" in section


@pytest.mark.parametrize(("page", "link"), LINKERS)
def test_pages_link_to_the_section(page: Path, link: str) -> None:
    assert link in page.read_text()


def test_section_avoids_hardcoded_secrets() -> None:
    section = _section()
    assert "code=secret" not in section
    assert "x-functions-key: secret" not in section
    assert "$FUNCTION_KEY" in section
