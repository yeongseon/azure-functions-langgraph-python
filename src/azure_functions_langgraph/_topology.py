"""Immutable hosting-topology plan and its single build-time validation pass (#880).

``LangGraphApp`` used to scatter its scale-out and auth guards across
``__post_init__`` and ``_build_function_app``. This module derives one frozen
:class:`TopologyPlan` from the final configuration (constructor arguments,
registrations, the thread store/lock actually wired, and the environment) and
evaluates every rule against it in :func:`validate_topology`.

Evidence policy. A rule is a hard **error** only when the configuration is
unsafe *by the operator's own declaration or by construction*:

* mixed per-graph auth on Platform/Durable routes (the routes are protected
  only by the app-level auth level), and
* ``AZFUNC_LANGGRAPH_LOCK_BACKEND`` / ``AZFUNC_LANGGRAPH_THREAD_STORE`` declaring
  a distributed deployment while a process-local lock, thread store or
  checkpointer is wired.

Anything that merely *suggests* scale-out is a **warning**, because deployment
detection cannot prove the instance count: ``WEBSITE_INSTANCE_ID`` and
``WEBSITE_SITE_NAME`` are App Service platform variables that are absent on
Functions-on-container hosts and present on single-instance plans;
``AZURE_FUNCTIONS_ENVIRONMENT`` defaults to ``Production`` when unset, is set to
``Development`` by Core Tools but may be set to anything in Azure, and
``FUNCTIONS_WORKER_RUNTIME`` is set locally too. The existing explicit
environment declarations therefore stay the only hard-guard trigger.

Everything here is private; nothing is exported from the package.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
import warnings

import azure.functions as func
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from azure_functions_langgraph.locks import InProcessThreadLock

_AUTH_RANK: dict[func.AuthLevel, int] = {
    func.AuthLevel.ANONYMOUS: 0,
    func.AuthLevel.FUNCTION: 1,
    func.AuthLevel.ADMIN: 2,
}

# Environment markers set by the Azure Functions host at runtime. Their presence
# suggests (but cannot prove) a real Azure Function App rather than local ``func
# start``. Their absence proves nothing (container hosts), and their presence
# does not prove more than one instance.
AZURE_HOST_MARKERS = ("WEBSITE_INSTANCE_ID", "WEBSITE_SITE_NAME")
LOCK_BACKEND_ENV = "AZFUNC_LANGGRAPH_LOCK_BACKEND"
THREAD_STORE_ENV = "AZFUNC_LANGGRAPH_THREAD_STORE"

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class EnvPlan:
    """Normalized deployment declarations read from the environment."""

    lock_backend: str = ""
    thread_store: str = ""
    azure_hosted: bool = False

    @property
    def distributed_declared(self) -> bool:
        """The operator declared a multi-instance deployment (explicit opt-in)."""
        return (bool(self.lock_backend) and self.lock_backend != "inprocess") or (
            bool(self.thread_store) and self.thread_store != "inmemory"
        )

    @property
    def single_instance_declared(self) -> bool:
        """The operator explicitly declared a single-writer / local deployment."""
        return self.lock_backend == "inprocess" or self.thread_store == "inmemory"


def read_env(environ: Mapping[str, str]) -> EnvPlan:
    """Normalize the declaration variables and host markers from *environ*."""
    return EnvPlan(
        lock_backend=environ.get(LOCK_BACKEND_ENV, "").strip().lower(),
        thread_store=environ.get(THREAD_STORE_ENV, "").strip().lower(),
        azure_hosted=any(environ.get(marker) for marker in AZURE_HOST_MARKERS),
    )


@dataclass(frozen=True)
class GraphPlan:
    """One registered graph as the topology sees it."""

    name: str
    surface: Literal["http", "service_bus"]
    auth_level: func.AuthLevel | None
    stateful: bool
    process_local_checkpointer: bool


@dataclass(frozen=True)
class TopologyPlan:
    """Immutable description of the final, built configuration."""

    app_label: str
    app_auth_level: func.AuthLevel
    platform_compat: bool
    async_runs: Literal[False, "durable"]
    thread_store_kind: Literal["none", "in_memory", "custom"]
    lock_kind: Literal["in_process", "custom"]
    graphs: tuple[GraphPlan, ...]
    env: EnvPlan


@dataclass(frozen=True)
class Finding:
    """One rule outcome: a hard error (raised) or a warning (emitted)."""

    rule: str
    severity: Severity
    message: str
    exc_type: type[Exception] = RuntimeError


def _checkpointer(graph: Any) -> Any:
    checkpointer = getattr(graph, "checkpointer", None)
    # ``True`` / ``False`` are LangGraph's inherit / disable sentinels, not savers.
    return None if isinstance(checkpointer, bool) else checkpointer


def _graph_plan(
    name: str,
    graph: Any,
    surface: Literal["http", "service_bus"],
    auth_level: func.AuthLevel | None,
) -> GraphPlan:
    checkpointer = _checkpointer(graph)
    return GraphPlan(
        name=name,
        surface=surface,
        auth_level=auth_level,
        stateful=isinstance(checkpointer, BaseCheckpointSaver),
        process_local_checkpointer=isinstance(checkpointer, InMemorySaver),
    )


def build_topology_plan(
    *,
    app_label: str,
    app_auth_level: func.AuthLevel,
    platform_compat: bool,
    async_runs: Literal[False, "durable"],
    thread_store: Any,
    thread_lock: Any,
    registrations: Mapping[str, Any],
    sb_registrations: Mapping[str, Any],
    environ: Mapping[str, str],
) -> TopologyPlan:
    """Derive the immutable plan from the app's final configuration."""
    from azure_functions_langgraph.platform.stores import InMemoryThreadStore

    store_kind: Literal["none", "in_memory", "custom"]
    if thread_store is None:
        store_kind = "none"
    elif isinstance(thread_store, InMemoryThreadStore):
        store_kind = "in_memory"
    else:
        store_kind = "custom"
    graphs = tuple(
        _graph_plan(name, reg.graph, "http", reg.auth_level) for name, reg in registrations.items()
    ) + tuple(
        _graph_plan(name, reg.graph, "service_bus", None) for name, reg in sb_registrations.items()
    )
    return TopologyPlan(
        app_label=app_label,
        app_auth_level=app_auth_level,
        platform_compat=platform_compat,
        async_runs=async_runs,
        thread_store_kind=store_kind,
        lock_kind="in_process" if isinstance(thread_lock, InProcessThreadLock) else "custom",
        graphs=graphs,
        env=read_env(environ),
    )


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def optional_route_auth_message(
    name: str,
    graph_level: func.AuthLevel | None,
    app_level: func.AuthLevel,
    *,
    platform_compat: bool,
    async_runs: Literal[False, "durable"],
    late: bool = False,
) -> str | None:
    """Return the #854 mixed-auth error text, or ``None`` when the graph is safe."""
    surfaces: list[str] = []
    if platform_compat:
        surfaces.append("platform_compat=True")
    if async_runs == "durable":
        surfaces.append('async_runs="durable"')
    if not surfaces or graph_level is None:
        return None
    if _AUTH_RANK[graph_level] <= _AUTH_RANK[app_level]:
        return None
    when = (
        " Registering it after the optional routes were already exported would let "
        "the exported handlers run it without its host key."
        if late
        else ""
    )
    return (
        f"Graph {name!r} requires auth_level={graph_level.name} but "
        f"{' and '.join(surfaces)} expose it through routes protected only by "
        f"the app-level auth_level={app_level.name}, which would bypass "
        f"the graph's host key.{when} Raise the app-level auth_level to at least "
        f"{graph_level.name}, relax the graph's auth_level, or disable the "
        "optional routes."
    )


