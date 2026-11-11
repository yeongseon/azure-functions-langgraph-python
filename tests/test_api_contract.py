"""Tests for the public API and Native HTTP contract gate (issue #879)."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any
import zipfile

from pydantic import BaseModel
import pytest

_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "api_contract.py"
_spec = importlib.util.spec_from_file_location("api_contract_tool", _TOOL_PATH)
assert _spec and _spec.loader
tool: ModuleType = importlib.util.module_from_spec(_spec)
sys.modules["api_contract_tool"] = tool
_spec.loader.exec_module(tool)

PKG = "azure_functions_langgraph"


def make_tree(root: Path, files: dict[str, str]) -> Path:
    """Write a tiny fake package tree; keys are paths relative to the package dir."""
    for relative, content in files.items():
        path = root / PKG / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


BASE_FILES = {
    "__init__.py": (
        "__all__ = ['LangGraphApp', 'helper', '__version__']\n"
        "__version__ = '1'\n"
        "from .app import LangGraphApp\n"
        "from .contracts import helper\n"
    ),
    "app.py": (
        "__all__ = ['LangGraphApp']\n"
        "class LangGraphApp:\n"
        "    timeout: int = 5\n"
        "    def register(self, graph, name, *, stream: bool = True) -> None: ...\n"
    ),
    "contracts.py": "def helper(a: int, b: int = 1) -> int: ...\n",
    "platform/__init__.py": (
        "__all__ = ['PlatformThing']\nclass PlatformThing:\n    def run(self, x): ...\n"
    ),
    "scratch.py": "__all__ = ['Scratch']\nclass Scratch: ...\n",
}


def snapshot(tmp_path: Path, name: str, overrides: Mapping[str, str | None]) -> dict[str, Any]:
    files = {**BASE_FILES}
    for key, value in overrides.items():
        if value is None:
            files.pop(key, None)
        else:
            files[key] = value
    root = make_tree(tmp_path / name, files)
    result: dict[str, Any] = tool.ApiExtractor(root).snapshot()
    return result


def evaluate(
    changes: list[Any], entries: list[dict[str, Any]] | None = None, baseline: str = "1"
) -> Any:
    return tool.evaluate(changes, {"changes": entries or [], "baseline_reviews": []}, baseline)


def entry(change_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": change_id,
        "baseline": "1",
        "classification": "breaking",
        "refs": ["#879"],
        "rationale": "intentional",
        "migration": "pass the new argument",
    }
    return {**base, **overrides}


# --------------------------------------------------------------------- python API


def test_removed_exported_symbol_is_caught(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})
    new = snapshot(
        tmp_path,
        "new",
        {
            "__init__.py": "__all__ = ['LangGraphApp']\nfrom .app import LangGraphApp\n",
            "contracts.py": None,
        },
    )
    report = evaluate(tool.diff_api(old, new))
    ids = {c.id for c in report.blocking}
    assert f"removed:{PKG}:helper" in ids
    assert not report.ok


def test_removed_symbol_passes_only_with_matching_classification(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})
    new = snapshot(
        tmp_path,
        "new",
        {"__init__.py": "__all__ = ['LangGraphApp']\nfrom .app import LangGraphApp\n"},
    )
    changes = tool.diff_api(old, new)
    target = f"removed:{PKG}:helper"
    assert evaluate(changes, [entry(target)]).ok
    # Entries are bound to the baseline version they were reviewed against.
    assert not evaluate(changes, [entry(target, baseline="0")]).ok
    # Rationale/migration/refs are mandatory, so an empty record cannot approve.
    bad = evaluate(changes, [entry(target, refs=[], migration="")])
    assert not bad.ok
    assert any("refs" in p for p in bad.problems)
    assert any("migration" in p for p in bad.problems)


def test_additive_change_passes_with_a_note(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})
    new = snapshot(
        tmp_path,
        "new",
        {
            "contracts.py": "def helper(a: int, b: int = 1, c: int = 2) -> int: ...\n"
            "def extra() -> None: ...\n",
            "__init__.py": BASE_FILES["__init__.py"].replace("'helper'", "'helper', 'extra'")
            + "from .contracts import extra\n",
        },
    )
    report = evaluate(tool.diff_api(old, new))
    assert report.ok
    added = {c.id for c in report.additive}
    assert f"added:{PKG}:extra" in added
    assert f"added:{PKG}.contracts:helper(c)" not in added  # contracts is not a public export path
    assert any(i.endswith("helper(c)") for i in added)
    assert "ADDITIVE" in tool.render_text(report)


def test_experimental_change_is_reported_but_not_blocking(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})
    new = snapshot(
        tmp_path,
        "new",
        {"platform/__init__.py": "__all__ = ['PlatformThing']\nclass PlatformThing: ...\n"},
    )
    report = evaluate(tool.diff_api(old, new))
    assert report.ok
    assert [(c.tier, c.id) for c in report.reported] == [
        ("experimental", f"removed:{PKG}.platform:PlatformThing.run")
    ]
    text = tool.render_text(report)
    assert "[experimental]" in text and "not blocking" in text
    # Removing an experimental symbol outright is also non-blocking, never "stable".
    gone = snapshot(tmp_path, "gone", {"platform/__init__.py": None})
    gone_report = evaluate(tool.diff_api(old, gone))
    assert gone_report.ok and gone_report.reported
    assert all(c.tier == "experimental" for c in gone_report.reported)


def test_undeclared_tier_is_never_certified_stable(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})
    new = snapshot(tmp_path, "new", {"scratch.py": None})
    report = evaluate(tool.diff_api(old, new))
    assert report.ok
    assert {c.tier for c in report.reported} == {"unclassified"}
    assert tool.tier_for(f"{PKG}.scratch") == "unclassified"
    assert tool.tier_for(f"{PKG}.app") == "stable"
    assert tool.tier_for(f"{PKG}.locks.base") == "beta"
    assert tool.tier_for(f"{PKG}.durable._state") == "experimental"
    assert tool.tier_for(f"{PKG}.streaming_authorization") == "experimental"


def test_signature_changes_are_classified(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})

    def changed(source: str) -> set[str]:
        new = snapshot(tmp_path, "new", {"contracts.py": source})
        return {c.id for c in evaluate(tool.diff_api(old, new)).blocking}

    prefix = f"{PKG}:helper"
    assert f"changed:{prefix}.returns" in changed("def helper(a: int, b: int = 1) -> str: ...\n")
    assert f"removed:{prefix}(b)" in changed("def helper(a: int) -> int: ...\n")
    assert f"added:{prefix}(c)" in changed(
        "def helper(a: int, b: int = 1, *, c: int) -> int: ...\n"
    )
    assert f"changed:{prefix}(b).default" in changed("def helper(a: int, b: int = 2) -> int: ...\n")
    assert f"changed:{prefix}(a).annotation" in changed(
        "def helper(a: str, b: int = 1) -> int: ...\n"
    )
    assert f"changed:{prefix}.order" in changed("def helper(b: int, a: int = 1) -> int: ...\n")
    assert f"added:{prefix}(z).position" in changed(
        "def helper(z: int = 0, a: int = 0, b: int = 1) -> int: ...\n"
    )
    assert f"changed:{prefix}.async" in changed(
        "async def helper(a: int, b: int = 1) -> int: ...\n"
    )
    # An optional trailing parameter and a relaxed default are additive.
    assert changed("def helper(a: int, b: int = 1, c: int = 0) -> int: ...\n") == set()
    assert changed("def helper(a: int = 0, b: int = 1) -> int: ...\n") == set()


def test_class_member_and_field_changes(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})

    def blocking(source: str) -> set[str]:
        new = snapshot(tmp_path, "new", {"app.py": source})
        return {c.id for c in evaluate(tool.diff_api(old, new)).blocking}

    cls = f"{PKG}.app:LangGraphApp"
    head = "__all__ = ['LangGraphApp']\n"
    assert f"removed:{cls}.timeout" in blocking(
        head + "class LangGraphApp:\n"
        "    def register(self, graph, name, *, stream: bool = True) -> None: ...\n"
    )
    assert f"removed:{cls}.register" in blocking(
        head + "class LangGraphApp:\n    timeout: int = 5\n"
    )
    assert f"added:{cls}.mandatory" in blocking(
        head + "class LangGraphApp:\n    timeout: int = 5\n    mandatory: int\n"
        "    def register(self, graph, name, *, stream: bool = True) -> None: ...\n"
    )
    assert f"changed:{cls}.timeout.default" in blocking(
        head + "class LangGraphApp:\n    timeout: int = 9\n"
        "    def register(self, graph, name, *, stream: bool = True) -> None: ...\n"
    )
    assert f"changed:{cls}.timeout.kind" in blocking(
        head + "class LangGraphApp:\n    def timeout(self): ...\n"
        "    def register(self, graph, name, *, stream: bool = True) -> None: ...\n"
    )


def test_class_base_and_protocol_changes(tmp_path: Path) -> None:
    proto = (
        "from typing import Protocol\n__all__ = ['P']\nclass P(Protocol):\n    def a(self): ...\n"
    )
    files = {"protocols.py": proto}
    root_old = snapshot(tmp_path, "old", files)
    root_new = snapshot(tmp_path, "new", {"protocols.py": proto + "    def b(self): ...\n"})
    ids = {c.id: c for c in tool.diff_api(root_old, root_new)}
    added = ids[f"added:{PKG}.protocols:P.b"]
    assert added.breaking and added.tier == "stable"
    # Class bases and kinds.
    plain = {"protocols.py": "__all__ = ['P']\nclass P:\n    def a(self): ...\n"}
    kinds = {c.id for c in tool.diff_api(root_old, snapshot(tmp_path, "k", plain))}
    assert f"changed:{PKG}.protocols:P.class_kind" in kinds
    assert f"removed:{PKG}.protocols:P.base(Protocol)" in kinds
    # A symbol changing kind (class -> function) is a breaking change.
    func = {"protocols.py": "__all__ = ['P']\ndef P(): ...\n"}
    assert f"changed:{PKG}.protocols:P.kind" in {
        c.id for c in tool.diff_api(root_old, snapshot(tmp_path, "f", func))
    }
    # Module-level variables (type aliases) compare by value.
    var_old = snapshot(tmp_path, "vo", {"protocols.py": "__all__ = ['T']\nT = Literal['a']\n"})
    var_new = snapshot(tmp_path, "vn", {"protocols.py": "__all__ = ['T']\nT = Literal['a', 'b']\n"})
    assert f"changed:{PKG}.protocols:T.value" in {c.id for c in tool.diff_api(var_old, var_new)}


def test_removed_and_added_public_modules(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})
    new = snapshot(tmp_path, "new", {"app.py": None, "fresh.py": "__all__ = []\n"})
    changes = {c.id: c for c in tool.diff_api(old, new)}
    assert changes[f"removed:module:{PKG}.app"].breaking
    assert not changes[f"added:module:{PKG}.fresh"].breaking
    assert changes[f"added:module:{PKG}.fresh"].tier == "unclassified"


def test_optional_extras_are_never_imported(tmp_path: Path) -> None:
    files = {
        "__init__.py": (
            "__all__ = ['OTel']\n"
            "_LAZY_IMPORTS = {'OTel': ('azure_functions_langgraph.otel', 'OTel')}\n"
        ),
        "otel.py": (
            "import opentelemetry_that_is_not_installed  # noqa\n"
            "import azure.storage.blob_that_is_not_installed  # noqa\n"
            "class OTel:\n    def run(self) -> None: ...\n"
        ),
    }
    root = make_tree(tmp_path / "extras", files)
    snap = tool.ApiExtractor(root).snapshot()
    symbol = snap["symbols"][f"{PKG}:OTel"]
    assert symbol["kind"] == "class" and symbol["defined_in"] == f"{PKG}.otel"
    assert "opentelemetry_that_is_not_installed" not in sys.modules


def test_resolution_follows_imports_and_flags_unresolved(tmp_path: Path) -> None:
    files = {
        "__init__.py": "__all__ = ['A', 'Missing', 'Ext', 'Cyc']\n"
        "from .impl import A\nfrom .impl import Missing\nfrom os import path as Ext\n"
        "from .cyc import Cyc\n",
        "impl.py": "class A: ...\n",
        "cyc.py": "from . import Cyc\n",
    }
    snap = tool.ApiExtractor(make_tree(tmp_path / "res", files)).snapshot()
    kinds = {k.split(":")[1]: v["kind"] for k, v in snap["symbols"].items()}
    assert kinds == {"A": "class", "Missing": "unresolved", "Ext": "external", "Cyc": "unresolved"}


def test_extractor_requires_the_package(tmp_path: Path) -> None:
    with pytest.raises(tool.ContractError):
        tool.ApiExtractor(tmp_path)


def test_stale_classification_entries_are_reported(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})
    report = evaluate(tool.diff_api(old, old), [entry(f"removed:{PKG}:helper")])
    assert report.ok
    assert report.stale == [f"removed:{PKG}:helper"]
    assert "STALE" in tool.render_text(report)


# ------------------------------------------------------------------------ HTTP


def committed(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = tool.load_json(tool.CONTRACT_DIR / name)
    return loaded


def blocking_ids(current: dict[str, Any]) -> set[str]:
    changes = tool.diff_http(committed("native_http_baseline.json"), current)
    classifications = tool.load_json(tool.CLASSIFICATIONS)
    report = tool.evaluate(changes, classifications, "0.13.0")
    return {c.id for c in report.blocking}


def test_live_native_http_contract_matches_the_baseline() -> None:
    assert blocking_ids(tool.collect_http_contract()) == set()


def test_changed_native_http_response_field_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    from azure_functions_langgraph import _handlers

    class RenamedState(BaseModel):
        state: dict[str, Any]
        next: list[str]
        metadata: dict[str, Any] | None = None

        def __init__(self, *, values: dict[str, Any], **rest: Any) -> None:
            super().__init__(state=values, **rest)

    monkeypatch.setattr(_handlers, "StateResponse", RenamedState)
    ids = blocking_ids(tool.collect_http_contract())
    assert "removed:http:state_ok.body.json.values" in ids


def test_changed_health_response_field_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    from azure_functions_langgraph import app as app_module

    class Renamed(BaseModel):
        state: str = "ok"
        graphs: list[Any] = []

    monkeypatch.setattr(app_module, "HealthResponse", lambda **kw: Renamed(graphs=kw["graphs"]))
    ids = blocking_ids(tool.collect_http_contract())
    assert "removed:http:health_details.body.json.status" in ids


def test_additive_http_field_passes_with_a_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from azure_functions_langgraph import app as app_module

    class Extended(BaseModel):
        status: str = "ok"
        graphs: list[Any] = []
        extra: str = "new"

    monkeypatch.setattr(app_module, "HealthResponse", lambda **kw: Extended(graphs=kw["graphs"]))
    changes = tool.diff_http(committed("native_http_baseline.json"), tool.collect_http_contract())
    report = tool.evaluate(changes, tool.load_json(tool.CLASSIFICATIONS), "0.13.0")
    assert report.ok
    assert "added:http:health_details.body.json.extra" in {c.id for c in report.additive}


def test_http_diff_covers_routes_status_sse_and_variants() -> None:
    base = committed("native_http_baseline.json")
    current = copy.deepcopy(base)
    current["routes"] = [r for r in current["routes"] if r["function"] != "aflg_agent_state"]
    current["routes"].append(
        {"function": "aflg_new", "route": "new", "methods": ["GET"], "auth_level": "FUNCTION"}
    )
    for route in current["routes"]:
        if route["function"] == "aflg_health":
            route["auth_level"] = "FUNCTION"
    current["exchanges"]["invoke_ok"]["status"] = 201
    current["exchanges"]["stream_ok"]["body"]["sse"] = [
        e for e in current["exchanges"]["stream_ok"]["body"]["sse"] if e["event"] != "end"
    ] + [{"event": "extra", "data": {}}]
    current["exchanges"]["health"]["body"]["json"]["status"] = "string"
    del current["exchanges"]["state_ok"]
    current["exchanges"]["invoke_graph_failure"]["body"]["json"]["detail"] = ["string"]
    ids = {c.id: c for c in tool.diff_http(base, current)}
    assert ids["removed:http:route:aflg_agent_state"].breaking
    assert not ids["added:http:route:aflg_new"].breaking
    assert ids["changed:http:route:aflg_health.auth_level"].breaking
    assert ids["changed:http:invoke_ok.status"].breaking
    assert ids["removed:http:stream_ok.body.event(end)"].breaking
    assert not ids["added:http:stream_ok.body.event(extra)"].breaking
    assert ids["changed:http:health.body.json.status"].breaking
    assert ids["removed:http:state_ok"].breaking
    assert any(i.startswith("changed:http:invoke_graph_failure") for i in ids)


def test_shape_of_reduces_values_to_structure() -> None:
    assert tool.shape_of({"b": [1, 2], "status": "ok", "n": None, "f": 1.5, "t": True}) == {
        "b": ["integer"],
        "f": "number",
        "n": "null",
        "status": "string=ok",
        "t": "boolean",
    }
    assert tool.shape_of([{"a": 1}, {"a": "x"}]) == [{"a": "integer"}, {"a": "string"}]


def test_sse_event_order_change_is_breaking() -> None:
    old = [{"event": "data", "data": {}}, {"event": "end", "data": {}}]
    new = [{"event": "end", "data": {}}, {"event": "data", "data": {}}]
    out: list[Any] = []
    tool._diff_sse("p", old, new, out)
    assert any(c.id == "changed:p.order" and c.breaking for c in out)


# ------------------------------------------------ baselines, provenance, policy


def test_committed_baselines_record_their_provenance() -> None:
    for name in ("api_baseline.json", "native_http_baseline.json"):
        data = committed(name)
        provenance = data["provenance"]
        assert data["version"] == "0.13.0"
        assert provenance["ref"] == "v0.13.0"
        assert len(provenance["commit"]) == 40
        assert len(provenance["published_wheel_sha256"]) == 64
        assert provenance["kind"] == "tag-source"
    api = committed("api_baseline.json")
    assert f"{PKG}:LangGraphApp" in api["symbols"]
    assert api["modules"][PKG]["all"] is not None
    assert "__version__" not in api["modules"][PKG]["all"]
    assert not [k for k, v in api["symbols"].items() if v["kind"] == "unresolved"]


def test_committed_baselines_are_reviewed_together_with_a_classification_record() -> None:
    """A snapshot update must ship a review entry for the new baseline bytes."""
    classifications = tool.load_json(tool.CLASSIFICATIONS)
    assert tool.validate_classifications(classifications) == []
    assert tool.unreviewed_baselines(classifications) == []


def test_updating_a_baseline_without_a_review_entry_fails(tmp_path: Path) -> None:
    classifications = tool.load_json(tool.CLASSIFICATIONS)
    tampered = tmp_path / "api_baseline.json"
    data = committed("api_baseline.json")
    del data["symbols"][f"{PKG}:LangGraphApp"]
    tampered.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    untouched = tool.CONTRACT_DIR / "native_http_baseline.json"
    assert tool.unreviewed_baselines(classifications, (tampered, untouched)) == [
        "api_baseline.json"
    ]
    # A review entry without an issue/PR reference does not count either.
    classifications["baseline_reviews"].append(
        {"file": "api_baseline.json", "sha256": tool.file_sha256(tampered), "refs": []}
    )
    assert tool.unreviewed_baselines(classifications, (tampered, untouched)) == [
        "api_baseline.json"
    ]
    classifications["baseline_reviews"][-1]["refs"] = ["#879"]
    assert tool.unreviewed_baselines(classifications, (tampered, untouched)) == []


def test_validate_classifications_rejects_untraceable_entries() -> None:
    problems = tool.validate_classifications(
        {
            "changes": [{"id": "", "classification": "maybe", "refs": ["see slack"]}],
            "baseline_reviews": [{"file": "x", "refs": []}],
        }
    )
    joined = "\n".join(problems)
    assert "classification must be one of" in joined
    assert "refs must list" in joined
    assert "baseline version" in joined
    assert "rationale" in joined
    assert "review of x" in joined
    assert (
        tool.validate_classifications(
            {"changes": [entry("changed:x", classification="compatible", migration="")]}
        )
        == []
    )
    assert (
        tool.validate_classifications(
            {"changes": [entry("changed:x", classification="security-breaking")]}
        )
        == []
    )


def test_load_json_rejects_wrong_schema(tmp_path: Path) -> None:
    bad = tmp_path / "x.json"
    bad.write_text('{"schema_version": 99}', encoding="utf-8")
    with pytest.raises(tool.ContractError):
        tool.load_json(bad)
    with pytest.raises(tool.ContractError):
        tool.load_json(tmp_path / "missing.json")


def test_repository_source_passes_the_gate() -> None:
    report = tool.run_check(None)
    assert report.ok, tool.render_text(report)
    assert report.baseline_version == "0.13.0"


# ------------------------------------------------------------------ wheel + CLI


def make_wheel(path: Path, extra: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in {
            f"{PKG}/__init__.py": "__all__ = []\n",
            f"{PKG}-1.dist-info/METADATA": "Name: x\n",
            **(extra or {}),
        }.items():
            archive.writestr(name, content)
    return path


def test_wheel_extraction_keeps_only_the_package(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "a.whl", {f"{PKG}/sub/": ""})
    root = tool.extract_wheel(wheel, tmp_path / "out")
    assert (root / PKG / "__init__.py").is_file()
    assert not (root / f"{PKG}-1.dist-info").exists()


def test_wheel_extraction_rejects_path_escape(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "evil.whl", {"../escape.py": "x = 1\n"})
    with pytest.raises(tool.ContractError):
        tool.extract_wheel(wheel, tmp_path / "out")
    assert not (tmp_path / "escape.py").exists()


def point_tool_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutate: bool) -> None:
    directory = tmp_path / "contracts"
    directory.mkdir()
    api = committed("api_baseline.json")
    if mutate:
        api["symbols"][f"{PKG}:Ghost"] = {
            **api["symbols"][f"{PKG}:HealthResponse"],
            "defined_in": f"{PKG}.contracts",
        }
    paths = {}
    for name, data in (
        ("api_baseline.json", api),
        ("native_http_baseline.json", committed("native_http_baseline.json")),
    ):
        path = directory / name
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths[name] = path
    classifications = {
        "schema_version": 1,
        "changes": [],
        "baseline_reviews": [
            {"file": n, "sha256": tool.file_sha256(p), "refs": ["#879"]} for n, p in paths.items()
        ],
    }
    cls_path = directory / "classifications.json"
    cls_path.write_text(json.dumps(classifications), encoding="utf-8")
    monkeypatch.setattr(tool, "API_BASELINE", paths["api_baseline.json"])
    monkeypatch.setattr(tool, "HTTP_BASELINE", paths["native_http_baseline.json"])
    monkeypatch.setattr(tool, "BASELINE_FILES", tuple(paths.values()))
    monkeypatch.setattr(tool, "CLASSIFICATIONS", cls_path)


def test_cli_check_passes_and_writes_a_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    point_tool_at(monkeypatch, tmp_path, mutate=False)
    summary = tmp_path / "summary.md"
    assert tool.main(["check", "--summary", str(summary)]) == 0
    assert "RESULT: PASS" in capsys.readouterr().out
    assert "API contract gate (baseline 0.13.0): pass" in summary.read_text(encoding="utf-8")


def test_cli_check_fails_on_an_unclassified_break(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    point_tool_at(monkeypatch, tmp_path, mutate=True)
    summary = tmp_path / "summary.md"
    assert tool.main(["check", "--summary", str(summary)]) == 1
    out = capsys.readouterr().out
    assert f"removed:{PKG}:Ghost" in out and "RESULT: FAIL" in out
    assert "**FAIL**" in summary.read_text(encoding="utf-8")


def test_cli_check_against_a_wheel_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    point_tool_at(monkeypatch, tmp_path, mutate=False)
    wheel = make_wheel(tmp_path / "empty.whl")
    # An empty package removes every baseline symbol, so the gate must fail.
    monkeypatch.setattr(
        tool, "collect_http_contract", lambda root=None: committed("native_http_baseline.json")
    )
    assert tool.main(["check", "--wheel", str(wheel)]) == 1
    assert "removed:module:" in capsys.readouterr().out


def test_cli_reports_usage_and_input_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert tool.main(["check", "--wheel", "a", "--source", "b"]) == 2
    assert tool.main(["snapshot", "--version", "1", "--ref", "v1"]) == 2
    monkeypatch.setattr(tool, "API_BASELINE", tmp_path / "missing.json")
    assert tool.main(["check"]) == 2
    assert "error:" in capsys.readouterr().err


def test_snapshot_writes_baselines_that_still_need_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_tree(tmp_path / "src", BASE_FILES)
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        tool, "collect_http_contract", lambda root=None: {"routes": [], "exchanges": {}}
    )
    real = tool.write_baselines
    monkeypatch.setattr(tool, "write_baselines", lambda r, v, p: real(r, v, p, directory=out_dir))
    wheel = make_wheel(tmp_path / "w.whl", {f"{PKG}/app.py": BASE_FILES["app.py"]})
    code = tool.main(
        [
            "snapshot",
            "--source",
            str(root),
            "--version",
            "9.9.9",
            "--ref",
            "v9.9.9",
            "--commit",
            "a" * 40,
            "--published-sha256",
            "b" * 64,
            "--note",
            "test",
        ]
    )
    assert code == 0
    written = json.loads((out_dir / "api_baseline.json").read_text(encoding="utf-8"))
    assert written["version"] == "9.9.9"
    assert written["provenance"] == {
        "kind": "tag-source",
        "ref": "v9.9.9",
        "commit": "a" * 40,
        "published_wheel_sha256": "b" * 64,
        "note": "test",
    }
    assert "baseline_reviews" in capsys.readouterr().out
    assert tool.main(["snapshot", "--wheel", str(wheel), "--version", "9", "--ref", "v9"]) == 0
    assert (
        "wheel_sha256"
        in json.loads((out_dir / "api_baseline.json").read_text("utf-8"))["provenance"]
    )


def test_markdown_report_lists_every_section(tmp_path: Path) -> None:
    old = snapshot(tmp_path, "old", {})
    new = snapshot(
        tmp_path,
        "new",
        {
            "contracts.py": None,
            "platform/__init__.py": None,
            "scratch.py": None,
            "__init__.py": "__all__ = ['LangGraphApp']\nfrom .app import LangGraphApp\n",
        },
    )
    report = evaluate(tool.diff_api(old, new), [entry(f"removed:{PKG}:helper")])
    text = tool.render_markdown(report)
    for heading in (
        "### Blocking",
        "### Classified breaking",
        "### Non-blocking tiers",
        "### Additive",
    ):
        assert heading in text
    assert "_none_" in text


# --------------------------------------------------------------------------- CI


def test_ci_workflow_runs_the_gate_without_widening_permissions() -> None:
    workflow = (Path(__file__).resolve().parent.parent / ".github/workflows/ci-test.yml").read_text(
        encoding="utf-8"
    )
    assert "python tools/api_contract.py check" in workflow
    assert "python tools/api_contract.py check --wheel" in workflow
    assert workflow.count("permissions:") == 1
    assert "permissions:\n  contents: read" in workflow
    assert "secrets." not in workflow.replace("secrets.CODECOV_TOKEN", "")


# ------------------------------------------------------- published-wheel verify

_SHA = "a" * 64


def _registry(digest: str = _SHA, kinds: tuple[str, ...] = ("bdist_wheel", "sdist")) -> bytes:
    urls = [
        {
            "packagetype": kind,
            "url": f"https://files.pythonhosted.org/packages/x/pkg-0.13.0-{kind}.whl",
            "digests": {"sha256": digest},
        }
        for kind in kinds
    ]
    return json.dumps({"urls": urls}).encode()


def test_registry_wheel_selects_the_single_wheel_digest() -> None:
    url, digest = tool.registry_wheel("0.13.0", lambda *_: _registry())
    assert url.startswith("https://files.pythonhosted.org/") and digest == _SHA


@pytest.mark.parametrize(
    "payload",
    [_registry(kinds=("sdist",)), _registry(digest="zz"), b"not json"],
)
def test_registry_wheel_rejects_unusable_metadata(payload: bytes) -> None:
    with pytest.raises(tool.ContractError):
        tool.registry_wheel("0.13.0", lambda *_: payload)


def test_download_retries_then_verifies_digest(tmp_path: Path) -> None:
    import hashlib

    data = b"wheel-bytes"
    digest = hashlib.sha256(data).hexdigest()
    calls: list[str] = []

    def fetch(url: str, timeout: float, limit: int) -> bytes:
        calls.append(url)
        if url.endswith("json"):
            return _registry(digest)
        if len([c for c in calls if not c.endswith("json")]) < 3:
            raise OSError("flaky")
        return data

    path, got = tool.download_published_wheel("0.13.0", tmp_path, fetch, sleep=lambda _s: None)
    assert got == digest and path.read_bytes() == data
    assert len(calls) == 4


def test_download_fails_closed_on_digest_mismatch_and_unreachable(tmp_path: Path) -> None:
    def tampered(url: str, timeout: float, limit: int) -> bytes:
        return _registry() if url.endswith("json") else b"other"

    with pytest.raises(tool.ContractError, match="!= registry digest"):
        tool.download_published_wheel("0.13.0", tmp_path, tampered, sleep=lambda _s: None)

    def down(url: str, timeout: float, limit: int) -> bytes:
        if url.endswith("json"):
            return _registry()
        raise OSError("unreachable")

    with pytest.raises(tool.ContractError, match="unreachable after 2 attempts"):
        tool.download_published_wheel("0.13.0", tmp_path, down, attempts=2, sleep=lambda _s: None)


def test_fetch_refuses_non_pypi_hosts() -> None:
    with pytest.raises(tool.ContractError, match="only https PyPI hosts"):
        tool._http_get("https://example.com/x.whl", 1, 10)
    with pytest.raises(tool.ContractError, match="only https PyPI hosts"):
        tool._http_get("http://pypi.org/x", 1, 10)


def test_verify_baseline_reports_match_and_differences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    header = {"schema_version": 1, "package": "p", "version": "1"}
    api = {**header, "provenance": {"published_wheel_sha256": _SHA}, "modules": {}, "symbols": {}}
    http = {**header, "provenance": {}, "routes": {}, "exchanges": {}}
    api_path, http_path = tmp_path / "a.json", tmp_path / "h.json"
    api_path.write_text(json.dumps(api))
    http_path.write_text(json.dumps(http))
    monkeypatch.setattr(tool, "API_BASELINE", api_path)
    monkeypatch.setattr(tool, "HTTP_BASELINE", http_path)
    same: tuple[dict[str, Any], dict[str, Any]] = (
        {"modules": {}, "symbols": {}},
        {"routes": {}, "exchanges": {}},
    )
    monkeypatch.setattr(tool, "current_snapshots", lambda _r: same)
    assert tool.verify_baseline_against_wheel(tmp_path, _SHA, _SHA) == []
    problems = tool.verify_baseline_against_wheel(tmp_path, "b" * 64, _SHA)
    assert any("!= expected" in p for p in problems)
    assert any("provenance" in p for p in problems)
    drift: tuple[dict[str, Any], dict[str, Any]] = (
        {"modules": {}, "symbols": {"x": {"tier": "stable", "kind": "function"}}},
        {"routes": {}, "exchanges": {}},
    )
    monkeypatch.setattr(tool, "current_snapshots", lambda _r: drift)
    found = tool.verify_baseline_against_wheel(tmp_path, _SHA, _SHA)
    assert any("api_baseline.json differs" in p for p in found)


def test_verify_baseline_cli_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import hashlib

    monkeypatch.setattr(tool, "load_json", lambda _p: {"version": "9.9.9"})

    def unreachable(*_a: Any, **_k: Any) -> Any:
        raise tool.ContractError("unreachable")

    monkeypatch.setattr(tool, "download_published_wheel", unreachable)
    assert tool.main(["verify-baseline"]) == 2
    assert "unreachable" in capsys.readouterr().err
    assert tool.main(["verify-baseline", "--wheel", str(tmp_path / "w.whl")]) == 2

    wheel = tmp_path / "w.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{PKG}/__init__.py", "")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    monkeypatch.setattr(tool, "verify_baseline_against_wheel", lambda *_a: [])
    assert tool.main(["verify-baseline", "--wheel", str(wheel), "--expected-sha256", digest]) == 0
    monkeypatch.setattr(tool, "verify_baseline_against_wheel", lambda *_a: ["boom"])
    assert tool.main(["verify-baseline", "--wheel", str(wheel), "--expected-sha256", digest]) == 1
    assert "does NOT match" in capsys.readouterr().out


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self._body = status, body

    def read(self, amt: int) -> bytes:
        return self._body[:amt]


class _FakeConnection:
    status = 200
    body = b"payload"
    seen: list[tuple[str, str]] = []

    def __init__(self, host: str, timeout: float) -> None:
        self.host = host

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        type(self).seen.append((self.host, path))

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(type(self).status, type(self).body)

    def close(self) -> None:
        pass


def test_http_get_reads_bounded_body_and_rejects_bad_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeConnection)
    assert tool._http_get("https://pypi.org/pypi/x/1/json?a=b", 1, 100) == b"payload"
    assert _FakeConnection.seen[-1] == ("pypi.org", "/pypi/x/1/json?a=b")
    with pytest.raises(tool.ContractError, match="exceeds 3 bytes"):
        tool._http_get("https://pypi.org/x", 1, 3)
    monkeypatch.setattr(_FakeConnection, "status", 503)
    with pytest.raises(tool.ContractError, match="HTTP 503"):
        tool._http_get("https://pypi.org/x", 1, 100)
