"""Test-only topology wiring; no storage I/O is needed to build or reject the app."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import importlib.metadata
import json
import platform
from typing import TypedDict
import uuid

from azure.data.tables import TableClient
import azure.functions as func
from azure.storage.blob import ContainerClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph._topology import build_topology_plan, evaluate_rules
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock, InProcessThreadLock
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore


class State(TypedDict):
    message: str


def echo(state: State) -> State:
    return {"message": state["message"]}


def create_app(environ: Mapping[str, str]) -> LangGraphApp:
    """Swap one final component, after construction, to exercise build-time validation."""
    connection = environ["AzureWebJobsStorage"]
    locks = ContainerClient.from_connection_string(connection, "langgraph-locks")
    checkpoints = ContainerClient.from_connection_string(connection, "langgraph-checkpoints")
    shared = AzureBlobCheckpointSaver(container_client=checkpoints)
    app = LangGraphApp(
        platform_compat=True,
        auth_level=func.AuthLevel.FUNCTION,
        thread_lock=AzureBlobLeaseThreadLock(container_client=locks),
    )
    mode = environ.get("TOPOLOGY_MODE", "safe")
    if mode not in {"store", "warnings"}:
        app.thread_store = AzureTableThreadStore.from_table_client(
            table_client=TableClient.from_connection_string(connection, "topologythreads")
        )
    if mode in {"lock", "warnings"}:
        app.thread_lock = InProcessThreadLock()
    builder = StateGraph(State)
    builder.add_node("echo", echo)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    app.register(
        builder.compile(checkpointer=InMemorySaver() if mode == "checkpointer" else shared),
        "agent",
    )
    if mode == "warnings":
        app.register(builder.compile(checkpointer=InMemorySaver()), "local_agent")
    return app


def export_app(environ: Mapping[str, str]) -> func.FunctionApp:
    """Export only after the package guard succeeds; never catch a refusal."""
    langgraph_app = create_app(environ)
    app = langgraph_app.function_app
    # This is the same private derivation/evaluator used by the package, on the
    # actual final app and actual environment, not fabricated Azure markers.
    plan = build_topology_plan(
        app_label="LangGraphApp",
        app_auth_level=langgraph_app.auth_level,
        platform_compat=langgraph_app.platform_compat,
        async_runs=langgraph_app.async_runs,
        thread_store=langgraph_app.thread_store,
        thread_lock=langgraph_app.thread_lock,
        registrations=langgraph_app._registrations,
        sb_registrations=langgraph_app._sb_registrations,
        environ=environ,
    )
    evidence = {
        "mode": environ.get("TOPOLOGY_MODE", "safe"),
        "boot_id": uuid.uuid4().hex,
        "candidate_sha": environ.get("TOPOLOGY_CANDIDATE_SHA", ""),
        "python": platform.python_version(),
        "package_version": importlib.metadata.version("azure-functions-langgraph"),
        "azure_functions": importlib.metadata.version("azure-functions"),
        "env": asdict(plan.env),
        "host_markers": {
            name: bool(environ.get(name)) for name in ("WEBSITE_INSTANCE_ID", "WEBSITE_SITE_NAME")
        },
        "lock_kind": plan.lock_kind,
        "thread_store_kind": plan.thread_store_kind,
        "findings": [
            {"rule": finding.rule, "severity": finding.severity, "message": finding.message}
            for finding in evaluate_rules(plan)
        ],
    }

    @app.route(route="topology/evidence", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
    def topology_evidence(req: func.HttpRequest) -> func.HttpResponse:
        return func.HttpResponse(json.dumps(evidence), mimetype="application/json")

    return app
