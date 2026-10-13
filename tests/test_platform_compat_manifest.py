from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import TypedDict

from azure.functions.decorators.http import HttpTrigger

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.platform._common import _UNSUPPORTED_THREAD_FILTER_FIELDS
from tests.conftest import FakeCompiledGraph

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "data" / "platform_compat_manifest.json"
COMPATIBILITY_PATH = ROOT / "COMPATIBILITY.md"
MATRIX_SECTIONS = {"Assistants", "Threads (CRUD)", "Runs", "State"}
STATUS_BY_MARKER = {
    "✅ Full": "supported",
    "⚠️ Partial": "partial",
    "⚠️ Silently ignored": "partial",
    "❌ Not exposed": "unexposed",
}


class RouteEntry(TypedDict):
    method: str
    endpoint: str


class OperationEntry(TypedDict):
    operation: str
    routes: list[RouteEntry]
    status: str


class UnsupportedEntry(TypedDict):
    field: str
    status: int


class CompatibilityManifest(TypedDict):
    operations: list[OperationEntry]
    unsupported_run_create_fields: list[UnsupportedEntry]
    unsupported_thread_filter_fields: list[UnsupportedEntry]


def _manifest() -> CompatibilityManifest:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return CompatibilityManifest(
        operations=raw["operations"],
        unsupported_run_create_fields=raw["unsupported_run_create_fields"],
        unsupported_thread_filter_fields=raw["unsupported_thread_filter_fields"],
    )


def _markdown_operations() -> dict[str, str]:
    operations: dict[str, str] = {}
    section: str | None = None
    for line in COMPATIBILITY_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("#### "):
            section = None
            continue
        if line.startswith("### "):
            section = line.removeprefix("### ")
            continue
        if section not in MATRIX_SECTIONS or not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        operation_cell, support = cells[0], cells[1]
        operations[operation_cell.strip("`")] = STATUS_BY_MARKER[support]
    return operations


def _markdown_unsupported_run_create_fields() -> set[str]:
    fields: set[str] = set()
    in_table = False
    for line in COMPATIBILITY_PATH.read_text(encoding="utf-8").splitlines():
        if line == "#### `RunCreate` field support":
            in_table = True
            continue
        if in_table and line.startswith("#### "):
            break
        match = re.match(r"\| `([^`]+)` .*\| ❌ `501`", line)
        if match is not None:
            fields.add(match.group(1))
    return fields


def _markdown_unsupported_thread_filter_fields() -> set[str]:
    for line in COMPATIBILITY_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("- **Unsupported request fields**"):
            fields = line.split(":", maxsplit=1)[1].split(" (`_common.py", maxsplit=1)[0]
            return set(re.findall(r"`([^`]+)`", fields))
    return set()


def test_manifest_matches_platform_matrix_rows() -> None:
    manifest_operations = {
        entry["operation"]: entry["status"] for entry in _manifest()["operations"]
    }

    assert manifest_operations == _markdown_operations()


def test_manifest_matches_documented_unsupported_run_create_fields() -> None:
    manifest_fields = {entry["field"] for entry in _manifest()["unsupported_run_create_fields"]}

    assert manifest_fields == _markdown_unsupported_run_create_fields()


def test_manifest_matches_thread_filter_sources() -> None:
    manifest_fields = {entry["field"] for entry in _manifest()["unsupported_thread_filter_fields"]}

    assert manifest_fields == _markdown_unsupported_thread_filter_fields()
    assert manifest_fields == _UNSUPPORTED_THREAD_FILTER_FIELDS


def test_registered_platform_routes_match_manifest() -> None:
    app = LangGraphApp(platform_compat=True)
    app.register(FakeCompiledGraph(), name="agent")
    function_app = app.function_app
    function_app.functions_bindings = {}
    registered = Counter(
        (method.value, f"/{binding.route}")
        for function in function_app.get_functions()
        for binding in function.get_bindings()
        if isinstance(binding, HttpTrigger)
        if (function_name := function.get_function_name()) is not None
        if function_name.startswith("aflg_platform_")
        if binding.methods is not None
        for method in binding.methods
    )
    expected = Counter(
        (route["method"], route["endpoint"])
        for entry in _manifest()["operations"]
        for route in entry["routes"]
    )

    assert registered == expected
