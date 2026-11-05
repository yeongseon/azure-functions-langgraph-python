"""E2E certification Function App for azure-functions-langgraph.

Deployed to real Azure Functions Consumption hosts by the e2e-azure workflow.
Exposes the NATIVE LangGraph routes (no platform_compat):

    GET  /api/health
    POST /api/graphs/e2e_agent/invoke
    POST /api/graphs/e2e_agent/stream

    # Function-key protected twin (#768): proves per-graph auth on the real host and SDK.
    POST /api/graphs/e2e_keyed_agent/invoke

    # Runtime evidence (#768): the interpreter and SDK versions the deployed host really runs.
    GET  /api/e2e/runtime

    # Two-app single-writer exclusivity certification (#386):
    POST /api/graphs/e2e_lock_agent/invoke
    GET  /api/graphs/e2e_lock_agent/threads/{thread_id}/state

The same app payload is deployed to TWO Function Apps (App A and App B) that
share one storage account, one lock Blob container, and one checkpoint Blob
container. Concurrent invokes of ``e2e_lock_agent`` with the same ``thread_id``
must resolve to exactly one writer (the loser receives HTTP 409), proving the
distributed :class:`AzureBlobLeaseThreadLock` + owner-safe release across hosts.
"""

import importlib.metadata
import json
import os
import platform

import azure.functions as func

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

# Anonymous auth so the certification suite can call invoke/stream without
# extracting a function key. This is a throwaway, single-release e2e host with
# no sensitive data; the explicit ANONYMOUS opt-in emits an intended UserWarning.
langgraph_app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
langgraph_app.register(
    graph=compiled_graph,
    name="e2e_agent",
    description="Two-node greeting agent used for real-Azure e2e certification",
)

# Function-key protected twin of e2e_agent (#768). The app itself is ANONYMOUS so the suite needs no
# key; this per-graph FUNCTION override lets the suite prove, on the real host and SDK under test,
# that a keyless request to a protected native route is refused while the anonymous graph is not.
langgraph_app.register(
    graph=compiled_graph,
    name="e2e_keyed_agent",
    description="Function-key protected twin of e2e_agent for auth-behavior certification",
    auth_level=func.AuthLevel.FUNCTION,
)

# ── Distributed-lock exclusivity graph (#386) ──────────────────────────────
# Registered alongside e2e_agent so the existing health/invoke/stream cert path
# is unchanged. Wired with a distributed AzureBlobLeaseThreadLock (shared lock
# container) and a single-writer AzureBlobCheckpointSaver (shared checkpoint
# container). Both are keyed off the SAME app settings on App A and App B so the
# two hosts genuinely contend for one lease. Guarded so a wiring failure never
# breaks the baseline e2e_agent certification.
try:
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import ContainerClient

    from lock_graph import compiled_lock_graph

    from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

    _connection_string = os.environ["AzureWebJobsStorage"]
    _lock_container_name = os.environ.get("E2E_LOCK_CONTAINER", "langgraph-locks")
    _lock_container = ContainerClient.from_connection_string(
        _connection_string, _lock_container_name
    )
    try:
        _lock_container.create_container()  # idempotent — Bicep pre-creates it
    except ResourceExistsError:  # pragma: no cover - depends on live Azure state
        # Container already exists (Bicep pre-created it, or a benign concurrent
        # create). Any other error (auth/RBAC/network) propagates to the outer
        # handler so the wiring failure is logged instead of silently masked.
        pass

    langgraph_app.thread_lock = AzureBlobLeaseThreadLock(container_client=_lock_container)
    langgraph_app.register(
        graph=compiled_lock_graph,
        name="e2e_lock_agent",
        description="Distributed-lock exclusivity graph for two-app e2e certification",
    )
except Exception:  # pragma: no cover - keep baseline e2e_agent cert resilient
    import logging

    logging.getLogger(__name__).exception(
        "e2e_lock_agent wiring failed; continuing with e2e_agent only"
    )

app = langgraph_app.function_app


@app.route(route="e2e/runtime", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def e2e_runtime(req: func.HttpRequest) -> func.HttpResponse:
    """Report the interpreter and SDK the deployed host really runs (#768 evidence).

    The e2e suite asserts the interpreter-split contract on this answer, so a certification run
    proves which ``azure-functions`` line it exercised instead of inferring it from requirements.
    """
    body = {
        "python": platform.python_version(),
        "azure_functions": importlib.metadata.version("azure-functions"),
        "azure_functions_langgraph": importlib.metadata.version("azure-functions-langgraph"),
    }
    return func.HttpResponse(json.dumps(body), mimetype="application/json")
