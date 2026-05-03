from __future__ import annotations

import logging
import os

from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableClient
from azure.storage.blob import ContainerClient
from graph import build_graph

import azure.functions as func

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

_BLOB_CONTAINER = os.environ.get("LANGGRAPH_BLOB_CONTAINER", "langgraph-checkpoints")
_THREADS_TABLE = os.environ.get("LANGGRAPH_THREADS_TABLE", "langgraphthreads")

_BLOB_ACCOUNT_URL = os.environ.get("AZURE_STORAGE_BLOB_ACCOUNT_URL")
_TABLE_ENDPOINT = os.environ.get("AZURE_TABLE_ENDPOINT")
_CONN_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")


def _build_storage_clients() -> tuple[ContainerClient, TableClient]:
    # SECURITY: reject partial Managed Identity config. Silently falling back to
    # AZURE_STORAGE_CONNECTION_STRING when only one MI endpoint var is set would
    # mask a broken production MI rollout and quietly re-enable secret-based auth.
    if _BLOB_ACCOUNT_URL or _TABLE_ENDPOINT:
        if not (_BLOB_ACCOUNT_URL and _TABLE_ENDPOINT):
            raise RuntimeError(
                "Partial Managed Identity configuration: set BOTH "
                "AZURE_STORAGE_BLOB_ACCOUNT_URL and AZURE_TABLE_ENDPOINT to use "
                "Managed Identity, or unset both and use "
                "AZURE_STORAGE_CONNECTION_STRING for Azurite/local dev."
            )

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

# LANGGRAPH_AUTO_CREATE_STORAGE bootstraps both the blob container AND the table.
_AUTO_CREATE_STORAGE = (
    os.environ.get("LANGGRAPH_AUTO_CREATE_STORAGE", "false").strip().lower() in _TRUTHY
)

if _AUTO_CREATE_STORAGE:
    try:
        if not container_client.exists():
            container_client.create_container()
    except Exception as exc:
        raise RuntimeError(
            "Failed to verify or create the checkpoint container at cold start. "
            "Pre-create the container and unset LANGGRAPH_AUTO_CREATE_STORAGE, "
            "or check Managed Identity RBAC propagation (Storage Blob Data Contributor)."
        ) from exc

    try:
        table_client.create_table()
    except ResourceExistsError:
        pass
    except Exception as exc:
        # Tolerate transient RBAC propagation / DNS / outage at cold start so a
        # 5-minute role-assignment lag doesn't take down the function. The first
        # real table operation will surface the underlying error if it persists.
        # Pre-create the table and unset LANGGRAPH_AUTO_CREATE_STORAGE in
        # production to fail fast instead.
        logger.warning(
            "Table create skipped at cold start (table=%s): %s. "
            "First real operation may fail if the table is missing.",
            _THREADS_TABLE,
            exc,
        )

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
    description=(
        "Turn-counting echo agent with persistent state via Azure Blob "
        "checkpointer and Azure Table thread store, wired with Managed Identity "
        "in production or connection-string fallback for local Azurite dev."
    ),
)

app = langgraph_app.function_app