def lock_backend_message(app_label: str, env_backend: str) -> str:
    """Error text for a declared distributed lock backed by ``InProcessThreadLock``."""
    return (
        f"{LOCK_BACKEND_ENV}={env_backend!r} requires a "
        "distributed thread_lock backend (for example "
        "AzureBlobLeaseThreadLock), but the app is still using the "
        "default InProcessThreadLock. Pass thread_lock=... explicitly "
        f"to {app_label}() or unset the env var for local dev."
    )


def check_declared_lock_backend(
    app_label: str, thread_lock: Any, environ: Mapping[str, str]
) -> None:
    """Fail fast when a distributed lock is declared but ``InProcessThreadLock`` is wired."""
    env_backend = read_env(environ).lock_backend
    if env_backend and env_backend != "inprocess" and isinstance(thread_lock, InProcessThreadLock):
        raise RuntimeError(lock_backend_message(app_label, env_backend))


_STORE_DECLARED_MESSAGE = (
    "The configured scale-out safety guard requires a "
    "distributed thread store (for example AzureTableThreadStore), "
    "but the final configured store is InMemoryThreadStore. Set "
    "app.thread_store = ... before accessing app.function_app, or "
    "unset AZFUNC_LANGGRAPH_THREAD_STORE and "
    "AZFUNC_LANGGRAPH_LOCK_BACKEND for local dev."
)

_STORE_HOSTED_MESSAGE = (
    "platform_compat is enabled with the final configured "
    "in-memory thread store (InMemoryThreadStore) on an "
    "Azure-hosted Function App. "
    "InMemoryThreadStore is per-instance, so thread state will "
    "diverge across scaled-out instances.\n"
    "  Recommended: app.thread_store = AzureTableThreadStore(...)\n"
    "  Fail-fast:   set AZFUNC_LANGGRAPH_THREAD_STORE=distributed\n"
    "  See the 'Distributed thread locking' scale-out matrix in "
    "docs/production-guide.md"
)


