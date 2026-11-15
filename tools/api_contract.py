#!/usr/bin/env python3
"""Public API and Native HTTP contract gate (issue #879).

Compares the package under review with a committed baseline of the last
published release and reports **added**, **removed** and **changed** public
surface, split into additive and breaking changes. A breaking change in the
*Stable* or *Beta* tier fails the gate unless ``tests/contracts/classifications.json``
carries an explicit, traceable classification for that exact change.
*Experimental* and undeclared-tier changes are reported in their own tier and
never certified as stable.

Two independent contracts are checked:

* **Python API**: ``__all__``, public module paths, signatures, class members
  and fields. Extracted statically with :mod:`ast`, so nothing is imported and
  optional extras / cloud backends are never required.
* **Native HTTP**: the real registered Functions routes and handlers are run
  offline against in-memory fake graphs; the observed status, content type,
  JSON shape and SSE frame shape are compared with
  ``tests/contracts/native_http_baseline.json``.

Why not griffe: a dev dependency would add a pinned supply-chain surface and
still needs the HTTP fixtures; the stdlib ``ast`` extraction below covers the
supported surface (functions, classes, fields, aliases) in about 300 lines and
is deterministic. Revisit if the surface needs inheritance resolution.

Regeneration policy (see ``docs/api-contract-gate.md``): baselines describe a
published release only. Running ``snapshot`` can never approve a change by
itself because ``check`` also requires a review entry in the classification
file whose ``sha256`` matches the committed baseline bytes.

This module is intentionally independent of any other API inventory tooling.
Stdlib-only; exit code 0 = gate passed, 1 = gate failed, 2 = usage error.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
import urllib.parse
import zipfile

PACKAGE = "azure_functions_langgraph"
SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = REPO_ROOT / "tests" / "contracts"
API_BASELINE = CONTRACT_DIR / "api_baseline.json"
HTTP_BASELINE = CONTRACT_DIR / "native_http_baseline.json"
CLASSIFICATIONS = CONTRACT_DIR / "classifications.json"
BASELINE_FILES = (API_BASELINE, HTTP_BASELINE)

# Tier declarations mirror the README stability table. Longest matching module
# prefix wins; anything else is "unclassified" and is never certified stable.
TIER_RULES: tuple[tuple[str, str], ...] = (
    ("app", "stable"),
    ("contracts", "stable"),
    ("protocols", "stable"),
    ("authorization", "stable"),
    ("openapi", "stable"),
    ("_metadata", "stable"),
    ("checkpointers", "beta"),
    ("locks", "beta"),
    ("observability", "beta"),
    ("observability_otel", "beta"),
    ("platform", "experimental"),
    ("durable", "experimental"),
    ("streaming", "experimental"),
    ("streaming_authorization", "experimental"),
    ("triggers", "experimental"),
    ("stores", "experimental"),
)
BLOCKING_TIERS = frozenset({"stable", "beta"})
CLASSIFICATION_KINDS = frozenset({"breaking", "security-breaking", "compatible"})
_REF_RE = re.compile(r"^(#\d+|https://github\.com/[\w.-]+/[\w.-]+/(issues|pull)/\d+)$")
_SKIPPED_EXPORTS = frozenset({"__version__"})


class ContractError(Exception):
    """Raised for malformed inputs (baselines, classifications, wheels)."""


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


def tier_for(module: str) -> str:
    """Return the declared tier of a dotted module path inside the package."""
    relative = module[len(PACKAGE) + 1 :] if module.startswith(PACKAGE + ".") else ""
    best = ""
    tier = "unclassified"
    for prefix, declared in TIER_RULES:
        if (relative == prefix or relative.startswith(prefix + ".")) and len(prefix) > len(best):
            best, tier = prefix, declared
    return tier


# ---------------------------------------------------------------------------
# Static Python API extraction
# ---------------------------------------------------------------------------


def _unparse(node: ast.AST | None) -> str | None:
    return None if node is None else ast.unparse(node)


def _describe_params(args: ast.arguments) -> list[dict[str, Any]]:
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults.extend(args.defaults)
    out: list[dict[str, Any]] = []
    posonly = {id(a) for a in args.posonlyargs}
    for arg, default in zip(positional, defaults, strict=True):
        out.append(
            _param(
                arg,
                "positional_only" if id(arg) in posonly else "positional_or_keyword",
                default,
            )
        )
    if args.vararg is not None:
        out.append(_param(args.vararg, "var_positional", None))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        out.append(_param(arg, "keyword_only", default))
    if args.kwarg is not None:
        out.append(_param(args.kwarg, "var_keyword", None))
    return out


def _param(arg: ast.arg, kind: str, default: ast.expr | None) -> dict[str, Any]:
    return {
        "name": arg.arg,
        "kind": kind,
        "annotation": _unparse(arg.annotation),
        "default": _unparse(default),
        "required": default is None and not kind.startswith("var_"),
    }


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    names: list[str] = []
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        names.append(ast.unparse(target).rsplit(".", 1)[-1])
    return names


def _describe_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    decorators = _decorator_names(node)
    kind = "property" if "property" in decorators else "function"
    return {
        "kind": kind,
        "async": isinstance(node, ast.AsyncFunctionDef),
        "decorators": sorted(d for d in decorators if d in {"classmethod", "staticmethod"}),
        "params": _describe_params(node.args),
        "returns": _unparse(node.returns),
    }


def _describe_class(node: ast.ClassDef) -> dict[str, Any]:
    bases = [ast.unparse(b) for b in node.bases]
    decorators = _decorator_names(node)
    class_kind = "other"
    if any(b.rsplit(".", 1)[-1] == "Protocol" or b.startswith("Protocol[") for b in bases):
        class_kind = "protocol"
    elif "dataclass" in decorators:
        class_kind = "dataclass"
    members: dict[str, dict[str, Any]] = {}
    for stmt in node.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            if not stmt.name.startswith("_") or stmt.name in {"__init__", "__call__"}:
                members[stmt.name] = _describe_function(stmt)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if not stmt.target.id.startswith("_"):
                members[stmt.target.id] = {
                    "kind": "field",
                    "annotation": _unparse(stmt.annotation),
                    "default": _unparse(stmt.value),
                    "required": stmt.value is None,
                }
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    members[target.id] = {
                        "kind": "attribute",
                        "annotation": None,
                        "default": _unparse(stmt.value),
                        "required": False,
                    }
    return {"kind": "class", "class_kind": class_kind, "bases": bases, "members": members}


def _describe_value(node: ast.Assign | ast.AnnAssign) -> dict[str, Any]:
    value = _unparse(node.value)
    return {
        "kind": "variable",
        "annotation": _unparse(node.annotation) if isinstance(node, ast.AnnAssign) else None,
        "value": value if value is not None and len(value) <= 200 else None,
    }


@dataclass
class _ModuleIndex:
    name: str
    is_package: bool
    defs: dict[str, ast.stmt] = field(default_factory=dict)
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    lazy: dict[str, tuple[str, str]] = field(default_factory=dict)
    exports: list[str] | None = None


def _module_path(root: Path, dotted: str) -> Path | None:
    parts = dotted.split(".")
    base = root.joinpath(*parts)
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _iter_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.If):
            yield from _iter_statements(stmt.body)
            yield from _iter_statements(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            yield from _iter_statements(stmt.body)


def _assign_names(stmt: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    return [t.id for t in targets if isinstance(t, ast.Name)]


def _literal(node: ast.expr | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except ValueError:
        return None


def _resolve_import(index_name: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    base = index_name.split(".")
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join([*base, *([node.module] if node.module else [])])


class ApiExtractor:
    """Static extractor over one source root containing ``azure_functions_langgraph``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, _ModuleIndex | None] = {}
        if _module_path(root, PACKAGE) is None:
            raise ContractError(f"{PACKAGE} not found under {root}")

    def index(self, dotted: str) -> _ModuleIndex | None:
        if dotted in self._cache:
            return self._cache[dotted]
        path = _module_path(self.root, dotted)
        result: _ModuleIndex | None = None
        if path is not None:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            result = _ModuleIndex(dotted, path.name == "__init__.py")
            for stmt in _iter_statements(tree.body):
                self._index_statement(result, stmt)
        self._cache[dotted] = result
        return result

    @staticmethod
    def _index_statement(idx: _ModuleIndex, stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            idx.defs[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign | ast.AnnAssign):
            for name in _assign_names(stmt):
                value = _literal(stmt.value)
                if name == "__all__" and isinstance(value, list | tuple):
                    idx.exports = [v for v in value if isinstance(v, str)]
                elif name == "_LAZY_IMPORTS" and isinstance(value, dict):
                    idx.lazy = {
                        str(k): (str(v[0]), str(v[1]))
                        for k, v in value.items()
                        if isinstance(v, tuple) and len(v) == 2
                    }
                elif not name.startswith("__"):
                    idx.defs.setdefault(name, stmt)
        elif isinstance(stmt, ast.ImportFrom):
            target = _resolve_import(idx.name, idx.is_package, stmt)
            if target is not None:
                for alias in stmt.names:
                    idx.imports[alias.asname or alias.name] = (target, alias.name)

    def resolve(
        self, module: str, name: str, _seen: frozenset[tuple[str, str]] = frozenset()
    ) -> tuple[str, dict[str, Any]]:
        """Return ``(defined_in, descriptor)`` for *name* exported from *module*."""
        if (module, name) in _seen:
            return module, {"kind": "unresolved"}
        seen = _seen | {(module, name)}
        idx = self.index(module)
        if idx is None:
            return module, {"kind": "external", "target": f"{module}.{name}"}
        node = idx.defs.get(name)
        if isinstance(node, ast.ClassDef):
            return module, _describe_class(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return module, _describe_function(node)
        if isinstance(node, ast.Assign | ast.AnnAssign):
            return module, _describe_value(node)
        target = idx.imports.get(name) or idx.lazy.get(name)
        if target is not None:
            return self.resolve(target[0], target[1], seen)
        return module, {"kind": "unresolved"}

    def public_modules(self) -> list[str]:
        modules: list[str] = []
        pkg_dir = self.root / PACKAGE
        for path in sorted(pkg_dir.rglob("*.py")):
            relative = path.relative_to(self.root).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if any(p.startswith("_") for p in parts[1:]):
                continue
            modules.append(".".join(parts))
        return sorted(set(modules))

    def snapshot(self) -> dict[str, Any]:
        modules: dict[str, Any] = {}
        symbols: dict[str, Any] = {}
        for module in self.public_modules():
            idx = self.index(module)
            if idx is None:  # pragma: no cover - public_modules lists existing files
                continue
            exports = sorted(n for n in (idx.exports or []) if n not in _SKIPPED_EXPORTS)
            modules[module] = {
                "tier": tier_for(module) if module != PACKAGE else "package",
                "all": exports if idx.exports is not None else None,
            }
            for name in exports:
                defined_in, descriptor = self.resolve(module, name)
                descriptor["defined_in"] = defined_in
                descriptor["tier"] = tier_for(defined_in)
                symbols[f"{module}:{name}"] = descriptor
        return {"modules": modules, "symbols": symbols}


# ---------------------------------------------------------------------------
# Native HTTP contract (real handlers, offline)
# ---------------------------------------------------------------------------

_LITERAL_KEYS = frozenset({"status", "error"})


def shape_of(value: Any, key: str = "") -> Any:
    """Reduce a JSON value to its structure; selected fixed keys keep their value."""
    if isinstance(value, dict):
        return {k: shape_of(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        shapes = {json.dumps(shape_of(v), sort_keys=True): shape_of(v) for v in value}
        return [shapes[k] for k in sorted(shapes)]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return f"string={value}" if key in _LITERAL_KEYS and len(value) <= 64 else "string"
    return type(value).__name__


def _body_shape(content_type: str, body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace")
    if content_type.startswith("text/event-stream"):
        events: list[dict[str, Any]] = []
        for frame in text.split("\n\n"):
            if not frame.strip():
                continue
            event = ""
            data: Any = None
            for line in frame.splitlines():
                if line.startswith("event:"):
                    event = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    try:
                        data = shape_of(json.loads(line[len("data:") :].strip()))
                    except ValueError:
                        data = "unparseable"
            events.append({"event": event, "data": data})
        return {"sse": events}
    try:
        return {"json": shape_of(json.loads(text))}
    except ValueError:
        return {"text": "present" if text else "empty"}


def _purge_package_modules() -> None:
    for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
        del sys.modules[name]


def collect_http_contract(source_root: Path | None = None) -> dict[str, Any]:
    """Run the real registered handlers offline and return the observed contract.

    When *source_root* is given the package is imported from that tree (for
    example an unpacked wheel) instead of the already importable one.
    """
    if source_root is not None:
        _purge_package_modules()
        sys.path.insert(0, str(source_root))
    logging.disable(logging.CRITICAL)  # handlers log expected graph failures
    try:
        return _collect_http_contract()
    finally:
        logging.disable(logging.NOTSET)
        if source_root is not None:
            sys.path.remove(str(source_root))
            _purge_package_modules()


class _Snapshot:
    values = {"messages": [{"role": "assistant", "content": "state"}]}
    next = ("node",)
    metadata = {"step": 1}
    config = {"configurable": {"thread_id": "t1"}}
    parent_config = None
    created_at = "2024-01-01T00:00:00+00:00"
    tasks = ()
    interrupts = ()


class _GoodGraph:
    checkpointer = object()

    def invoke(self, input: Any, config: Any = None) -> Any:
        return {"messages": [{"role": "assistant", "content": "hello"}]}

    def stream(self, input: Any, config: Any = None, stream_mode: str = "values") -> Any:
        yield {"messages": [{"role": "assistant", "content": "chunk1"}]}
        yield {"messages": [{"role": "assistant", "content": "chunk2"}]}

    def get_state(self, config: Any) -> Any:
        return _Snapshot()


class _FailingGraph:
    checkpointer = None

    def invoke(self, input: Any, config: Any = None) -> Any:
        raise RuntimeError("boom")

    def stream(self, input: Any, config: Any = None, stream_mode: str = "values") -> Any:
        raise RuntimeError("boom")


def _collect_http_contract() -> dict[str, Any]:
    import azure.functions as func

    from azure_functions_langgraph import LangGraphApp

    app = LangGraphApp()
    app.register(graph=_GoodGraph(), name="agent")
    app.register(graph=_FailingGraph(), name="broken")
    functions = {fn.get_function_name(): fn for fn in app.function_app.get_functions()}

    routes: list[dict[str, Any]] = []
    for name, fn in sorted(functions.items()):
        trigger = fn.get_trigger()
        routes.append(
            {
                "function": name,
                "route": getattr(trigger, "route", None),
                "methods": sorted(
                    str(getattr(m, "name", m)) for m in (getattr(trigger, "methods", None) or [])
                ),
                "auth_level": str(getattr(getattr(trigger, "auth_level", None), "name", "")),
            }
        )

    def call(
        function: str,
        method: str,
        url: str,
        body: bytes = b"",
        route_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        fn = functions.get(function)
        if fn is None:
            return {"missing_function": function}
        request = func.HttpRequest(
            method=method,
            url=url,
            body=body,
            headers={"Content-Type": "application/json"},
            route_params=route_params or {},
        )
        response: Any = fn.get_user_function()(request)
        content_type = str(response.mimetype or "")
        return {
            "status": response.status_code,
            "content_type": content_type,
            "body": _body_shape(content_type, response.get_body()),
        }

    good = json.dumps({"input": {"messages": [{"role": "human", "content": "hi"}]}}).encode()
    exchanges: dict[str, Any] = {
        "health": call("aflg_health", "GET", "/api/health"),
        "health_details": call("aflg_health_details", "GET", "/api/health/details"),
        "invoke_ok": call("aflg_agent_invoke", "POST", "/api/graphs/agent/invoke", good),
        "invoke_invalid_json": call("aflg_agent_invoke", "POST", "/api/graphs/agent/invoke", b"{"),
        "invoke_missing_input": call(
            "aflg_agent_invoke", "POST", "/api/graphs/agent/invoke", b"{}"
        ),
        "invoke_graph_failure": call(
            "aflg_broken_invoke", "POST", "/api/graphs/broken/invoke", good
        ),
        "stream_ok": call("aflg_agent_stream", "POST", "/api/graphs/agent/stream", good),
        "stream_graph_failure": call(
            "aflg_broken_stream", "POST", "/api/graphs/broken/stream", good
        ),
        "state_ok": call(
            "aflg_agent_state",
            "GET",
            "/api/graphs/agent/threads/t1/state",
            route_params={"thread_id": "t1"},
        ),
    }
    return {"routes": routes, "exchanges": exchanges}


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One observed difference between baseline and current."""

    id: str
    category: str  # added | removed | changed
    breaking: bool
    tier: str
    surface: str  # python | http
    detail: str


def _change(
    category: str, path: str, breaking: bool, tier: str, surface: str, detail: str
) -> Change:
    return Change(f"{category}:{path}", category, breaking, tier, surface, detail)


def _diff_params(
    path: str, old: dict[str, Any], new: dict[str, Any], tier: str, out: list[Change]
) -> None:
    old_params = {p["name"]: p for p in old["params"]}
    new_params = {p["name"]: p for p in new["params"]}
    for name in old_params.keys() - new_params.keys():
        out.append(
            _change("removed", f"{path}({name})", True, tier, "python", f"parameter {name} removed")
        )
    for name in new_params.keys() - old_params.keys():
        param = new_params[name]
        breaking = bool(param["required"])
        out.append(
            _change(
                "added",
                f"{path}({name})",
                breaking,
                tier,
                "python",
                f"{'required' if breaking else 'optional'} parameter {name} added",
            )
        )
    for name in old_params.keys() & new_params.keys():
        before, after = old_params[name], new_params[name]
        compatible_kind = (
            before["kind"] == "keyword_only" and after["kind"] == "positional_or_keyword"
        )
        if before["kind"] != after["kind"]:
            out.append(
                _change(
                    "changed",
                    f"{path}({name}).kind",
                    not compatible_kind,
                    tier,
                    "python",
                    f"parameter kind {before['kind']} -> {after['kind']}",
                )
            )
        for aspect in ("annotation", "default"):
            if before[aspect] != after[aspect]:
                relaxed = aspect == "default" and before["default"] is None
                out.append(
                    _change(
                        "changed",
                        f"{path}({name}).{aspect}",
                        not relaxed,
                        tier,
                        "python",
                        f"parameter {aspect} {before[aspect]!r} -> {after[aspect]!r}",
                    )
                )
    old_order = [p["name"] for p in old["params"] if p["name"] in new_params]
    new_order = [p["name"] for p in new["params"] if p["name"] in old_params]
    if old_order != new_order:
        out.append(
            _change("changed", f"{path}.order", True, tier, "python", "parameter order changed")
        )
    else:
        shifted = [
            p["name"]
            for index, p in enumerate(new["params"])
            if p["name"] in new_params.keys() - old_params.keys()
            and p["kind"] in {"positional_only", "positional_or_keyword"}
            and any(
                q["name"] in old_params
                and q["kind"] in {"positional_only", "positional_or_keyword"}
                for q in new["params"][index + 1 :]
            )
        ]
        for name in shifted:
            out.append(
                _change(
                    "added",
                    f"{path}({name}).position",
                    True,
                    tier,
                    "python",
                    f"parameter {name} inserted before existing positional parameters",
                )
            )


def _diff_callable(
    path: str, old: dict[str, Any], new: dict[str, Any], tier: str, out: list[Change]
) -> None:
    _diff_params(path, old, new, tier, out)
    for aspect, label in (("returns", "return annotation"), ("async", "async-ness")):
        if old[aspect] != new[aspect]:
            out.append(
                _change(
                    "changed",
                    f"{path}.{aspect}",
                    True,
                    tier,
                    "python",
                    f"{label} {old[aspect]!r} -> {new[aspect]!r}",
                )
            )
    if old["decorators"] != new["decorators"]:
        out.append(
            _change("changed", f"{path}.decorators", True, tier, "python", "binding changed")
        )


def _diff_member(
    path: str,
    old: dict[str, Any],
    new: dict[str, Any],
    tier: str,
    out: list[Change],
) -> None:
    if old["kind"] != new["kind"]:
        out.append(
            _change(
                "changed",
                f"{path}.kind",
                True,
                tier,
                "python",
                f"member kind {old['kind']} -> {new['kind']}",
            )
        )
    elif old["kind"] in {"function", "property"}:
        _diff_callable(path, old, new, tier, out)
    else:
        for aspect in ("annotation", "default"):
            if old[aspect] != new[aspect]:
                out.append(
                    _change(
                        "changed",
                        f"{path}.{aspect}",
                        True,
                        tier,
                        "python",
                        f"{aspect} {old[aspect]!r} -> {new[aspect]!r}",
                    )
                )


def _diff_symbol(
    path: str, old: dict[str, Any], new: dict[str, Any], tier: str, out: list[Change]
) -> None:
    if old["kind"] != new["kind"]:
        out.append(
            _change(
                "changed", f"{path}.kind", True, tier, "python", f"{old['kind']} -> {new['kind']}"
            )
        )
        return
    kind = old["kind"]
    if kind == "function":
        _diff_callable(path, old, new, tier, out)
    elif kind == "variable":
        for aspect in ("annotation", "value"):
            if old[aspect] != new[aspect]:
                out.append(
                    _change(
                        "changed",
                        f"{path}.{aspect}",
                        True,
                        tier,
                        "python",
                        f"{aspect} {old[aspect]!r} -> {new[aspect]!r}",
                    )
                )
    elif kind == "class":
        _diff_class(path, old, new, tier, out)


def _diff_class(
    path: str, old: dict[str, Any], new: dict[str, Any], tier: str, out: list[Change]
) -> None:
    for base in sorted(set(old["bases"]) - set(new["bases"])):
        out.append(_change("removed", f"{path}.base({base})", True, tier, "python", "base removed"))
    for base in sorted(set(new["bases"]) - set(old["bases"])):
        out.append(_change("added", f"{path}.base({base})", False, tier, "python", "base added"))
    if old["class_kind"] != new["class_kind"]:
        out.append(
            _change(
                "changed",
                f"{path}.class_kind",
                True,
                tier,
                "python",
                f"{old['class_kind']} -> {new['class_kind']}",
            )
        )
    for name in sorted(old["members"].keys() - new["members"].keys()):
        out.append(
            _change("removed", f"{path}.{name}", True, tier, "python", f"member {name} removed")
        )
    for name in sorted(new["members"].keys() - old["members"].keys()):
        member = new["members"][name]
        # A new abstract method on a Protocol breaks implementers; a new required
        # field breaks constructors. Everything else is additive.
        breaking = (new["class_kind"] == "protocol" and member["kind"] == "function") or (
            member["kind"] == "field" and bool(member["required"])
        )
        out.append(
            _change(
                "added",
                f"{path}.{name}",
                breaking,
                tier,
                "python",
                f"member {name} added"
                + (" (breaks implementers or constructors)" if breaking else ""),
            )
        )
    for name in sorted(old["members"].keys() & new["members"].keys()):
        _diff_member(f"{path}.{name}", old["members"][name], new["members"][name], tier, out)


def diff_api(baseline: dict[str, Any], current: dict[str, Any]) -> list[Change]:
    out: list[Change] = []
    for module in sorted(baseline["modules"].keys() - current["modules"].keys()):
        tier = baseline["modules"][module]["tier"]
        out.append(
            _change("removed", f"module:{module}", True, tier, "python", "public module removed")
        )
    for module in sorted(current["modules"].keys() - baseline["modules"].keys()):
        tier = current["modules"][module]["tier"]
        out.append(
            _change("added", f"module:{module}", False, tier, "python", "public module added")
        )
    for key in sorted(baseline["symbols"].keys() - current["symbols"].keys()):
        old = baseline["symbols"][key]
        out.append(
            _change("removed", key, True, old["tier"], "python", f"exported {old['kind']} removed")
        )
    for key in sorted(current["symbols"].keys() - baseline["symbols"].keys()):
        new = current["symbols"][key]
        out.append(
            _change("added", key, False, new["tier"], "python", f"exported {new['kind']} added")
        )
    for key in sorted(baseline["symbols"].keys() & current["symbols"].keys()):
        old, new = baseline["symbols"][key], current["symbols"][key]
        _diff_symbol(key, old, new, old["tier"], out)
    return out


def _diff_shape(path: str, old: Any, new: Any, out: list[Change]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() - new.keys()):
            out.append(_change("removed", f"{path}.{key}", True, "stable", "http", "field removed"))
        for key in sorted(new.keys() - old.keys()):
            out.append(_change("added", f"{path}.{key}", False, "stable", "http", "field added"))
        for key in sorted(old.keys() & new.keys()):
            _diff_shape(f"{path}.{key}", old[key], new[key], out)
    elif isinstance(old, list) and isinstance(new, list):
        old_keys = {json.dumps(v, sort_keys=True): v for v in old}
        new_keys = {json.dumps(v, sort_keys=True): v for v in new}
        if len(old) == 1 and len(new) == 1:
            _diff_shape(f"{path}[]", old[0], new[0], out)
        else:
            for key in sorted(old_keys.keys() - new_keys.keys()):
                out.append(
                    _change("removed", f"{path}[]", True, "stable", "http", f"variant {key[:80]}")
                )
            for key in sorted(new_keys.keys() - old_keys.keys()):
                out.append(
                    _change("added", f"{path}[]", False, "stable", "http", f"variant {key[:80]}")
                )
    elif old != new:
        out.append(_change("changed", path, True, "stable", "http", f"value {old!r} -> {new!r}"))


def _diff_sse(path: str, old: list[Any], new: list[Any], out: list[Change]) -> None:
    old_names = [e["event"] for e in old]
    new_names = [e["event"] for e in new]
    if sorted(set(old_names)) != sorted(set(new_names)) or old_names[:1] != new_names[:1]:
        for name in sorted(set(old_names) - set(new_names)):
            out.append(
                _change(
                    "removed", f"{path}.event({name})", True, "stable", "http", "SSE event removed"
                )
            )
        for name in sorted(set(new_names) - set(old_names)):
            out.append(
                _change(
                    "added", f"{path}.event({name})", False, "stable", "http", "SSE event added"
                )
            )
    if old_names[-1:] != new_names[-1:] and set(old_names) == set(new_names):
        out.append(
            _change("changed", f"{path}.order", True, "stable", "http", "SSE event order changed")
        )
    by_name_old = {e["event"]: e["data"] for e in old}
    by_name_new = {e["event"]: e["data"] for e in new}
    for name in sorted(by_name_old.keys() & by_name_new.keys()):
        _diff_shape(f"{path}.event({name}).data", by_name_old[name], by_name_new[name], out)


def diff_http(baseline: dict[str, Any], current: dict[str, Any]) -> list[Change]:
    out: list[Change] = []
    old_routes = {r["function"]: r for r in baseline["routes"]}
    new_routes = {r["function"]: r for r in current["routes"]}
    for name in sorted(old_routes.keys() - new_routes.keys()):
        out.append(
            _change("removed", f"http:route:{name}", True, "stable", "http", "route removed")
        )
    for name in sorted(new_routes.keys() - old_routes.keys()):
        out.append(_change("added", f"http:route:{name}", False, "stable", "http", "route added"))
    for name in sorted(old_routes.keys() & new_routes.keys()):
        for aspect in ("route", "methods", "auth_level"):
            if old_routes[name][aspect] != new_routes[name][aspect]:
                out.append(
                    _change(
                        "changed",
                        f"http:route:{name}.{aspect}",
                        True,
                        "stable",
                        "http",
                        f"{aspect} {old_routes[name][aspect]!r} -> {new_routes[name][aspect]!r}",
                    )
                )
    for scenario in sorted(baseline["exchanges"].keys() - current["exchanges"].keys()):
        out.append(
            _change("removed", f"http:{scenario}", True, "stable", "http", "scenario missing")
        )
    for scenario in sorted(baseline["exchanges"].keys() & current["exchanges"].keys()):
        old, new = baseline["exchanges"][scenario], current["exchanges"][scenario]
        path = f"http:{scenario}"
        for aspect in ("status", "content_type"):
            if old.get(aspect) != new.get(aspect):
                out.append(
                    _change(
                        "changed",
                        f"{path}.{aspect}",
                        True,
                        "stable",
                        "http",
                        f"{aspect} {old.get(aspect)!r} -> {new.get(aspect)!r}",
                    )
                )
        old_body, new_body = old.get("body", {}), new.get("body", {})
        if "sse" in old_body and "sse" in new_body:
            _diff_sse(f"{path}.body", old_body["sse"], new_body["sse"], out)
        else:
            _diff_shape(f"{path}.body", old_body, new_body, out)
    return out


# ---------------------------------------------------------------------------
# Classification and evaluation
# ---------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"{path.name}: schema_version must be {SCHEMA_VERSION}")
    return data


def _valid_refs(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(r, str) and _REF_RE.match(r) for r in value)
    )


def validate_classifications(data: dict[str, Any]) -> list[str]:
    """Return human-readable problems with the classification record."""
    problems: list[str] = []
    for entry in data.get("changes", []):
        label = entry.get("id", "<missing id>")
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            problems.append("classification entry without id")
        if entry.get("classification") not in CLASSIFICATION_KINDS:
            problems.append(
                f"{label}: classification must be one of {sorted(CLASSIFICATION_KINDS)}"
            )
        if not _valid_refs(entry.get("refs")):
            problems.append(f"{label}: refs must list issue/PR references such as '#879'")
        if not str(entry.get("baseline", "")).strip():
            problems.append(f"{label}: baseline version is required")
        if not str(entry.get("rationale", "")).strip():
            problems.append(f"{label}: rationale is required")
        if (
            entry.get("classification") in {"breaking", "security-breaking"}
            and not str(entry.get("migration", "")).strip()
        ):
            problems.append(f"{label}: migration guidance is required for breaking changes")
    for review in data.get("baseline_reviews", []):
        if not _valid_refs(review.get("refs")):
            problems.append(f"review of {review.get('file')}: refs must reference an issue or PR")
    return problems


def unreviewed_baselines(
    data: dict[str, Any], files: tuple[Path, ...] = BASELINE_FILES
) -> list[str]:
    """Baseline files whose current bytes have no matching review entry.

    This is what stops a snapshot update from being silently approved: the
    review entry (with an issue/PR reference) must be committed together with
    the new baseline and records the exact sha256 that was reviewed.
    """
    reviews = {
        (r.get("file"), r.get("sha256"))
        for r in data.get("baseline_reviews", [])
        if _valid_refs(r.get("refs"))
    }
    return [f.name for f in files if (f.name, file_sha256(f)) not in reviews]


@dataclass
class Report:
    baseline_version: str
    blocking: list[Change] = field(default_factory=list)
    classified: list[tuple[Change, dict[str, Any]]] = field(default_factory=list)
    reported: list[Change] = field(default_factory=list)  # non-blocking tiers
    additive: list[Change] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking and not self.problems


def evaluate(
    changes: list[Change],
    classifications: dict[str, Any],
    baseline_version: str,
    extra_problems: list[str] | None = None,
) -> Report:
    report = Report(baseline_version, problems=[*(extra_problems or [])])
    report.problems.extend(validate_classifications(classifications))
    entries = {
        e["id"]: e
        for e in classifications.get("changes", [])
        if isinstance(e.get("id"), str) and e.get("baseline") == baseline_version
    }
    seen: set[str] = set()
    for change in changes:
        if not change.breaking:
            report.additive.append(change)
        elif change.tier not in BLOCKING_TIERS:
            report.reported.append(change)
        elif change.id in entries:
            seen.add(change.id)
            report.classified.append((change, entries[change.id]))
        else:
            report.blocking.append(change)
    report.stale = sorted(entries.keys() - seen)
    return report


def render_text(report: Report) -> str:
    lines = [f"API contract report (baseline {report.baseline_version})"]

    def section(title: str, items: list[Change]) -> None:
        lines.append(f"\n{title} ({len(items)})")
        for c in items:
            lines.append(f"  [{c.tier}] {c.id}: {c.detail}")

    section("BLOCKING: breaking change without classification", report.blocking)
    lines.append(f"\nCLASSIFIED breaking changes ({len(report.classified)})")
    for c, entry in report.classified:
        lines.append(f"  [{c.tier}] {c.id}: {entry['classification']} {', '.join(entry['refs'])}")
    section("REPORTED breaking changes in non-stable tiers (not blocking)", report.reported)
    section("ADDITIVE changes (note only)", report.additive)
    if report.stale:
        lines.append(
            f"\nSTALE classification entries ({len(report.stale)}): " + ", ".join(report.stale)
        )
    if report.problems:
        lines.append("\nPROBLEMS")
        lines.extend(f"  {p}" for p in report.problems)
    lines.append("\nRESULT: " + ("PASS" if report.ok else "FAIL"))
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    def table(items: list[Change]) -> str:
        if not items:
            return "_none_\n"
        rows = "\n".join(f"| {c.tier} | `{c.id}` | {c.detail} |" for c in items)
        return f"| tier | change | detail |\n| --- | --- | --- |\n{rows}\n"

    parts = [
        f"## API contract gate (baseline {report.baseline_version}): "
        + ("pass" if report.ok else "**FAIL**"),
        f"### Blocking ({len(report.blocking)})\n{table(report.blocking)}",
        f"### Classified breaking ({len(report.classified)})\n"
        + table([c for c, _ in report.classified]),
        f"### Non-blocking tiers, reported ({len(report.reported)})\n{table(report.reported)}",
        f"### Additive ({len(report.additive)})\n{table(report.additive)}",
    ]
    if report.problems:
        parts.append("### Problems\n" + "\n".join(f"- {p}" for p in report.problems))
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Sources: directory or wheel
# ---------------------------------------------------------------------------


def extract_wheel(wheel: Path, target: Path) -> Path:
    """Unpack the package files of *wheel* under *target*, rejecting path escapes."""
    root = target.resolve()
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            name = member.filename
            destination = (root / name).resolve()
            if name.endswith("/") or not (
                destination == root or str(destination).startswith(str(root) + os.sep)
            ):
                if not name.endswith("/"):
                    raise ContractError(f"unsafe path in wheel: {name}")
                continue
            if name.startswith(PACKAGE + "/"):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(member))
    return root


def current_snapshots(source_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    return ApiExtractor(source_root).snapshot(), collect_http_contract(source_root)


def run_check(source_root: Path | None, classifications_path: Path = CLASSIFICATIONS) -> Report:
    api_baseline = load_json(API_BASELINE)
    http_baseline = load_json(HTTP_BASELINE)
    classifications = load_json(classifications_path)
    root = source_root if source_root is not None else REPO_ROOT / "src"
    api_now = ApiExtractor(root).snapshot()
    http_now = collect_http_contract(source_root)
    changes = diff_api(api_baseline, api_now) + diff_http(http_baseline, http_now)
    extra = [
        f"{name}: baseline changed without a matching review entry in classifications.json"
        for name in unreviewed_baselines(classifications)
    ]
    return evaluate(changes, classifications, str(api_baseline["version"]), extra)


def write_baselines(
    source_root: Path, version: str, provenance: dict[str, Any], directory: Path = CONTRACT_DIR
) -> list[Path]:
    api_now, http_now = current_snapshots(source_root)
    header = {"schema_version": SCHEMA_VERSION, "package": "azure-functions-langgraph"}
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in (("api_baseline.json", api_now), ("native_http_baseline.json", http_now)):
        path = directory / name
        document = {**header, "version": version, "provenance": provenance, **payload}
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# Published-wheel verification of the committed baseline
# ---------------------------------------------------------------------------

PACKAGE_DISTRIBUTION = "azure-functions-langgraph"
PYPI_JSON_URL = "https://pypi.org/pypi/{package}/{version}/json"
_WHEEL_HOSTS = frozenset({"files.pythonhosted.org"})
_MAX_WHEEL_BYTES = 25_000_000
_HEADER_KEYS = frozenset({"schema_version", "package", "version", "provenance"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _http_get(url: str, timeout: float, limit: int) -> bytes:
    """Read at most *limit* bytes from an https URL on an allowed host."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in (_WHEEL_HOSTS | {"pypi.org"}):
        raise ContractError(f"refusing to fetch {url!r}: only https PyPI hosts are allowed")
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)
    try:
        connection.request(
            "GET",
            parsed.path + (f"?{parsed.query}" if parsed.query else ""),
            headers={"User-Agent": "api-contract-verify/1"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ContractError(f"{parsed.hostname} answered HTTP {response.status}")
        data = bytes(response.read(limit + 1))
    except http.client.HTTPException as exc:
        raise OSError(f"HTTP error from {parsed.hostname}: {exc}") from exc
    finally:
        connection.close()
    if len(data) > limit:
        raise ContractError(f"response from {parsed.hostname} exceeds {limit} bytes")
    return data


def registry_wheel(
    version: str,
    fetch: Callable[[str, float, int], bytes] = _http_get,
    *,
    timeout: float = 30,
) -> tuple[str, str]:
    """Return ``(url, sha256)`` of the pure-Python wheel the registry lists for *version*."""
    url = PYPI_JSON_URL.format(package=PACKAGE_DISTRIBUTION, version=version)
    try:
        document = json.loads(fetch(url, timeout, 5_000_000))
    except (OSError, ValueError) as exc:
        raise ContractError(f"cannot read registry metadata for {version}: {exc}") from exc
    wheels = [
        entry
        for entry in document.get("urls", [])
        if isinstance(entry, dict) and entry.get("packagetype") == "bdist_wheel"
    ]
    if len(wheels) != 1:
        raise ContractError(
            f"expected exactly one wheel for {version}, registry lists {len(wheels)}"
        )
    digest = str(wheels[0].get("digests", {}).get("sha256", ""))
    if not _SHA256_RE.match(digest) or not isinstance(wheels[0].get("url"), str):
        raise ContractError(f"registry metadata for {version} lacks a valid wheel url or sha256")
    return str(wheels[0]["url"]), digest


def download_published_wheel(
    version: str,
    directory: Path,
    fetch: Callable[[str, float, int], bytes] = _http_get,
    *,
    attempts: int = 4,
    timeout: float = 60,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Path, str]:
    """Download the published wheel, retrying, and verify it against the registry digest."""
    url, expected = registry_wheel(version, fetch)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            data = fetch(url, timeout, _MAX_WHEEL_BYTES)
        except (OSError, ContractError) as exc:
            last = exc
            logging.getLogger(__name__).warning(
                "wheel download attempt %d failed: %s", attempt, exc
            )
            if attempt < attempts:
                sleep(min(2.0**attempt, 20.0))
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ContractError(f"published wheel sha256 {actual} != registry digest {expected}")
        path = directory / url.rsplit("/", 1)[-1]
        path.write_bytes(data)
        return path, expected
    raise ContractError(f"published wheel unreachable after {attempts} attempts: {last}")


def verify_baseline_against_wheel(
    wheel_root: Path, expected_sha256: str, wheel_sha256: str
) -> list[str]:
    """Return problems found comparing the committed baselines with an unpacked wheel."""
    problems: list[str] = []
    api_baseline = load_json(API_BASELINE)
    http_baseline = load_json(HTTP_BASELINE)
    if wheel_sha256 != expected_sha256:
        problems.append(f"wheel sha256 {wheel_sha256} != expected {expected_sha256}")
    recorded = api_baseline.get("provenance", {}).get("published_wheel_sha256")
    if recorded != expected_sha256:
        problems.append(
            f"baseline provenance published_wheel_sha256 {recorded!r} != {expected_sha256}"
        )
    api_now, http_now = current_snapshots(wheel_root)
    for name, baseline, now in (
        ("api_baseline.json", api_baseline, api_now),
        ("native_http_baseline.json", http_baseline, http_now),
    ):
        committed = {k: v for k, v in baseline.items() if k not in _HEADER_KEYS}
        if committed != now:
            problems.append(f"{name} differs from the published wheel")
    for change in diff_api(api_baseline, api_now) + diff_http(http_baseline, http_now):
        problems.append(f"{change.category}: {change.id}")
    return problems


def _cmd_verify_baseline(args: argparse.Namespace) -> int:
    version = str(load_json(API_BASELINE)["version"])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            if args.wheel:
                wheel = Path(args.wheel)
                if not args.expected_sha256:
                    raise ContractError("--wheel needs --expected-sha256")
                expected = args.expected_sha256
            else:
                wheel, expected = download_published_wheel(version, Path(tmp))
            actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
            root = extract_wheel(wheel, Path(tmp) / "unpacked")
            problems = verify_baseline_against_wheel(root, expected, actual)
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if problems:
        print(f"baseline {version} does NOT match the published wheel:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"baseline {version} matches the published wheel (sha256={actual})")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _with_source(args: argparse.Namespace, action: Callable[[Path | None], int]) -> int:
    if args.wheel and args.source:
        print("--wheel and --source are mutually exclusive", file=sys.stderr)
        return 2
    if args.wheel:
        with tempfile.TemporaryDirectory() as tmp:
            return action(extract_wheel(Path(args.wheel), Path(tmp)))
    return action(Path(args.source).resolve() if args.source else None)


def _cmd_check(args: argparse.Namespace) -> int:
    def action(root: Path | None) -> int:
        try:
            report = run_check(root)
        except ContractError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(render_text(report))
        summary = args.summary or os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(render_markdown(report))
        return 0 if report.ok else 1

    return _with_source(args, action)


def _cmd_snapshot(args: argparse.Namespace) -> int:
    provenance: dict[str, Any] = {"kind": args.kind, "ref": args.ref}
    if args.commit:
        provenance["commit"] = args.commit
    if args.published_sha256:
        provenance["published_wheel_sha256"] = args.published_sha256
    if args.note:
        provenance["note"] = args.note

    def action(root: Path | None) -> int:
        if root is None:
            print("snapshot needs --source or --wheel of the release being pinned", file=sys.stderr)
            return 2
        if args.wheel:
            provenance["wheel_sha256"] = hashlib.sha256(Path(args.wheel).read_bytes()).hexdigest()
        for path in write_baselines(root, args.version, provenance):
            print(f"wrote {path} sha256={file_sha256(path)}")
        print(
            "Next: add baseline_reviews entries (with issue/PR refs) for these sha256 values to "
            "tests/contracts/classifications.json; `check` fails until reviewed."
        )
        return 0

    return _with_source(args, action)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("check", _cmd_check), ("snapshot", _cmd_snapshot)):
        p = sub.add_parser(name)
        p.add_argument(
            "--source", help="directory containing the azure_functions_langgraph package"
        )
        p.add_argument("--wheel", help="built wheel to unpack and evaluate")
        p.set_defaults(handler=handler)
        if name == "check":
            p.add_argument("--summary", help="append a Markdown report to this file")
        else:
            p.add_argument("--version", required=True)
            p.add_argument("--kind", default="tag-source")
            p.add_argument("--ref", required=True, help="tag or version the baseline pins")
            p.add_argument("--commit", help="commit SHA of the pinned tag")
            p.add_argument("--published-sha256", help="registry-reported wheel sha256, if known")
            p.add_argument("--note", help="free-form provenance note")
    verify = sub.add_parser(
        "verify-baseline",
        help="re-download the published wheel and compare it with the committed baselines",
    )
    verify.add_argument("--wheel", help="use a local wheel instead of downloading (offline)")
    verify.add_argument("--expected-sha256", help="registry sha256 for --wheel")
    verify.set_defaults(handler=_cmd_verify_baseline)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
