"""Navigation and internal-link validation for the published MkDocs site (#884)."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# Pages deliberately not in the nav. Keep this empty unless a page has a reason
# to stay hidden; every entry needs a comment.
UNLISTED: frozenset[str] = frozenset()

# Maintainer-only pages must stay in the "Maintainers" section, never the newcomer path.
MAINTAINER_PAGES = {
    "agent-playbook.md",
    "review-evidence-policy.md",
    "hardening-status.md",
    "deferred-edge-triage.md",
    "release_process.md",
    "contributing.md",
    "development.md",
    "testing.md",
}

NEWCOMER_ORDER = [
    "Home",
    "Getting Started",
    "Deploy",
    "Production Checklist",
    "Examples",
    "Reference",
    "Troubleshooting",
    "Maintainers",
]


class _Loader(yaml.SafeLoader):  # type: ignore[misc]
    pass


_Loader.add_multi_constructor("", lambda loader, suffix, node: None)


def _load_config() -> dict[str, Any]:
    config: dict[str, Any] = yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=_Loader)  # noqa: S506
    return config


def _walk(nav: Any) -> list[tuple[str, str]]:
    """Return (top-level section, target) for each leaf in the nav."""
    leaves: list[tuple[str, str]] = []

    def visit(node: Any, top: str) -> None:
        if isinstance(node, str):
            leaves.append((top, node))
        elif isinstance(node, list):
            for item in node:
                visit(item, top)
        elif isinstance(node, dict):
            for key, value in node.items():
                visit(value, top or key)

    for entry in nav:
        if isinstance(entry, dict):
            ((title, value),) = entry.items()
            visit(value, title)
        else:
            visit(entry, "")
    return leaves


def _slug(heading: str) -> str:
    text = re.sub(r"`", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    for line in path.read_text().splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        match = None if in_fence else re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match:
            slug = _slug(match.group(1))
            n = counts.get(slug, 0)
            counts[slug] = n + 1
            anchors.add(slug if n == 0 else f"{slug}_{n}")
    anchors.update(re.findall(r'id="([^"]+)"', path.read_text()))
    return anchors


def test_nav_entries_resolve_to_files() -> None:
    missing = [
        target
        for _, target in _walk(_load_config()["nav"])
        if not target.startswith("https://") and not (DOCS / target).is_file()
    ]
    assert missing == []


def test_every_docs_page_is_reachable_from_nav() -> None:
    in_nav = {t for _, t in _walk(_load_config()["nav"]) if not t.startswith("https://")}
    pages = {p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*.md")}
    assert sorted(pages - in_nav - UNLISTED) == []
    assert sorted(UNLISTED & in_nav) == [], "UNLISTED entries must not be in the nav"
    assert sorted(UNLISTED - pages) == [], "UNLISTED lists a page that does not exist"


def test_newcomer_progression_and_maintainer_separation() -> None:
    nav = _load_config()["nav"]
    titles = [next(iter(e)) if isinstance(e, dict) else e for e in nav]
    assert titles == NEWCOMER_ORDER
    leaves = _walk(nav)
    for top, target in leaves:
        if target in MAINTAINER_PAGES:
            assert top == "Maintainers", target
    in_maint = {t for top, t in leaves if top == "Maintainers"}
    assert MAINTAINER_PAGES <= in_maint


def test_examples_nav_has_learning_tiers() -> None:
    nav = _load_config()["nav"]
    examples = next(e["Examples"] for e in nav if isinstance(e, dict) and "Examples" in e)
    assert [next(iter(e)) for e in examples] == ["Beginner", "Production", "Advanced"]
    for target in (t for top, t in _walk(_load_config()["nav"]) if top == "Examples"):
        if target.startswith("https://"):
            folder = target.rsplit("/examples/", 1)[1]
            assert (ROOT / "examples" / folder).is_dir(), target


def test_home_page_links_every_newcomer_stage() -> None:
    index = (DOCS / "index.md").read_text()
    for page in (
        "installation.md",
        "getting-started.md",
        "tutorial-5-min.md",
        "deployment.md",
        "production-guide.md",
        "api.md",
        "troubleshooting.md",
    ):
        assert f"]({page})" in index, page


_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _doc_links() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for page in sorted(DOCS.rglob("*.md")):
        in_fence = False
        for line in page.read_text().splitlines():
            if line.lstrip().startswith(("```", "~~~")):
                in_fence = not in_fence
            if in_fence:
                continue
            for target in _LINK.findall(re.sub(r"(`+)[^`].*?\1", "", line)):
                if not re.match(r"[a-z][a-z0-9+.-]*:", target):
                    found.append((page, target))
    return found


def _link_id(value: object) -> str:
    return value.relative_to(ROOT).as_posix() if isinstance(value, Path) else str(value)


@pytest.mark.parametrize(("page", "target"), _doc_links(), ids=_link_id)
def test_internal_links_and_anchors_resolve(page: Path, target: str) -> None:
    path_part, _, fragment = target.partition("#")
    dest = (page.parent / unquote(path_part)).resolve() if path_part else page
    if path_part and not dest.exists():
        pytest.fail(f"{page.relative_to(ROOT)} links to missing {target}")
    if fragment and dest.suffix == ".md" and dest.is_relative_to(DOCS):
        assert fragment in _anchors(dest), f"{page.relative_to(ROOT)}: no anchor {target}"