def _surfaces(plan: TopologyPlan, graph: GraphPlan) -> str:
    names = ["Service Bus" if graph.surface == "service_bus" else "native HTTP"]
    if graph.surface == "http" and plan.platform_compat:
        names.append("Platform")
    if graph.surface == "http" and plan.async_runs == "durable":
        names.append("Durable")
    return "/".join(names)


def _process_local_names(plan: TopologyPlan) -> list[str]:
    return [g.name for g in plan.graphs if g.process_local_checkpointer]


def evaluate_rules(plan: TopologyPlan) -> list[Finding]:
    """Evaluate every topology rule in a fixed order (errors first, then warnings)."""
    findings: list[Finding] = []
    env = plan.env

    # R1 mixed-auth (#854): Platform/Durable routes ignore per-graph auth.
    for graph in plan.graphs:
        if graph.surface != "http":
            continue
        message = optional_route_auth_message(
            graph.name,
            graph.auth_level,
            plan.app_auth_level,
            platform_compat=plan.platform_compat,
            async_runs=plan.async_runs,
        )
        if message:
            findings.append(Finding("mixed-auth", "error", message, ValueError))

    # R2 declared distributed lock with the process-local default lock.
    if env.lock_backend and env.lock_backend != "inprocess" and plan.lock_kind == "in_process":
        findings.append(
            Finding(
                "lock-backend-declared",
                "error",
                lock_backend_message(plan.app_label, env.lock_backend),
            )
        )

    # R3 declared distributed deployment with the per-instance Platform store.
    if plan.platform_compat and plan.thread_store_kind == "in_memory":
        if env.distributed_declared:
            findings.append(Finding("thread-store-declared", "error", _STORE_DECLARED_MESSAGE))

    # R4 declared distributed deployment with a process-local checkpointer.
    local = _process_local_names(plan)
    if local and env.distributed_declared:
        findings.append(
            Finding(
                "process-local-checkpointer-declared",
                "error",
                f"Graph(s) {', '.join(repr(n) for n in local)} use a process-local "
                "InMemorySaver checkpointer, but AZFUNC_LANGGRAPH_LOCK_BACKEND / "
                "AZFUNC_LANGGRAPH_THREAD_STORE declares a distributed deployment. "
                "Checkpoints would diverge across instances and workers. Use a shared "
                "checkpointer (for example AzureBlobCheckpointSaver), or declare the "
                "single-instance deployment with AZFUNC_LANGGRAPH_LOCK_BACKEND=inprocess "
                "and AZFUNC_LANGGRAPH_THREAD_STORE=inmemory (or unset both) for local dev.",
            )
        )

    if not env.azure_hosted:
        return findings

    # W1 hosted Platform run on the per-instance store (unchanged text).
    if plan.platform_compat and plan.thread_store_kind == "in_memory":
        findings.append(Finding("thread-store-hosted", "warning", _STORE_HOSTED_MESSAGE))
    if env.single_instance_declared:
        return findings

    # W2 hosted + process-local checkpointer (instance count cannot be proven).
    if local:
        findings.append(
            Finding(
                "process-local-checkpointer-hosted",
                "warning",
                f"Graph(s) {', '.join(repr(n) for n in local)} use a process-local "
                "InMemorySaver checkpointer on an Azure-hosted Function App. Checkpoints "
                "are lost on restart and diverge across scaled-out instances. Use a "
                "shared checkpointer, or set AZFUNC_LANGGRAPH_LOCK_BACKEND=inprocess "
                "to declare a single-instance deployment.",
            )
        )

    # W3 hosted + stateful graphs serialized only by the process-local lock.
    if plan.lock_kind == "in_process":
        stateful = [g for g in plan.graphs if g.stateful and not g.process_local_checkpointer]
        if stateful:
            detail = "; ".join(f"{g.name!r} ({_surfaces(plan, g)})" for g in stateful)
            findings.append(
                Finding(
                    "inprocess-lock-hosted",
                    "warning",
                    f"Checkpointed graph(s) {detail} are serialized only by the default "
                    "process-local InProcessThreadLock on an Azure-hosted Function App. "
                    "Concurrent writers on other instances or worker processes are not "
                    "excluded. Pass thread_lock=AzureBlobLeaseThreadLock(...), or set "
                    "AZFUNC_LANGGRAPH_LOCK_BACKEND=inprocess if exactly one process writes "
                    "each thread. See docs/production-guide.md, 'Topology validation'.",
                )
            )
    return findings


def enforce_topology(plan: TopologyPlan, *, stacklevel: int = 3) -> None:
    """Run the single validation pass: raise the first error, otherwise warn.

    Executes before any route exists, so a rejected configuration serves no
    request and runs no graph.
    """
    findings = evaluate_rules(plan)
    for finding in findings:
        if finding.severity == "error":
            raise finding.exc_type(finding.message)
    for finding in findings:
        warnings.warn(finding.message, RuntimeWarning, stacklevel=stacklevel)
