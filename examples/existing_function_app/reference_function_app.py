"""Opt-in Flex recipe; stage as function_app.py rather than changing the default example."""

from __future__ import annotations

from importlib.metadata import version
import json
import logging
import os
import platform
import re
from typing import Final
from uuid import UUID, uuid4

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient
from graph import State, builder
from langchain_core.messages import HumanMessage, convert_to_messages, messages_to_dict
from langchain_core.runnables import RunnableConfig
from reference_policy import BlobOwnerPolicy, verified_owner

from azure_functions_langgraph import LangGraphApp, LoggingRunObserver
from azure_functions_langgraph.authorization import (
    AuthorizationAction,
    AuthorizationContext,
    UnauthenticatedError,
    authorization_status,
)
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

container = ContainerClient(
    account_url=os.environ["AZURE_STORAGE_BLOB_ACCOUNT_URL"],
    container_name=os.environ["LANGGRAPH_BLOB_CONTAINER"],
    credential=DefaultAzureCredential(),
)
policy = BlobOwnerPolicy(container, UUID(os.environ["REFERENCE_TENANT"]))
compiled = builder.compile(checkpointer=AzureBlobCheckpointSaver(container_client=container))
thread_lock = AzureBlobLeaseThreadLock(container_client=container)
logging.getLogger("azure_functions_langgraph.observability.run").setLevel(logging.INFO)
langgraph_app = LangGraphApp(
    authorize=policy,
    observer=LoggingRunObserver(),
    thread_lock=thread_lock,
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


@app.route(route="reference/runtime", methods=["GET", "POST"], auth_level=func.AuthLevel.FUNCTION)
def reference_runtime(req: func.HttpRequest) -> func.HttpResponse:
    """Return protected inventory or an owner-authorized, process-correlated checkpoint probe."""
    try:
        owner = verified_owner(req)
    except UnauthenticatedError:
        return func.HttpResponse(status_code=401)
    if owner.tenant != policy.tenant:
        return func.HttpResponse(status_code=403)
    thread = req.params.get("restart_thread")
    if thread is not None:
        if not re.fullmatch(r"restart-[a-f0-9]{32}", thread):
            return func.HttpResponse(status_code=400)
        action = AuthorizationAction.RUN if req.method == "POST" else AuthorizationAction.STATE_READ
        status = authorization_status(
            policy, AuthorizationContext(action, "official_basic", thread, req)
        )
        if status is not None:
            return func.HttpResponse(status_code=status)
        config: RunnableConfig = {"configurable": {"thread_id": thread}}
        if req.method == "POST":
            token = thread_lock.acquire("official_basic", thread)
            if token is None:
                return func.HttpResponse(status_code=409)
            try:
                probe_input: State = {
                    "messages": [HumanMessage(content="before")],
                    "extra_field": 10,
                }
                compiled.invoke(probe_input, config=config)
            finally:
                thread_lock.release("official_basic", thread, token)
        snapshot = compiled.get_state(config)
        return func.HttpResponse(
            json.dumps(
                {
                    "process_token": _PROCESS_TOKEN,
                    "values": {
                        "messages": messages_to_dict(
                            convert_to_messages(snapshot.values.get("messages", []))
                        )
                    },
                }
            ),
            mimetype="application/json",
            headers={"Cache-Control": "no-store"},
        )
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
