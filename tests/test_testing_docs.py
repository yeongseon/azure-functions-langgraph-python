"""Keep ``docs/testing.md`` in step with the fixtures and test levels it describes (#853)."""

from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "testing.md").read_text()
CONFTEST = ast.parse((ROOT / "tests" / "conftest.py").read_text())

SNIPPET_CLASSES = ("FakeCompiledGraph", "FakeInvokeOnlyGraph", "FakeFailingGraph")


def _conftest_class(name: str) -> ast.ClassDef:
    for node in CONFTEST.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"tests/conftest.py has no class {name}")


def _without_docstring(node: ast.ClassDef) -> ast.ClassDef:
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:]
    return node


def _documented_class(name: str) -> ast.ClassDef:
    section = DOC.split(f"### {name}\n", 1)[1]
    match = re.search(r"```python\n(.*?)```", section, re.DOTALL)
    assert match, f"docs/testing.md has no snippet under {name}"
    tree = ast.parse(match.group(1))
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.ClassDef)
    return tree.body[0]


@pytest.mark.parametrize("name", SNIPPET_CLASSES)
def test_fake_graph_snippet_matches_conftest(name: str) -> None:
    documented = ast.dump(_documented_class(name))
    actual = ast.dump(_without_docstring(_conftest_class(name)))
    assert documented == actual


def test_every_conftest_fake_is_named_in_the_guide() -> None:
    fakes = [
        node.name
        for node in CONFTEST.body
        if isinstance(node, ast.ClassDef) and node.name.startswith("Fake")
    ]
    assert fakes
    for name in fakes:
        assert name in DOC, f"{name} is defined in tests/conftest.py but not in docs/testing.md"


def test_documented_fixtures_exist() -> None:
    fixtures = {
        node.name
        for node in CONFTEST.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("fake_")
    }
    mentioned = set(re.findall(r"`(fake_[a-z_]+)`", DOC))
    assert mentioned == fixtures


def test_documented_make_targets_and_paths_exist() -> None:
    makefile = (ROOT / "Makefile").read_text()
    for target in re.findall(r"`make ([a-z][a-z0-9-]*)", DOC):
        assert re.search(rf"^{re.escape(target)}:", makefile, re.MULTILINE), target
    for path in (
        "tools/benchmark_table_store.py",
        "tests/host_smoke",
        "tests/e2e",
        "tests/integration",
    ):
        assert (ROOT / path).exists(), path
        assert path in DOC
    assert "azure-e2e" in (ROOT / ".github" / "workflows" / "publish-pypi.yml").read_text()


def test_guide_does_not_claim_mocks_certify_azure() -> None:
    text = " ".join(DOC.split())
    assert "never certifies real Azure behavior" in text
    assert "do not quote those numbers as Azure measurements" in text
