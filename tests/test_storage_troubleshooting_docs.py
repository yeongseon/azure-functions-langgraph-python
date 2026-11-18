"""Storage troubleshooting section matches the recorded observations (#926)."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from tests.test_docs_navigation import _anchors

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
TROUBLESHOOTING = DOCS / "troubleshooting.md"
HEADING = "## Storage and persistence settings"
ANCHOR = "storage-and-persistence-settings"

# Verbatim texts from the local experiment of 2026-11-18; the sanitized results are recorded in a
# comment on issue #926.
OBSERVED_FRAGMENTS = (
    "Worker failed to index functions",
    "KeyError: 'LANGGRAPH_POSTGRES_CONNECTION_STRING'",
    'connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed',
    'role "<your OS user name>" does not exist',
    'connection to server at "127.0.0.1", port 1 failed: Connection refused',
    "Set AZURE_STORAGE_BLOB_ACCOUNT_URL for Managed Identity, or AZURE_STORAGE_CONNECTION_STRING",
    "Connection string is either blank or malformed.",
    "Connection string missing required connection details.",
    "Requesting metadata from worker failed.",
    "ErrorCode:ContainerNotFound",
    '"code":"TableNotFound"',
    "Table create skipped at cold start",
    "79 to 84 s",
    "Failed to establish a new connection: [Errno 111] Connection refused",
)
NOT_REPRODUCED = (
    "missing managed-identity data role",
    "empty `AzureWebJobsStorage`",
    "SAS-authenticated recovery",
    "Storage URL userinfo variants",
)
VERSIONS = (
    "Core Tools 4.8.0",
    "`azure-functions-langgraph` 0.13.0",
    "`azure-storage-blob` 12.31.1",
    "`azure-data-tables` 12.7.0",
    "`azure-core` 1.41.0",
    "`psycopg` 3.3.9",
    "Azurite 3.35.0",
)
LINKERS = (
    (DOCS / "deployment.md", "troubleshooting.md#" + ANCHOR),
    (
        ROOT / "examples" / "postgres_checkpoint_production" / "README.md",
        "../../docs/troubleshooting.md#" + ANCHOR,
    ),
    (
        ROOT / "examples" / "production_persistent_agent" / "README.md",
        "../../docs/troubleshooting.md#" + ANCHOR,
    ),
    (
        ROOT / "examples" / "managed_identity_storage" / "README.md",
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
    # Given: the troubleshooting page
    # When: its anchors are computed
    # Then: the storage section is present and resolvable
    assert HEADING + "\n" in TROUBLESHOOTING.read_text()
    assert ANCHOR in _anchors(TROUBLESHOOTING)


@pytest.mark.parametrize("fragment", OBSERVED_FRAGMENTS)
def test_section_quotes_each_observed_fragment(fragment: str) -> None:
    # Given: the section; When: searching for an observed text; Then: it is present
    assert fragment in _section()


def test_section_lists_not_reproduced_items_and_environment() -> None:
    # Given: the section
    section = _section()

    # When/Then: unverified causes and the environment are stated
    assert "### Not reproduced" in section
    for item in NOT_REPRODUCED:
        assert item in section
    for version in VERSIONS:
        assert version in section
    assert "not Azure certification" in section
    assert "not a guarantee" in section


def test_section_does_not_claim_a_403_difference_it_did_not_observe() -> None:
    # Given: the section and its "Where to look" note
    section = _section()
    where_to_look = section.split("### Where to look", 1)[1].split("### ", 1)[0]

    # When/Then: a 403 is mentioned only in the not-reproduced list, as a caution
    assert "403" not in where_to_look
    assert section.count("403") == 1


@pytest.mark.parametrize(("page", "link"), LINKERS)
def test_pages_link_to_the_section(page: Path, link: str) -> None:
    # Given: a page that should point at the section; When: reading it; Then: the link exists
    assert link in page.read_text()


def test_postgres_setting_name_is_consistent() -> None:
    # Given: docs, examples and the root README
    files = [
        *DOCS.rglob("*.md"),
        *(ROOT / "examples").rglob("*.md"),
        *(ROOT / "examples").rglob("*.py"),
        ROOT / "README.md",
    ]

    # When: searching for the old standalone name
    stale = [
        str(f.relative_to(ROOT))
        for f in files
        if re.search(r"(?<![A-Z_])POSTGRES_CONN_STRING(?![A-Z_])", f.read_text())
    ]

    # Then: it appears nowhere
    assert stale == []
    assert "LANGGRAPH_POSTGRES_CONNECTION_STRING" in (DOCS / "production-guide.md").read_text()
