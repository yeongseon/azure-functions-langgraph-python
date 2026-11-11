"""Reproducible public-surface inventory for ``azure_functions_langgraph``.

The scan is purely static (``ast`` / ``tomllib`` / text search). It never imports
the package or any optional extra, so it preserves lazy-import behaviour and
needs no cloud backend. Output is deterministic: no timestamps, no versions, no
line numbers, everything sorted.

Usage::

    python tools/public_api_inventory.py --write   # regenerate the artifacts
    python tools/public_api_inventory.py --check   # exit 1 when they are stale

Artifacts (committed): ``docs/api-inventory.json`` (machine readable) and
``docs/api-inventory.md`` (rendered). Decisions live in ``docs/api-boundary.md``.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable, Sequence
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PKG = "azure_functions_langgraph"
JSON_ARTIFACT = Path("docs/api-inventory.json")
MD_ARTIFACT = Path("docs/api-inventory.md")
SCHEMA_VERSION = 1

CORE = "Stable-intent Core"
BETA = "Beta"
EXPERIMENTAL = "Experimental"
INTERNAL = "Internal"

# Longest-prefix match on dotted module names. ``basis`` is ``README`` when the
# README stability table names the surface, ``provisional`` when it does not and
# a maintainer should confirm the tier.
MODULE_TIERS: dict[str, tuple[str, str]] = {
    PKG: (CORE, "README"),
    f"{PKG}.app": (CORE, "README"),
    f"{PKG}.contracts": (CORE, "README"),
    f"{PKG}.protocols": (CORE, "README"),
    f"{PKG}.openapi": (BETA, "provisional"),
    f"{PKG}.authorization": (BETA, "provisional"),
    f"{PKG}.observability": (BETA, "README"),
    f"{PKG}.observability_otel": (BETA, "README"),
    f"{PKG}.checkpointers": (BETA, "README"),
    f"{PKG}.locks": (BETA, "README"),
    f"{PKG}.stores": (EXPERIMENTAL, "provisional"),
    f"{PKG}.platform": (EXPERIMENTAL, "README"),
    f"{PKG}.streaming": (EXPERIMENTAL, "README"),
    f"{PKG}.streaming_authorization": (EXPERIMENTAL, "provisional"),
    f"{PKG}.triggers": (EXPERIMENTAL, "README"),
    f"{PKG}.durable": (EXPERIMENTAL, "README"),
}

# Dotted third-party import prefix -> optional extra that supplies it.
IMPORT_EXTRA: dict[str, str] = {
    "azure.storage.blob": "azure-blob",
    "azure.data.tables": "azure-table",
    "azure.identity": "azure-identity",
    "psycopg": "postgres",
    "langgraph.checkpoint.postgres": "postgres",
    "langgraph.checkpoint.sqlite": "sqlite",
    "langgraph.checkpoint.cosmosdb": "cosmos",
    "langgraph_checkpoint_cosmosdb": "cosmos",
    "opentelemetry": "otel",
    "azurefunctions.extensions.http.fastapi": "streaming",
    "azure.durable_functions": "durable",
    "azure_functions_openapi": "openapi",
}

ROUTE_FAMILIES: dict[str, tuple[str, str]] = {
    "app.py": ("Native HTTP (LangGraphApp)", CORE),
    "streaming.py": ("Native HTTP (StreamingLangGraphApp)", EXPERIMENTAL),
    "platform/": ("Platform SDK compatibility (partial)", EXPERIMENTAL),
    "durable/": ("Durable async-run lifecycle", EXPERIMENTAL),
}

# Files whose tokens count as in-repo consumers; the generated artifacts and the
# test that pins them are excluded so the inventory never references itself.
SELF_FILES = frozenset(
    {
        "docs/api-inventory.md",
        "docs/api-inventory.json",
        "docs/api-boundary.md",
        "tests/test_public_api_inventory.py",
    }
)

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")


# --------------------------------------------------------------------------- #
# Module loading
# --------------------------------------------------------------------------- #


class Module:
    def __init__(self, name: str, path: Path, is_package: bool, tree: ast.Module) -> None:
        self.name = name
        self.path = path
        self.is_package = is_package
        self.tree = tree
        self.constants: dict[str, str] = {}
        self.imports: dict[str, tuple[str, str]] = {}
        self.all_names: list[str] | None = None


def _module_name(src_root: Path, path: Path) -> tuple[str, bool]:
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def _resolve_relative(mod: Module, level: int, module: str | None) -> str:
    base = mod.name.split(".") if mod.is_package else mod.name.split(".")[:-1]
    if level > 1:
        base = base[: -(level - 1)]
    return ".".join(base + ([module] if module else []))


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def load_modules(root: Path) -> dict[str, Module]:
    src_root = root / "src"
    modules: dict[str, Module] = {}
    for path in sorted((src_root / PKG).rglob("*.py")):
        name, is_pkg = _module_name(src_root, path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        mod = Module(name, path, is_pkg, tree)
        for node in tree.body:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            if isinstance(target, ast.Name) and value is not None:
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and target.id != "__all__"
                ):
                    mod.constants[target.id] = value.value
                if target.id == "__all__":
                    mod.all_names = [str(x) for x in ast.literal_eval(value)]
        for walked in ast.walk(tree):
            if isinstance(walked, ast.ImportFrom):
                full = (
                    _resolve_relative(mod, walked.level, walked.module)
                    if walked.level
                    else (walked.module or "")
                )
                if full == PKG or full.startswith(PKG + "."):
                    for alias in walked.names:
                        mod.imports.setdefault(alias.asname or alias.name, (full, alias.name))
        modules[name] = mod
    return modules


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #


def _tier_for(module: str) -> tuple[str, str]:
    best = ""
    for prefix in MODULE_TIERS:
        if (module == prefix or module.startswith(prefix + ".")) and len(prefix) > len(best):
            best = prefix
    return MODULE_TIERS[best]


def _is_public_module(name: str) -> bool:
    return not any(part.startswith("_") for part in name.split("."))


def _args(args: ast.arguments, drop_first: bool) -> str:
    text = ast.unparse(args)
    if drop_first:
        text = re.sub(r"^(self|cls)(, )?", "", text)
    return text


def _func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, drop_first: bool) -> str:
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"({_args(node.args, drop_first)}){ret}"


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    names: list[str] = []
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        names.append(ast.unparse(target))
    return names


def _dataclass_fields(node: ast.ClassDef) -> list[str]:
    fields: list[str] = []
    for stmt in node.body:
        if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
            continue
        if stmt.target.id.startswith("_"):
            continue
        value = stmt.value
        if (
            isinstance(value, ast.Call)
            and ast.unparse(value.func).split(".")[-1] == "field"
            and any(
                kw.arg == "init" and isinstance(kw.value, ast.Constant) and kw.value.value is False
                for kw in value.keywords
            )
        ):
            continue
        text = f"{stmt.target.id}: {ast.unparse(stmt.annotation)}"
        if value is not None:
            text += f" = {ast.unparse(value)}"
        fields.append(text)
    return fields


def _class_info(node: ast.ClassDef) -> dict[str, Any]:
    bases = [ast.unparse(b) for b in node.bases]
    decorators = _decorator_names(node)
    is_dataclass = any(d.split(".")[-1] == "dataclass" for d in decorators)
    init_sig: str | None = None
    methods: list[dict[str, str]] = []
    for stmt in node.body:
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if stmt.name == "__init__":
            init_sig = f"({_args(stmt.args, True)})"
            continue
        if stmt.name.startswith("_"):
            continue
        decos = _decorator_names(stmt)
        tag = "async " if isinstance(stmt, ast.AsyncFunctionDef) else ""
        if "property" in decos:
            tag += "property "
        elif "staticmethod" in decos:
            tag += "staticmethod "
        elif "classmethod" in decos:
            tag += "classmethod "
        methods.append(
            {
                "name": stmt.name,
                "kind": tag.strip() or "method",
                "signature": _func_signature(stmt, "staticmethod" not in decos),
            }
        )
    if init_sig is not None:
        signature = init_sig
    elif is_dataclass or any(b.split(".")[-1] in {"BaseModel", "TypedDict"} for b in bases):
        signature = "(" + ", ".join(_dataclass_fields(node)) + ")"
    else:
        signature = "()"
    if any(b.split(".")[-1] == "Protocol" for b in bases):
        kind = "protocol"
    elif is_dataclass:
        kind = "dataclass"
    else:
        kind = "class"
    return {
        "kind": kind,
        "signature": signature,
        "bases": bases,
        "methods": sorted(methods, key=lambda m: m["name"]),
    }


def _top_level_symbols(mod: Module) -> dict[str, dict[str, Any]]:
    symbols: dict[str, dict[str, Any]] = {}
    for node in mod.tree.body:
        if isinstance(node, ast.ClassDef):
            symbols[node.name] = _class_info(node)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbols[node.name] = {
                "kind": "async function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                "signature": _func_signature(node, False),
                "bases": [],
                "methods": [],
            }
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is None or len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            name = targets[0].id
            if name.startswith("_") or name == "__all__":
                continue
            text = ast.unparse(node.value)
            symbols[name] = {
                "kind": "constant" if name.isupper() else "alias",
                "signature": text if len(text) <= 120 else text[:117] + "...",
                "bases": [],
                "methods": [],
            }
    return symbols


class SymbolIndex:
    def __init__(self, modules: dict[str, Module]) -> None:
        self.modules = modules
        self._own = {name: _top_level_symbols(m) for name, m in modules.items()}

    def find(
        self, module: str, name: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> tuple[str, dict[str, Any]] | None:
        """Return ``(defining_module, info)``, following in-package re-exports."""
        if (module, name) in seen or module not in self.modules:
            return None
        if name in self._own[module]:
            return module, self._own[module][name]
        target = self.modules[module].imports.get(name)
        if target is None:
            return None
        return self.find(target[0], target[1], seen | {(module, name)})

    def own(self, module: str) -> dict[str, dict[str, Any]]:
        return self._own[module]


# --------------------------------------------------------------------------- #
# Consumers
# --------------------------------------------------------------------------- #


def _iter_files(root: Path, base: str, suffixes: tuple[str, ...]) -> Iterable[Path]:
    start = root / base
    if not start.is_dir():
        return
    for path in sorted(start.rglob("*")):
        if path.is_file() and path.suffix in suffixes and ".venv" not in path.parts:
            if "wheels" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


class Consumers:
    def __init__(self, root: Path) -> None:
        self.tokens: dict[str, set[str]] = {"examples": set(), "tests": set(), "docs": set()}
        self.docs_text = ""
        buckets: list[tuple[str, list[Path]]] = [
            ("examples", list(_iter_files(root, "examples", (".py", ".md")))),
            ("tests", list(_iter_files(root, "tests", (".py",)))),
            (
                "docs",
                [p for p in [root / "README.md", root / "COMPATIBILITY.md"] if p.is_file()]
                + list(_iter_files(root, "docs", (".md",))),
            ),
        ]
        docs_parts: list[str] = []
        for bucket, paths in buckets:
            for path in paths:
                if path.relative_to(root).as_posix() in SELF_FILES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                self.tokens[bucket].update(_TOKEN.findall(text))
                if bucket == "docs":
                    docs_parts.append(text)
        self.docs_text = "\n".join(docs_parts)

    def flags(self, name: str) -> dict[str, bool]:
        return {bucket: name in toks for bucket, toks in sorted(self.tokens.items())}


# --------------------------------------------------------------------------- #
# Root exports, extras, optional imports, routes, env vars
# --------------------------------------------------------------------------- #


def _literal_assign(mod: Module, name: str) -> Any:
    for node in mod.tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value:
                value = node.value
                if isinstance(value, ast.Call) and value.args:  # frozenset({...})
                    value = value.args[0]
                return ast.literal_eval(value)
    raise KeyError(name)


def root_inventory(
    modules: dict[str, Module], index: SymbolIndex, consumers: Consumers
) -> dict[str, Any]:
    root = modules[PKG]
    lazy: dict[str, tuple[str, str]] = _literal_assign(root, "_LAZY_IMPORTS")
    requires_extra: dict[str, str] = _literal_assign(root, "_REQUIRES_EXTRA")
    runtime_deps: set[str] = set(_literal_assign(root, "_REQUIRES_RUNTIME_DEPS"))
    all_names: list[str] = list(root.all_names or [])
    exports: list[dict[str, Any]] = []
    for name in sorted(n for n in all_names if n != "__version__"):
        module, attr = lazy[name]
        found = index.find(module, attr)
        info = found[1] if found else {"kind": "unknown", "signature": "", "bases": []}
        tier, basis = _tier_for(module)
        exports.append(
            {
                "name": name,
                "defined_in": module,
                "kind": info["kind"],
                "signature": info["signature"],
                "tier": tier,
                "tier_basis": basis,
                "requires_extra": requires_extra.get(name),
                "requires_runtime_deps": name in runtime_deps,
                "consumers": consumers.flags(name),
            }
        )
    return {
        "all": sorted(all_names),
        "lazy_exports": {k: {"module": v[0], "attr": v[1]} for k, v in sorted(lazy.items())},
        "all_matches_lazy": sorted(n for n in all_names if n != "__version__") == sorted(lazy),
        "exports": exports,
    }


def _extra_for(dotted: str) -> str | None:
    best = ""
    for prefix in IMPORT_EXTRA:
        if (dotted == prefix or dotted.startswith(prefix + ".")) and len(prefix) > len(best):
            best = prefix
    return IMPORT_EXTRA[best] if best else None


def _collect_imports(node: ast.AST, scope: str, out: list[tuple[str, str]]) -> None:
    for child in ast.iter_child_nodes(node):
        child_scope = scope
        if isinstance(child, ast.If) and _is_type_checking(child.test):
            for sub in child.body:
                _collect_imports(sub, "type_checking", out)
                _record(sub, "type_checking", out)
            for sub in child.orelse:
                _record(sub, scope, out)
                _collect_imports(sub, scope, out)
            continue
        if scope == "module" and isinstance(
            child,
            ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Try | ast.If | ast.With,
        ):
            child_scope = "nested"
        _record(child, scope, out)
        _collect_imports(child, child_scope, out)


def _record(node: ast.AST, scope: str, out: list[tuple[str, str]]) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            out.append((alias.name, scope))
    elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
        for alias in node.names:
            dotted = node.module
            if _extra_for(dotted) is None:
                dotted = f"{node.module}.{alias.name}"
            out.append((dotted, scope))
    elif (
        isinstance(node, ast.Call)
        and ast.unparse(node.func) in {"importlib.import_module", "import_module"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        out.append((node.args[0].value, "dynamic"))


def extras_inventory(root: Path, modules: dict[str, Module]) -> dict[str, Any]:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]

    def names(reqs: Iterable[str]) -> list[str]:
        return sorted({m.group(0).lower() for r in reqs if (m := re.match(r"[A-Za-z0-9_.\-]+", r))})

    extras = {
        extra: names(reqs)
        for extra, reqs in sorted(project.get("optional-dependencies", {}).items())
        if extra not in {"dev", "docs"}
    }
    seen: set[tuple[str, str, str, str]] = set()
    for mod in modules.values():
        found: list[tuple[str, str]] = []
        _collect_imports(mod.tree, "module", found)
        for dotted, scope in found:
            extra = _extra_for(dotted)
            if extra is not None:
                seen.add((mod.name, extra, dotted, scope))
    imports = [{"module": m, "extra": e, "import": d, "scope": s} for m, e, d, s in sorted(seen)]
    return {
        "required_dependencies": names(project.get("dependencies", [])),
        "extras": extras,
        "optional_imports": imports,
        "root_import_pulls_optional_extras": any(
            i["module"] == PKG and i["scope"] == "module" for i in imports
        ),
    }


def _norm_route(route: str) -> str:
    return route.replace("{{", "{").replace("}}", "}")


def _family(rel: str) -> tuple[str, str]:
    for prefix, value in ROUTE_FAMILIES.items():
        if rel == prefix or (prefix.endswith("/") and rel.startswith(prefix)):
            return value
    return ("Other", INTERNAL)


def _methods(call: ast.Call) -> list[str]:
    for kw in call.keywords:
        if kw.arg == "methods":
            try:
                return [str(m) for m in ast.literal_eval(kw.value)]
            except (ValueError, SyntaxError):
                return []
    return []


def routes_inventory(
    root: Path, modules: dict[str, Module], consumers: Consumers
) -> list[dict[str, Any]]:
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    pkg_dir = root / "src" / PKG
    for mod in modules.values():
        assigns: list[tuple[int, str, str]] = []
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt, val = node.targets[0], node.value
                if isinstance(tgt, ast.Name):
                    base = (
                        val.func.value
                        if isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
                        else val
                    )
                    if isinstance(base, ast.Name) and base.id in mod.constants:
                        assigns.append((node.lineno, tgt.id, mod.constants[base.id]))
        for node in ast.walk(mod.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"route", "_register_route"}:
                continue
            for kw in node.keywords:
                if kw.arg not in {"route", "route_template"}:
                    continue
                route: str | None = None
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    route = kw.value.value
                elif isinstance(kw.value, ast.Name):
                    if kw.value.id in mod.constants:
                        route = mod.constants[kw.value.id]
                    else:
                        prior = [a for a in assigns if a[1] == kw.value.id and a[0] < node.lineno]
                        route = max(prior)[2] if prior else None
                if route is None:
                    continue
                rel = mod.path.relative_to(pkg_dir).as_posix()
                family, tier = _family(rel)
                for method in _methods(node) or ["?"]:
                    key = (family, _norm_route(route), method)
                    found[key] = {
                        "family": family,
                        "tier": tier,
                        "method": method,
                        "route": _norm_route(route),
                        "documented": ("/" + _norm_route(route)) in consumers.docs_text
                        or ("/" + route) in consumers.docs_text,
                    }
    order = {name: i for i, (name, _) in enumerate(ROUTE_FAMILIES.values())}
    return sorted(
        found.values(),
        key=lambda r: (order.get(r["family"], 99), r["route"], r["method"]),
    )


# ``environ`` / ``env`` are the conventional names of an injected environment mapping
# (e.g. ``read_env(environ)`` in ``_topology``); matching them keeps variables that are
# read through a parameter instead of ``os.environ`` in the inventory.
_ENV_MAPPINGS = {"os.environ", "environ", "env"}
_ENV_READERS = {"os.getenv"} | {f"{m}.{fn}" for m in _ENV_MAPPINGS for fn in ("get", "pop")}


def env_inventory(modules: dict[str, Module], consumers: Consumers) -> list[dict[str, Any]]:
    users: dict[str, set[str]] = {}

    def resolve(mod: Module, arg: ast.expr) -> str | None:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name):
            return mod.constants.get(arg.id)
        return None

    for mod in modules.values():
        for node in ast.walk(mod.tree):
            name: str | None = None
            if isinstance(node, ast.Call) and node.args:
                text = ast.unparse(node.func)
                if text in _ENV_READERS:
                    name = resolve(mod, node.args[0])
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in _ENV_MAPPINGS:
                name = resolve(mod, node.slice)
            if name and _ENV_NAME.match(name):
                users.setdefault(name, set()).add(mod.name)
    return [
        {
            "name": name,
            "modules": sorted(users[name]),
            "documented": name in consumers.tokens["docs"],
        }
        for name in sorted(users)
    ]


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #


def generate(root: Path = ROOT) -> dict[str, Any]:
    modules = load_modules(root)
    index = SymbolIndex(modules)
    consumers = Consumers(root)
    module_rows: list[dict[str, Any]] = []
    for name in sorted(modules):
        mod = modules[name]
        public = _is_public_module(name)
        tier, basis = _tier_for(name) if public else (INTERNAL, "private module")
        row: dict[str, Any] = {
            "module": name,
            "public": public,
            "tier": tier,
            "tier_basis": basis,
        }
        if public:
            own = index.own(name)
            exported = sorted(mod.all_names or [])
            symbols: list[dict[str, Any]] = []
            names = {n for n in own if not n.startswith("_")} | {
                n for n in exported if n != "__version__"
            }
            for sym in sorted(names):
                found = index.find(name, sym)
                info = found[1] if found else {"kind": "unknown", "signature": "", "methods": []}
                symbols.append(
                    {
                        "name": sym,
                        "kind": info["kind"],
                        "signature": info["signature"],
                        "bases": info.get("bases", []),
                        "methods": info.get("methods", []),
                        "defined_in": found[0] if found else None,
                        "in_all": sym in exported,
                        "consumers": consumers.flags(sym),
                    }
                )
            row["all"] = exported
            row["symbols"] = symbols
        module_rows.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "package": PKG,
        "generated_by": "tools/public_api_inventory.py",
        "tiers": [CORE, BETA, EXPERIMENTAL],
        "root": root_inventory(modules, index, consumers),
        "modules": module_rows,
        "packaging": extras_inventory(root, modules),
        "routes": routes_inventory(root, modules, consumers),
        "environment_variables": env_inventory(modules, consumers),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _flags(consumers: dict[str, bool]) -> str:
    return "".join(
        letter if consumers[key] else "-"
        for letter, key in (("E", "examples"), ("T", "tests"), ("D", "docs"))
    )


def render_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_markdown(data: dict[str, Any]) -> str:
    out: list[str] = []
    add = out.append
    add("# Public API inventory")
    add("")
    add(
        "<!-- GENERATED by tools/public_api_inventory.py -- do not edit by hand. "
        "Regenerate with `python tools/public_api_inventory.py --write`. -->"
    )
    add("")
    add(
        "Generated by a static scan (`ast`, no imports of the package or optional extras). "
        "Decisions, tier rationale and the migration policy live in "
        "[API boundary and migration policy](api-boundary.md). The same data is available "
        "as [api-inventory.json](api-inventory.json)."
    )
    add("")
    add(
        "Consumer flags: `E` = referenced under `examples/`, `T` = under `tests/`, "
        "`D` = in README/COMPATIBILITY/`docs/`. `-` means no reference found. A reference is a "
        "name match, not proof of supported usage."
    )
    add("")
    add(
        "Tier basis: `README` = named by the README stability table; `provisional` = not named "
        "there, assigned by analogy and needing maintainer confirmation."
    )
    add("")
    root = data["root"]
    add("## Root exports (`azure_functions_langgraph.__all__`)")
    add("")
    add(
        f"`__all__` and `_LAZY_IMPORTS` agree: **{'yes' if root['all_matches_lazy'] else 'NO'}**. "
        "All exports are resolved lazily through module `__getattr__`."
    )
    add("")
    add("| Symbol | Kind | Defined in | Tier | Basis | Extra | Consumers |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for e in root["exports"]:
        extra = e["requires_extra"] or ("runtime deps" if e["requires_runtime_deps"] else "")
        add(
            f"| `{e['name']}` | {e['kind']} | `{e['defined_in'].removeprefix(PKG + '.')}` | "
            f"{e['tier']} | {e['tier_basis']} | {extra} | `{_flags(e['consumers'])}` |"
        )
    add("")
    add("### Root export signatures")
    add("")
    for e in root["exports"]:
        sig = e["signature"]
        sig = sig if len(sig) <= 160 else sig[:157] + "..."
        add(f"- `{e['name']}{sig}`")
    add("")
    add("## Public modules")
    add("")
    add("| Module | Tier | Basis | `__all__` | Public symbols |")
    add("| --- | --- | --- | --- | --- |")
    for m in data["modules"]:
        if not m["public"]:
            continue
        label = m["module"].removeprefix(PKG).lstrip(".") or "(root)"
        add(
            f"| `{label}` | {m['tier']} | {m['tier_basis']} | {len(m['all'])} | "
            f"{len(m['symbols'])} |"
        )
    private = [m["module"] for m in data["modules"] if not m["public"]]
    add("")
    add(
        f"{len(private)} private modules (any path component starting with `_`) are "
        "**Internal** and carry no compatibility promise."
    )
    add("")
    add("### Symbols by module")
    for m in data["modules"]:
        if not m["public"] or not m["symbols"]:
            continue
        label = m["module"].removeprefix(PKG).lstrip(".") or "(root)"
        add("")
        add(f"#### `{label}` — {m['tier']}")
        add("")
        add("| Symbol | Kind | In `__all__` | Consumers |")
        add("| --- | --- | --- | --- |")
        for s in m["symbols"]:
            add(
                f"| `{s['name']}` | {s['kind']} | {'yes' if s['in_all'] else 'no'} | "
                f"`{_flags(s['consumers'])}` |"
            )
    add("")
    pack = data["packaging"]
    add("## Packaging and optional extras")
    add("")
    add(
        "Required dependencies: " + ", ".join(f"`{d}`" for d in pack["required_dependencies"]) + "."
    )
    add("")
    add(
        "Importing the root package pulls in an optional extra at module level: "
        f"**{'YES' if pack['root_import_pulls_optional_extras'] else 'no'}**."
    )
    add("")
    add("| Extra | Distributions |")
    add("| --- | --- |")
    for extra, dists in pack["extras"].items():
        add(f"| `{extra}` | {', '.join(f'`{d}`' for d in dists)} |")
    add("")
    add("### Modules importing optional extras")
    add("")
    add(
        "`module` scope means the import runs when the module is imported; `nested` means inside "
        "a function/conditional; `dynamic` means `importlib.import_module` at call time; "
        "`type_checking` is annotation-only."
    )
    add("")
    add("| Module | Extra | Import | Scope |")
    add("| --- | --- | --- | --- |")
    for i in pack["optional_imports"]:
        label = i["module"].removeprefix(PKG).lstrip(".") or "(root)"
        add(f"| `{label}` | `{i['extra']}` | `{i['import']}` | {i['scope']} |")
    add("")
    add("## HTTP routes")
    add("")
    add(
        "Routes are shown relative to the Functions host `routePrefix` (default `/api`). "
        "`{name}` is the registered graph name. Only Native HTTP routes are a supported "
        "contract; Platform routes are explicitly partial."
    )
    families: dict[str, list[dict[str, Any]]] = {}
    for r in data["routes"]:
        families.setdefault(r["family"], []).append(r)
    for family, rows in families.items():
        add("")
        add(f"### {family} — {rows[0]['tier']}")
        add("")
        add("| Method | Route | Documented |")
        add("| --- | --- | --- |")
        for r in rows:
            add(f"| {r['method']} | `/{r['route']}` | {'yes' if r['documented'] else 'no'} |")
    add("")
    add("## Environment variables")
    add("")
    add("| Variable | Read in | Documented |")
    add("| --- | --- | --- |")
    for v in data["environment_variables"]:
        mods = ", ".join(f"`{m.removeprefix(PKG).lstrip('.') or '(root)'}`" for m in v["modules"])
        add(f"| `{v['name']}` | {mods} | {'yes' if v['documented'] else 'no'} |")
    add("")
    add("## Public module symbols without an in-repo consumer")
    add("")
    add(
        "Names found in no example, test or doc (name match only, so a name shared by two "
        "modules is counted for both). These are candidates for a documentation or test "
        "follow-up, not for removal."
    )
    add("")
    add("| Module | Symbols |")
    add("| --- | --- |")
    for m in data["modules"]:
        if not m["public"]:
            continue
        unref = [s["name"] for s in m["symbols"] if not any(s["consumers"].values())]
        if unref:
            label = m["module"].removeprefix(PKG).lstrip(".") or "(root)"
            add(f"| `{label}` | " + ", ".join(f"`{n}`" for n in unref) + " |")
    add("")
    add("## Root exports without an in-repo consumer")
    add("")
    orphans = [e["name"] for e in root["exports"] if not any(e["consumers"].values())]
    add(", ".join(f"`{n}`" for n in orphans) + "." if orphans else "None.")
    add("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def render_all(root: Path = ROOT) -> dict[Path, str]:
    data = generate(root)
    return {JSON_ARTIFACT: render_json(data), MD_ARTIFACT: render_markdown(data)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else "")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate committed artifacts")
    mode.add_argument("--check", action="store_true", help="exit 1 if artifacts are stale")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    rendered = render_all(args.root)
    if args.write:
        for rel, text in rendered.items():
            (args.root / rel).write_bytes(text.encode("utf-8"))
            print(f"wrote {rel.as_posix()}")
        return 0
    stale = [
        rel.as_posix()
        for rel, text in rendered.items()
        if not (args.root / rel).is_file() or (args.root / rel).read_bytes() != text.encode("utf-8")
    ]
    if stale:
        print(
            "stale public API inventory: " + ", ".join(stale) + "\n"
            "run `python tools/public_api_inventory.py --write` and commit the result",
            file=sys.stderr,
        )
        return 1
    print("public API inventory is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
