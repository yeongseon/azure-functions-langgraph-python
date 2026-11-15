"""Pins the public API inventory (issue #878): reproducibility, tiers, lazy imports."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

import azure_functions_langgraph as pkg
from tools import public_api_inventory as inv

ROOT = Path(__file__).resolve().parents[1]
DATA = inv.generate(ROOT)

# Optional extras plus the heavy runtime deps: none may load on ``import pkg``.
LAZY_FORBIDDEN = (
    "azure_functions_langgraph.app",
    "azure.functions",
    "langgraph",
    "opentelemetry",
    "azure.storage",
    "azure.data",
    "azure.identity",
    "azure.durable_functions",
    "azurefunctions",
    "psycopg",
    "azure_functions_openapi",
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #


def test_committed_inventory_is_current() -> None:
    for rel, text in inv.render_all(ROOT).items():
        assert (ROOT / rel).read_bytes() == text.encode("utf-8"), (
            f"{rel} is stale: run `python tools/public_api_inventory.py --write`"
        )


def test_generation_is_deterministic() -> None:
    assert inv.render_all(ROOT) == inv.render_all(ROOT)
    assert json.loads(inv.render_json(DATA)) == DATA


def test_cli_check_passes_on_repo() -> None:
    assert inv.main(["--check"]) == 0


def test_cli_check_fails_when_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "docs").mkdir()
    rendered = {inv.JSON_ARTIFACT: "{}\n", inv.MD_ARTIFACT: "# x\n"}
    monkeypatch.setattr(inv, "render_all", lambda root=inv.ROOT: rendered)
    assert inv.main(["--check", "--root", str(tmp_path)]) == 1  # missing files
    assert inv.main(["--write", "--root", str(tmp_path)]) == 0
    assert inv.main(["--check", "--root", str(tmp_path)]) == 0
    (tmp_path / inv.MD_ARTIFACT).write_text("# drifted\n", encoding="utf-8")
    assert inv.main(["--check", "--root", str(tmp_path)]) == 1
    assert "stale public API inventory" in capsys.readouterr().err


def test_scanner_does_not_import_the_package() -> None:
    result = _run(
        "import sys\n"
        "from tools import public_api_inventory as t\n"
        "t.generate()\n"
        "print([m for m in sys.modules if m == 'azure_functions_langgraph' "
        "or m.startswith('azure_functions_langgraph.')])\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# --------------------------------------------------------------------------- #
# Inventory content and tiers
# --------------------------------------------------------------------------- #


def test_root_all_matches_lazy_exports_and_runtime() -> None:
    root = DATA["root"]
    assert root["all_matches_lazy"] is True
    assert root["all"] == sorted(pkg.__all__)
    assert {e["name"] for e in root["exports"]} == set(pkg.__all__) - {"__version__"}
    assert all(e["kind"] != "unknown" for e in root["exports"])


def test_every_public_symbol_has_a_tier() -> None:
    for e in DATA["root"]["exports"]:
        assert e["tier"] in DATA["tiers"], e["name"]
    for m in DATA["modules"]:
        if m["public"]:
            assert m["tier"] in DATA["tiers"], m["module"]
        else:
            assert m["tier"] == inv.INTERNAL
            assert any(part.startswith("_") for part in m["module"].split("."))


def _readme_rows() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[1] in {"Stable", "Beta", "Experimental"}:
            rows[cells[1]] = cells[0]
    return rows


def test_tiers_match_readme_stability_table() -> None:
    rows = _readme_rows()
    assert set(rows) == {"Stable", "Beta", "Experimental"}
    by_name = {e["name"]: e["tier"] for e in DATA["root"]["exports"]}
    assert "`LangGraphApp`" in rows["Stable"] and by_name["LangGraphApp"] == inv.CORE
    assert "`RunObserver`" in rows["Beta"] and by_name["RunObserver"] == inv.BETA
    assert "true HTTP streaming" in rows["Experimental"]
    assert by_name["StreamingLangGraphApp"] == inv.EXPERIMENTAL
    assert "Service Bus" in rows["Experimental"]
    assert by_name["default_message_mapper"] == inv.EXPERIMENTAL
    tier_of = {m["module"]: m["tier"] for m in DATA["modules"] if m["public"]}
    assert tier_of[f"{inv.PKG}.checkpointers"] == inv.BETA
    assert tier_of[f"{inv.PKG}.locks"] == inv.BETA
    assert tier_of[f"{inv.PKG}.platform.routes"] == inv.EXPERIMENTAL
    assert tier_of[f"{inv.PKG}.durable"] == inv.EXPERIMENTAL


def test_routes_are_classified_by_family() -> None:
    routes = {(r["family"], r["method"], r["route"]): r for r in DATA["routes"]}
    native = "Native HTTP (LangGraphApp)"
    assert (native, "POST", "graphs/{name}/invoke") in routes
    assert (native, "GET", "graphs/{name}/threads/{thread_id}/state") in routes
    platform = "Platform SDK compatibility (partial)"
    assert (platform, "POST", "threads/{thread_id}/runs/wait") in routes
    durable = "Durable async-run lifecycle"
    assert (durable, "POST", "runs/{run_id}/cancel") in routes
    assert all(r["family"] != "Other" and r["method"] != "?" for r in DATA["routes"])
    assert routes[(native, "POST", "graphs/{name}/invoke")]["tier"] == inv.CORE
    assert routes[(platform, "POST", "threads/{thread_id}/runs/wait")]["tier"] == inv.EXPERIMENTAL


def test_environment_variables_and_extras_are_inventoried() -> None:
    env = {v["name"] for v in DATA["environment_variables"]}
    assert {"AZFUNC_LANGGRAPH_LOCK_BACKEND", "AZFUNC_LANGGRAPH_PLATFORM_STRICT"} <= env
    packaging = DATA["packaging"]
    assert {"otel", "streaming", "durable", "azure-blob"} <= set(packaging["extras"])
    assert "dev" not in packaging["extras"]
    covered = {i["extra"] for i in packaging["optional_imports"]}
    assert set(packaging["extras"]) - {"azure-identity"} <= covered


def test_consumer_flags_reflect_repository_usage() -> None:
    by_name = {e["name"]: e["consumers"] for e in DATA["root"]["exports"]}
    assert by_name["LangGraphApp"] == {"docs": True, "examples": True, "tests": True}


# --------------------------------------------------------------------------- #
# Positive / negative import checks and lazy behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "export",
    [e for e in DATA["root"]["exports"] if e["requires_extra"] is None],
    ids=lambda e: str(e["name"]),
)
def test_root_export_resolves_lazily(export: dict[str, Any]) -> None:
    obj = getattr(pkg, export["name"])
    target = getattr(importlib.import_module(export["defined_in"]), export["name"], None)
    assert obj is target or target is None
    assert pkg.__getattr__(export["name"]) is obj


def test_unknown_names_are_rejected() -> None:
    with pytest.raises(AttributeError):
        pkg.__getattr__("DoesNotExist")
    with pytest.raises(ImportError):
        exec("from azure_functions_langgraph import CompiledStateGraph", {})
    assert "CompiledStateGraph" not in pkg.__all__


def test_no_experimental_namespace_was_introduced() -> None:
    assert importlib.util.find_spec(f"{inv.PKG}.experimental") is None


@pytest.mark.parametrize(
    "export",
    [e for e in DATA["root"]["exports"] if e["requires_extra"] or e["requires_runtime_deps"]],
    ids=lambda e: str(e["name"]),
)
def test_missing_dependency_yields_install_hint(
    export: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = importlib.import_module
    target = export["defined_in"]

    def fake(name: str, package: str | None = None) -> Any:
        if name == target:
            raise ImportError("simulated missing dependency")
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", fake)
    with pytest.raises(ImportError) as excinfo:
        pkg.__getattr__(export["name"])
    message = str(excinfo.value)
    if export["requires_extra"]:
        assert f"[{export['requires_extra']}]" in message
    else:
        assert "pip install azure-functions-langgraph" in message


def test_importing_the_package_loads_no_optional_dependency() -> None:
    result = _run(
        "import sys, json\n"
        "import azure_functions_langgraph\n"
        f"forbidden = {LAZY_FORBIDDEN!r}\n"
        "loaded = [m for m in forbidden if m in sys.modules]\n"
        "print(json.dumps(loaded))\n"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert DATA["packaging"]["root_import_pulls_optional_extras"] is False


# --------------------------------------------------------------------------- #
# Scanner internals
# --------------------------------------------------------------------------- #


def test_import_scopes_are_classified() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "import opentelemetry\n"
        "if TYPE_CHECKING:\n"
        "    from psycopg import rows\n"
        "def f():\n"
        "    import azure.durable_functions\n"
        "    importlib.import_module('azure.data.tables')\n"
    )
    found: list[tuple[str, str]] = []
    inv._collect_imports(tree, "module", found)
    assert ("opentelemetry", "module") in found
    assert ("psycopg", "type_checking") in found or ("psycopg.rows", "type_checking") in found
    assert ("azure.durable_functions", "nested") in found
    assert ("azure.data.tables", "dynamic") in found


def test_relative_import_resolution() -> None:
    tree = ast.parse("")
    pkg_mod = inv.Module("a.b", Path("x"), True, tree)
    leaf = inv.Module("a.b.c", Path("x"), False, tree)
    assert inv._resolve_relative(pkg_mod, 1, "d") == "a.b.d"
    assert inv._resolve_relative(leaf, 1, "d") == "a.b.d"
    assert inv._resolve_relative(leaf, 2, None) == "a"
