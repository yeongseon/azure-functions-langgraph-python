"""Durable async-run e2e Function App (#811). OPTIONAL lane, not a release gate.

Deployed by ``.github/workflows/e2e-azure-durable.yml`` (dispatch only). Exposes the native
Durable lifecycle routes and a small evidence endpoint:

    POST /api/graphs/e2e_durable/runs          create (202)
    GET  /api/runs/{run_id}                    poll
    POST /api/runs/{run_id}/cancel             request termination
    GET  /api/e2e/health
    GET  /api/e2e/runtime
    GET  /api/e2e/evidence/{run_id}            attempt / node-entry / completion / effect evidence

The app is ANONYMOUS because it is a throwaway host holding no sensitive data (as in
``examples/e2e_app``). Evidence endpoints expose counts and short instance tags only.
"""

import contextlib
import importlib.metadata
import json
import os
import platform

from azure.core.exceptions import ResourceExistsError
import azure.functions as func
from azure.storage.blob import ContainerClient
from graph import (
    EvidenceObserver,
    build_graph,
    count_markers,
    effect_applied_count,
    marker_times_ms,
)

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

_lock_container = ContainerClient.from_connection_string(
    os.environ["AzureWebJobsStorage"], os.environ.get("E2E_LOCK_CONTAINER", "langgraph-locks")
)
with contextlib.suppress(ResourceExistsError):  # Bicep pre-creates it; other errors propagate
    _lock_container.create_container()

langgraph_app = LangGraphApp(
    auth_level=func.AuthLevel.ANONYMOUS, async_runs="durable", observer=EvidenceObserver()
)
langgraph_app.thread_lock = AzureBlobLeaseThreadLock(container_client=_lock_container)
langgraph_app.register(graph=build_graph(), name="e2e_durable")

app = langgraph_app.function_app


def _json(body: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json")


@app.route(route="e2e/health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def e2e_health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({"status": "ok"})


@app.route(route="e2e/runtime", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def e2e_runtime(req: func.HttpRequest) -> func.HttpResponse:
    return _json(
        {
            "python": platform.python_version(),
            "azure_functions": importlib.metadata.version("azure-functions"),
            "azure_functions_durable": importlib.metadata.version("azure-functions-durable"),
            "azure_functions_langgraph": importlib.metadata.version("azure-functions-langgraph"),
        }
    )


@app.route(route="e2e/evidence/{run_id}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def e2e_evidence(req: func.HttpRequest) -> func.HttpResponse:
    run_id = req.route_params["run_id"]
    return _json(
        {
            "run_id": run_id,
            # Activity deliveries (started + rejected); node entries are counted separately.
            "attempts": count_markers("attempt", run_id),
            "started": count_markers("started", run_id),
            "rejected": count_markers("rejected", run_id),
            "node_entries": count_markers("node-enter", run_id),
            "hold_finished": count_markers("hold-finished", run_id),
            # Emitted after graph invocation returned (effect + checkpointing done).
            "completions": count_markers("activity-finished", run_id),
            "completion_times_ms": marker_times_ms("activity-finished", run_id),
            "effect_calls": count_markers("effect-call", run_id),
            "effects_applied": effect_applied_count(run_id),
        }
    )
