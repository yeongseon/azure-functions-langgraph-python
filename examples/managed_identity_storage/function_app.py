from __future__ import annotations

import os

from azure.data.tables import TableClient
from azure.storage.blob import ContainerClient
from graph import build_graph

import azure.functions as func

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

_BLOB_CONTAINER = os.environ.get("LANGGRAPH_BLOB_CONTAINER", "langgraph-checkpoints")
_THREADS_TABLE = os.environ.get("LANGGRAPH_THREADS_TABLE", "langgraphthreads")

_BLOB_ACCOUNT_URL = os.environ.get("AZURE_STORAGE_BLOB_ACCOUNT_URL")
_TABLE_ENDPOINT = os.environ.get("AZURE_TABLE_ENDPOINT")
_CONN_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")


def _build_storage_clients() -> tuple[ContainerClient, TableClient]:
    if _BLOB_ACCOUNT_URL and _TABLE_ENDPOINT:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        container = ContainerClient(
            account_url=_BLOB_ACCOUNT_URL,
            container_name=_BLOB_CONTAINER,
            credential=credential,
        )
        table = TableClient(
            endpoint=_TABLE_ENDPOINT,
            table_name=_THREADS_TABLE,
            credential=credential,
        )
        return container, table

    if not _CONN_STRING:
        raise RuntimeError(
            "Set AZURE_STORAGE_BLOB_ACCOUNT_URL + AZURE_TABLE_ENDPOINT for Managed "
            "Identity, or AZURE_STORAGE_CONNECTION_STRING for Azurite/local dev."
        )

    container = ContainerClient.from_connection_string(_CONN_STRING, _BLOB_CONTAINER)
    table = TableClient.from_connection_string(_CONN_STRING, _THREADS_TABLE)
    return container, table


container_client, table_client = _build_storage_clients()

if not container_client.exists():
    container_client.create_container()

checkpointer = AzureBlobCheckpointSaver(container_client=container_client)
thread_store = AzureTableThreadStore.from_table_client(table_client=table_client)

compiled_graph = build_graph().compile(checkpointer=checkpointer)

langgraph_app = LangGraphApp(
    platform_compat=True,
    auth_level=func.AuthLevel.FUNCTION,
)
langgraph_app.thread_store = thread_store
langgraph_app.register(
    graph=compiled_graph,
    name="managed_identity_agent",
    description="Echo agent persisted via DefaultAzureCredential (Managed Identity in prod, AzureCliCredential in dev).",
)

app = langgraph_app.function_app
