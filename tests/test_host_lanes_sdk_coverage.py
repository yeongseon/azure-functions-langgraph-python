"""Every exposed Platform operation has a scenario in the host-lane SDK module (#883).

``tests/host_lanes/test_platform_sdk.py`` names, in ``COVERED_OPERATIONS``, the test that exercises
each supported or partial operation of ``tests/data/platform_compat_manifest.json`` through the real
``langgraph-sdk`` client against a real local Functions host, and lists a value to send for each
unsupported ``RunCreate`` field. This module reads that file as source and never imports it: it
needs a langgraph-sdk newer than the 0.2.2 floor, and this check has to run in every lane.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SDK_MODULE = ROOT / "tests" / "host_lanes" / "test_platform_sdk.py"
MANIFEST = json.loads(
    (ROOT / "tests" / "data" / "platform_compat_manifest.json").read_text("utf-8")
)
EXPOSED = {"supported", "partial"}


def _module_literal(name: str) -> Any:
    for node in ast.parse(SDK_MODULE.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            assert node.value is not None
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{SDK_MODULE.name} defines no {name}")


def _exposed_operations() -> set[str]:
    return {entry["operation"] for entry in MANIFEST["operations"] if entry["status"] in EXPOSED}


def test_the_manifest_uses_only_the_statuses_this_check_knows() -> None:
    assert {entry["status"] for entry in MANIFEST["operations"]} <= EXPOSED | {"unexposed"}


def test_every_exposed_operation_has_a_scenario() -> None:
    uncovered = _exposed_operations() - set(_module_literal("COVERED_OPERATIONS"))

    assert uncovered == set(), (
        f"add a scenario for these operations to {SDK_MODULE.name} and name it in "
        f"COVERED_OPERATIONS: {sorted(uncovered)}"
    )


def test_no_scenario_is_claimed_for_an_operation_the_manifest_does_not_expose() -> None:
    stale = set(_module_literal("COVERED_OPERATIONS")) - _exposed_operations()

    assert stale == set(), (
        f"COVERED_OPERATIONS names operations that are not exposed: {sorted(stale)}"
    )


def test_every_named_scenario_is_a_test_of_the_module() -> None:
    tests = {
        node.name
        for node in ast.parse(SDK_MODULE.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }
    named = set(_module_literal("COVERED_OPERATIONS").values())

    assert named <= tests, (
        f"COVERED_OPERATIONS names tests that do not exist: {sorted(named - tests)}"
    )


def test_every_unsupported_run_field_has_a_value_to_send() -> None:
    fields = {entry["field"] for entry in MANIFEST["unsupported_run_create_fields"]}

    assert fields == set(_module_literal("RUN_FIELD_VALUES"))
