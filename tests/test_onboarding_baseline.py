"""Recompute the first-run baseline page from the example and tutorial (#917)."""

from __future__ import annotations

import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "onboarding-baseline.md"
TUTORIAL = ROOT / "docs" / "tutorial-5-min.md"
EXAMPLE = ROOT / "examples" / "simple_agent"

# Frozen at commit 164993d on 2026-11-17. Never edit these silently.
BASELINE: dict[str, int] = {
    "Integration code lines": 9,
    "Integration top-level statements": 5,
    "Constructor arguments passed": 0,
    "Minimal app files": 5,
    "Minimal app requirements": 4,
    "Command lines to the first local response": 6,
    "Command lines to deploy and verify": 13,
    "Shell variable assignments among them": 5,
    "Public LangGraphApp parameters (snapshot)": 17,
    "Package exported names (snapshot)": 31,
}
RECOMPUTED = tuple(BASELINE)[:8]
CHOICES = "Manual configuration choices"


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _example_stats() -> tuple[int, int, int]:
    source = (EXAMPLE / "function_app.py").read_text()
    tree = ast.parse(source)
    body = tree.body[1:] if tree.body and _is_docstring(tree.body[0]) else tree.body
    doc_end = tree.body[0].end_lineno if body is not tree.body and tree.body[0].end_lineno else 0
    lines = [
        line
        for number, line in enumerate(source.splitlines(), start=1)
        if number > doc_end and line.strip() and not line.strip().startswith("#")
    ]
    args = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LangGraphApp"
        ):
            args = len(node.args) + len(node.keywords)
    return len(lines), len(body), args


def _bash_logical_lines(text: str) -> list[str]:
    """Logical command lines of every ```bash fence in text."""
    result: list[str] = []
    for fence in re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL):
        pending = ""
        for raw in fence.splitlines():
            line = raw.strip()
            if not pending and (not line or line.startswith("#")):
                continue
            if line.endswith("\\"):
                pending += line[:-1].strip() + " "
                continue
            result.append(pending + line)
            pending = ""
    return result


def _tutorial_stats() -> tuple[int, int, int]:
    text = TUTORIAL.read_text()
    step1 = text.index("## Step 1")
    step5 = text.index("## Step 5")
    end = text.index("## How this tutorial stays honest")
    local: list[str] = []
    for line in _bash_logical_lines(text[step1:step5]):
        local.append(line)
        if line.startswith("curl"):
            break
    deploy = _bash_logical_lines(text[step5:end])
    assigns = [line for line in deploy if re.match(r"[A-Z_][A-Z0-9_]*=", line)]
    return len(local), len(deploy), len(assigns)


def _measure() -> dict[str, int]:
    lines, statements, args = _example_stats()
    files = [
        p
        for p in EXAMPLE.iterdir()
        if p.is_file() and p.name != "README.md" and not p.name.startswith(".")
    ]
    reqs = [
        line
        for line in (EXAMPLE / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    local, deploy, assigns = _tutorial_stats()
    measures = dict(
        zip(
            RECOMPUTED,
            (lines, statements, args, len(files), len(reqs), local, deploy, assigns),
            strict=True,
        )
    )
    measures[CHOICES] = sum(
        bool(re.match(r"[A-Z_][A-Z0-9_]*=(?!\$\()", line))
        for line in _bash_logical_lines(TUTORIAL.read_text())
    )
    return measures


def _page_rows() -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for line in PAGE.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0] in (*BASELINE, CHOICES):
            rows[cells[0]] = (cells[1], cells[2])
    return rows


def test_current_column_matches_recomputed_values() -> None:
    # Given: the page rows and values recomputed from the example and tutorial
    rows = _page_rows()
    # When: each recomputed measure is compared with its "Current" cell
    # Then: a stale row is named in the failure message
    for label, value in _measure().items():
        assert label in rows, f"missing row {label!r} in {PAGE.name}"
        assert rows[label][1] == str(value), (
            f"stale row {label!r}: page says {rows[label][1]}, recomputed {value}"
        )


def test_baseline_column_is_pinned() -> None:
    # Given: the frozen baseline in this test
    rows = _page_rows()
    # When: the page's Baseline column is read
    # Then: it equals the frozen values for all ten rows
    assert {label: rows[label][0] for label in BASELINE} == {
        label: str(value) for label, value in BASELINE.items()
    }
    assert rows[CHOICES][0] == "not recorded"


def test_tutorial_file_inventory_matches_the_example() -> None:
    # Given: Step 1's file list and the shipped example.
    text = TUTORIAL.read_text().split("## Step 1", 1)[1].split("## Step 2", 1)[0]
    # When: listed files are extracted (README is explicitly additional).
    listed = set(re.findall(r"^- `([^`]+)` —", text, re.MULTILINE))
    # Then: the list covers exactly the files counted in the baseline.
    assert listed == {
        p.name
        for p in EXAMPLE.iterdir()
        if p.is_file() and p.name != "README.md" and not p.name.startswith(".")
    }


def test_baseline_has_exactly_ten_keys() -> None:
    # Given/When/Then: the baseline may diverge from current, but its key set is fixed
    assert len(BASELINE) == 10
    assert len(RECOMPUTED) == 8


def _nav_sections() -> dict[str, list[str]]:
    """Minimal nav parse: top-level `  - Title:` keys and every `: target.md` leaf."""
    sections: dict[str, list[str]] = {}
    current = ""
    in_nav = False
    for line in (ROOT / "mkdocs.yml").read_text().splitlines():
        if line.startswith("nav:"):
            in_nav = True
            continue
        if in_nav and line and not line.startswith(" "):
            break
        if not in_nav:
            continue
        top = re.match(r"  - ([^:]+):\s*(\S*)$", line)
        if top:
            current = top.group(1)
            sections[current] = [top.group(2)] if top.group(2) else []
            continue
        leaf = re.match(r"\s+- [^:]+:\s*(\S+)$", line)
        if leaf and current:
            sections[current].append(leaf.group(1))
    return sections


def test_page_is_linked_once_in_maintainers_only() -> None:
    # Given: the nav sections of mkdocs.yml
    sections = _nav_sections()
    # When/Then: the page appears exactly once, in Maintainers
    hits = [(s, t) for s, targets in sections.items() for t in targets if t == PAGE.name]
    assert hits == [("Maintainers", "onboarding-baseline.md")]


def test_tutorial_documents_unhealthy_storage_message() -> None:
    # Given: the tutorial text
    text = TUTORIAL.read_text().lower()
    # When/Then: the Step 2 note names the storage setting and the unhealthy message
    step2 = text[text.index("## step 2") : text.index("## step 3")]
    assert "azurewebjobsstorage" in step2
    assert "unhealthy" in step2
