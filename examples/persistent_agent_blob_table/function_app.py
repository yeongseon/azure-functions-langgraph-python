from __future__ import annotations

import os

import azure.functions as func
from azure.storage.blob import ContainerClient
from graph import build_graph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

_CONN = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
_BLOB_CONTAINER = os.environ.get("LANGGRAPH_BLOB_CONTAINER", "langgraph-checkpoints")
_THREADS_TABLE = os.environ.get("LANGGRAPH_THREADS_TABLE", "langgraphthreads")

container = ContainerClient.from_connection_string(_CONN, _BLOB_CONTAINER)
if not container.exists():
    container.create_container()

checkpointer = AzureBlobCheckpointSaver(container_client=container)
thread_store = AzureTableThreadStore.from_connection_string(
    connection_string=_CONN,
    table_name=_THREADS_TABLE,
)

compiled_graph = build_graph().compile(checkpointer=checkpointer)

langgraph_app = LangGraphApp(
    platform_compat=True,
    auth_level=func.AuthLevel.ANONYMOUS,
)
langgraph_app.thread_store = thread_store
langgraph_app.register(
    graph=compiled_graph,
    name="persistent_agent",
    description="Echo agent persisted with Azure Blob checkpoints + Azure Table threads.",
)

app = langgraph_app.function_app
