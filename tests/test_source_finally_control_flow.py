from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src"


class _FinallyControlFlowVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []

    def visit_Return(self, node: ast.Return) -> None:
        self.violations.append((node.lineno, "return"))

    def visit_Break(self, node: ast.Break) -> None:
        self.violations.append((node.lineno, "break"))

    def visit_Continue(self, node: ast.Continue) -> None:
        self.violations.append((node.lineno, "continue"))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        pass

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        pass

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        pass

    def visit_Lambda(self, node: ast.Lambda) -> None:
        pass


@pytest.mark.parametrize(
    "source_path", sorted(SRC.rglob("*.py")), ids=lambda path: str(path.relative_to(SRC))
)
def test_source_has_no_control_flow_leaving_finally(source_path: Path) -> None:
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)):
            continue
        visitor = _FinallyControlFlowVisitor()
        for statement in node.finalbody:
            visitor.visit(statement)
        violations.extend(visitor.violations)

    assert violations == [], f"control flow leaves finally in {source_path}: {violations}"
