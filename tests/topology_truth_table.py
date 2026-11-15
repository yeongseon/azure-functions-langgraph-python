"""Truth table of hosting-topology outcomes (#880), shared by tests and docs.

``ROWS`` is the single source: ``tests/test_topology_truth_table.py`` executes
every row against a real app build, and renders the same rows into the
``topology-truth-table`` section of ``docs/production-guide.md`` (a test fails
when the document drifts).
"""

from __future__ import annotations

from dataclasses import dataclass
import re

START = "<!-- topology-truth-table:start -->"
END = "<!-- topology-truth-table:end -->"

# Rule id -> regex identifying its message. Independent of the implementation's
# Finding.rule so the table proves the user-visible text, not an internal label.
RULE_PATTERNS: dict[str, re.Pattern[str]] = {
    "mixed-auth": re.compile(r"routes protected only by the app-level auth_level"),
    "lock-backend-declared": re.compile(r"requires a distributed thread_lock backend"),
    "thread-store-declared": re.compile(r"requires a distributed thread store"),
    "process-local-checkpointer-declared": re.compile(
        r"process-local InMemorySaver checkpointer, but AZFUNC"
    ),
    "thread-store-hosted": re.compile(r"InMemoryThreadStore\) on an Azure-hosted"),
    "process-local-checkpointer-hosted": re.compile(
        r"process-local InMemorySaver checkpointer on an Azure-hosted"
    ),
    "inprocess-lock-hosted": re.compile(r"serialized only by the default process-local"),
}


@dataclass(frozen=True)
class Row:
    surface: str  # native | platform | streaming | durable | service_bus
    hosting: str  # local | hosted
    declaration: str  # none | lock=distributed | store=distributed | inprocess
    checkpointer: str  # none | shared | process-local
    lock: str  # in-process | distributed
    store: str  # n/a | in-memory | distributed
    auth: str  # uniform | mixed
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _r(
    surface: str,
    hosting: str,
    declaration: str,
    checkpointer: str,
    lock: str,
    store: str,
    auth: str,
    outcome: str = "ok",
) -> Row:
    if outcome == "ok":
        return Row(surface, hosting, declaration, checkpointer, lock, store, auth)
    kind, _, rules = outcome.partition(":")
    names = tuple(rules.split(","))
    if kind == "error":
        return Row(surface, hosting, declaration, checkpointer, lock, store, auth, errors=names)
    return Row(surface, hosting, declaration, checkpointer, lock, store, auth, warnings=names)


