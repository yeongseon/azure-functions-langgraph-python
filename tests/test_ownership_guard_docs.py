"""Documentation contracts for the opt-in ownership guard (#790).

The guard is easy to over-describe (it is not fencing) and easy to leave out of the
reference. These checks keep the operator guide, the API reference and the design
record aligned with the code, without pinning prose that is meant to evolve.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from azure_functions_langgraph import locks
from azure_functions_langgraph.locks.guard import OWNERSHIP_GUARD_CONFIG_KEY

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "production-guide.md"
DOCS = ROOT / "docs"
SECTION_HEADING = "Opt-in guard for Blob checkpoint writes (#790)"


def _slug(heading: str) -> str:
    """Mirror the Markdown toc slug that MkDocs generates for a heading."""
    text = re.sub(r"[^\w\s-]", "", heading.lower().strip())
    return re.sub(r"[-\s]+", "-", text)


def _headings(path: Path) -> set[str]:
    return {
        _slug(match.group(1))
        for match in re.finditer(r"^#{1,6}\s+(.*?)\s*$", path.read_text(), flags=re.MULTILINE)
    }


def _plain(text: str) -> str:
    """Drop bold markers so wording checks do not depend on where the emphasis sits."""
    return text.replace("**", "")


def _section() -> str:
    text = GUIDE.read_text()
    start = text.index(f"#### {SECTION_HEADING}")
    end = text.index("\n### ", start)
    return _plain(text[start:end])


def test_production_guide_documents_the_contract_and_its_limits() -> None:
    # Given: the operator-facing section for the guard.
    section = _section()

    # Then: it names the switches, the error, every reason, and the honest limits.
    for needle in (
        "guard_checkpoint_writes=True",
        "require_ownership_guard=True",
        "ThreadLeaseLostError",
        "`lost`",
        "`released`",
        "`superseded`",
        "`missing`",
        "It is not fencing.",
        "not proof",
        'durability="async"',
        "`session_exclusive=True`",
        "`delete_thread()`",
        "Beta",
    ):
        assert needle in section, needle
    # And: it never claims a guarantee the code cannot give.
    for forbidden in ("guarantee", "prevents stale", "exactly-once"):
        assert forbidden not in section.lower(), forbidden


def test_guide_no_longer_calls_the_shipped_guard_unimplemented() -> None:
    # Given: the guide as a whole (the decision bullet used to say "not implemented yet").
    text = GUIDE.read_text()

    # Then: no stale sentence remains, and the decision bullet points at the new section.
    assert not re.search(r"not\s+implemented\s+yet", text)
    assert _slug(SECTION_HEADING) in text


@pytest.mark.parametrize(
    "doc",
    [
        "README.md",
        "docs/production-guide.md",
        "docs/ops-and-apis.md",
        "docs/adapter-reference.md",
        "docs/architecture.md",
    ],
)
def test_every_link_to_a_guard_anchor_resolves(doc: str) -> None:
    # Given: links in the docs that point at the guard sections.
    source = ROOT / doc
    targets = re.findall(r"\(((?:[\w./-]+\.md)?)#(opt-in-[\w-]+)\)", source.read_text())

    # Then: every such link names a heading that exists in its target document.
    for target_doc, anchor in targets:
        path = (source.parent / target_doc) if target_doc else source
        assert anchor in _headings(path.resolve()), (doc, target_doc, anchor)
    if doc != "docs/architecture.md":
        assert targets, f"{doc} no longer links to the guard section"


def test_api_reference_lists_every_public_lock_name() -> None:
    # Given: the public names of the locks package.
    api = (DOCS / "api.md").read_text()

    # Then: each one has a mkdocstrings entry, so a new export cannot ship undocumented.
    for name in locks.__all__:
        assert f"::: azure_functions_langgraph.locks.{name}" in api, name


def test_design_record_names_the_private_key_and_the_decisions() -> None:
    # Given: the decision record.
    design = (ROOT / "DESIGN.md").read_text()
    section = _plain(design[design.index("### 15. Blob stale-writer decision record") :])

    # Then: it records the exact private key and the non-obvious decisions.
    assert OWNERSHIP_GUARD_CONFIG_KEY in section
    assert "does not infer expiry from the clock" in section
    assert "lock-free" in section
    assert "scoped binding" in section
    assert "not fencing" in section.lower() or "not a fence" in section.lower()
