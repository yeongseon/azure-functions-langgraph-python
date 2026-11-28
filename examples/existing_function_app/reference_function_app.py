"""Opt-in Flex recipe; stage as function_app.py rather than changing the default example."""

from __future__ import annotations

from importlib.metadata import version
import json
import logging
import os
import platform
from typing import Final
from uuid import UUID, uuid4

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient
from graph import builder
from reference_policy import BlobOwnerPolicy, verified_owner

from azure_functions_langgraph import LangGraphApp, LoggingRunObserver
from azure_functions_langgraph.authorization import UnauthenticatedError
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

container = ContainerClient(
    account_url=os.environ["AZURE_STORAGE_BLOB_ACCOUNT_URL"],
    container_name=os.environ["LANGGRAPH_BLOB_CONTAINER"],
    credential=DefaultAzureCredential(),
)
policy = BlobOwnerPolicy(container, UUID(os.environ["REFERENCE_TENANT"]))
compiled = builder.compile(checkpointer=AzureBlobCheckpointSaver(container_client=container))
logging.getLogger("azure_functions_langgraph.observability.run").setLevel(logging.INFO)
langgraph_app = LangGraphApp(
    authorize=policy,
    observer=LoggingRunObserver(),
    thread_lock=AzureBlobLeaseThreadLock(container_client=container),
)
langgraph_app.register(graph=compiled, name="official_basic")
app = func.FunctionApp()
_PROCESS_TOKEN: Final = uuid4().hex


@app.route(route="existing", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def existing_http(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("existing HTTP still works")


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False)
def existing_timer(timer: func.TimerRequest) -> None:
    """Retain the non-startup timer of the existing application."""


@app.route(route="reference/runtime", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def reference_runtime(req: func.HttpRequest) -> func.HttpResponse:
    """Return inventory and a private worker token, never identity claims or checkpoints."""
    try:
        owner = verified_owner(req)
    except UnauthenticatedError:
        return func.HttpResponse(status_code=401)
    if owner.tenant != policy.tenant:
        return func.HttpResponse(status_code=403)
    return func.HttpResponse(
        json.dumps(
            {
                "process_token": _PROCESS_TOKEN,
                "python": platform.python_version(),
                "adapter": version("azure-functions-langgraph"),
                "functions": version("azure-functions"),
                "langgraph": version("langgraph"),
                "blob": version("azure-storage-blob"),
                "identity": version("azure-identity"),
            }
        ),
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


app.register_functions(langgraph_app.to_blueprint())