IP, DL = "in-process", "distributed"
ROWS: tuple[Row, ...] = (
    # --- Native HTTP -------------------------------------------------------
    _r("native", "local", "none", "none", IP, "n/a", "uniform"),
    _r("native", "local", "none", "shared", IP, "n/a", "uniform"),
    _r("native", "local", "none", "process-local", IP, "n/a", "uniform"),
    _r("native", "hosted", "none", "none", IP, "n/a", "uniform"),
    _r("native", "hosted", "none", "shared", IP, "n/a", "uniform", "warn:inprocess-lock-hosted"),
    _r("native", "hosted", "none", "shared", DL, "n/a", "uniform"),
    _r(
        "native",
        "hosted",
        "none",
        "process-local",
        IP,
        "n/a",
        "uniform",
        "warn:process-local-checkpointer-hosted",
    ),
    _r("native", "hosted", "inprocess", "shared", IP, "n/a", "uniform"),
    _r("native", "hosted", "inprocess", "process-local", IP, "n/a", "uniform"),
    _r(
        "native",
        "local",
        "lock=distributed",
        "none",
        IP,
        "n/a",
        "uniform",
        "error:lock-backend-declared",
    ),
    _r("native", "local", "lock=distributed", "shared", DL, "n/a", "uniform"),
    _r(
        "native",
        "local",
        "lock=distributed",
        "process-local",
        DL,
        "n/a",
        "uniform",
        "error:process-local-checkpointer-declared",
    ),
    _r("native", "local", "store=distributed", "shared", IP, "n/a", "uniform"),
    _r("native", "hosted", "none", "shared", IP, "n/a", "mixed", "warn:inprocess-lock-hosted"),
    # --- Platform ----------------------------------------------------------
    _r("platform", "local", "none", "shared", IP, "in-memory", "uniform"),
    _r(
        "platform", "hosted", "none", "none", IP, "in-memory", "uniform", "warn:thread-store-hosted"
    ),
    _r(
        "platform",
        "hosted",
        "none",
        "shared",
        IP,
        "in-memory",
        "uniform",
        "warn:thread-store-hosted,inprocess-lock-hosted",
    ),
    _r("platform", "hosted", "none", "shared", DL, "distributed", "uniform"),
    _r(
        "platform",
        "hosted",
        "none",
        "shared",
        IP,
        "distributed",
        "uniform",
        "warn:inprocess-lock-hosted",
    ),
    _r(
        "platform",
        "local",
        "lock=distributed",
        "shared",
        DL,
        "in-memory",
        "uniform",
        "error:thread-store-declared",
    ),
    _r(
        "platform",
        "local",
        "store=distributed",
        "none",
        IP,
        "in-memory",
        "uniform",
        "error:thread-store-declared",
    ),
    _r(
        "platform",
        "local",
        "store=distributed",
        "process-local",
        IP,
        "distributed",
        "uniform",
        "error:process-local-checkpointer-declared",
    ),
    _r("platform", "local", "none", "shared", IP, "in-memory", "mixed", "error:mixed-auth"),
    _r(
        "platform",
        "local",
        "store=distributed",
        "shared",
        IP,
        "in-memory",
        "mixed",
        "error:mixed-auth",
    ),
    # --- Durable -----------------------------------------------------------
    _r("durable", "local", "none", "shared", IP, "n/a", "uniform"),
    _r(
        "durable",
        "hosted",
        "none",
        "process-local",
        IP,
        "n/a",
        "uniform",
        "warn:process-local-checkpointer-hosted",
    ),
    _r("durable", "hosted", "none", "shared", DL, "n/a", "uniform"),
    _r(
        "durable",
        "local",
        "lock=distributed",
        "shared",
        IP,
        "n/a",
        "uniform",
        "error:lock-backend-declared",
    ),
    _r(
        "durable",
        "local",
        "store=distributed",
        "process-local",
        IP,
        "n/a",
        "uniform",
        "error:process-local-checkpointer-declared",
    ),
    _r("durable", "local", "store=distributed", "shared", IP, "n/a", "uniform"),
    _r("durable", "local", "none", "shared", IP, "n/a", "mixed", "error:mixed-auth"),
    # --- Service Bus -------------------------------------------------------
    _r("service_bus", "local", "none", "shared", IP, "n/a", "uniform"),
    _r("service_bus", "hosted", "none", "none", IP, "n/a", "uniform"),
    _r(
        "service_bus",
        "hosted",
        "none",
        "shared",
        IP,
        "n/a",
        "uniform",
        "warn:inprocess-lock-hosted",
    ),
    _r(
        "service_bus",
        "hosted",
        "none",
        "process-local",
        IP,
        "n/a",
        "uniform",
        "warn:process-local-checkpointer-hosted",
    ),
    _r(
        "service_bus",
        "local",
        "lock=distributed",
        "process-local",
        DL,
        "n/a",
        "uniform",
        "error:process-local-checkpointer-declared",
    ),
    # --- Streaming (StreamingLangGraphApp) --------------------------------
    _r("streaming", "local", "none", "shared", IP, "n/a", "uniform"),
    _r("streaming", "hosted", "none", "shared", IP, "n/a", "uniform", "warn:inprocess-lock-hosted"),
    _r(
        "streaming",
        "local",
        "lock=distributed",
        "none",
        IP,
        "n/a",
        "uniform",
        "error:lock-backend-declared",
    ),
    _r(
        "streaming",
        "local",
        "store=distributed",
        "process-local",
        IP,
        "n/a",
        "uniform",
        "error:process-local-checkpointer-declared",
    ),
    _r("streaming", "hosted", "inprocess", "process-local", IP, "n/a", "uniform"),
)


def outcome_text(row: Row) -> str:
    if row.errors:
        return "**Error** " + ", ".join(f"`{n}`" for n in row.errors)
    if row.warnings:
        return "Warning " + ", ".join(f"`{n}`" for n in row.warnings)
    return "OK"


def render_table() -> str:
    header = (
        "| Surface | Hosting | Declaration | Checkpointer | Lock | Platform store | Auth "
        "| Outcome |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    lines = [
        f"| {r.surface} | {r.hosting} | {r.declaration} | {r.checkpointer} | {r.lock} | "
        f"{r.store} | {r.auth} | {outcome_text(r)} |"
        for r in ROWS
    ]
    return "\n".join([START, header, *lines, END])
