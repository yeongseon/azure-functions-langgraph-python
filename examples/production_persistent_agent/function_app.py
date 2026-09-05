"""Deploy the production persistent Azure OpenAI agent as Azure Functions.

Wiring responsibilities live **here**, not in ``graph.py``:

* Build an Azure Blob ``ContainerClient`` — Managed Identity
  (``DefaultAzureCredential``) in production, connection string (Azurite) for
  local dev — using the same partial-config guard as
  [`managed_identity_storage`](../managed_identity_storage/).
* Wrap it in ``AzureBlobCheckpointSaver`` so graph state is durable and
  Azure-native.
* Compile the graph *with* that checkpointer and register it.

The native invoke/stream/state endpoints alone deliver persistent per-``thread_id``
state — no Platform layer required. Platform compatibility (``platform_compat``
and ``AzureTableThreadStore``) is an **optional** add-on, toggled by
``LANGGRAPH_ENABLE_PLATFORM``, for teams that also want SDK-compatible
thread/run/assistant endpoints.
"""

from __future__ import annotations

import logging
import os

from azure.storage.blob import ContainerClient
from graph import build_graph

import azure.functions as func

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

_BLOB_CONTAINER = os.environ.get("LANGGRAPH_BLOB_CONTAINER", "langgraph-checkpoints")

_BLOB_ACCOUNT_URL = os.environ.get("AZURE_STORAGE_BLOB_ACCOUNT_URL")
_CONN_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")

# Optional Platform-compatibility layer (SDK-compatible thread/run/assistant
# endpoints). Native persistence works WITHOUT this — see the README.
_ENABLE_PLATFORM = os.environ.get("LANGGRAPH_ENABLE_PLATFORM", "false").strip().lower() in _TRUTHY
_TABLE_ENDPOINT = os.environ.get("AZURE_TABLE_ENDPOINT")
_THREADS_TABLE = os.environ.get("LANGGRAPH_THREADS_TABLE", "langgraphthreads")


def _build_container_client() -> ContainerClient:
    """Return a Blob ``ContainerClient`` for checkpoint persistence.

    Managed Identity when ``AZURE_STORAGE_BLOB_ACCOUNT_URL`` is set (production),
    otherwise a connection string (Azurite / local dev).
    """
    if _BLOB_ACCOUNT_URL:
        # SECURITY: a Managed Identity account URL is present, so never silently
        # fall back to a connection-string secret — that would mask a broken MI
        # rollout and quietly re-enable secret-based auth.
        from azure.identity import DefaultAzureCredential

        return ContainerClient(
            account_url=_BLOB_ACCOUNT_URL,
            container_name=_BLOB_CONTAINER,
            credential=DefaultAzureCredential(),
        )

    if not _CONN_STRING:
        raise RuntimeError(
            "Set AZURE_STORAGE_BLOB_ACCOUNT_URL for Managed Identity, or "
            "AZURE_STORAGE_CONNECTION_STRING for Azurite/local dev."
        )

    return ContainerClient.from_connection_string(_CONN_STRING, _BLOB_CONTAINER)


container_client = _build_container_client()

# LANGGRAPH_AUTO_CREATE_STORAGE bootstraps the checkpoint container at cold
# start. Convenient for Azurite/local dev; in production pre-create the
# container instead and leave this unset (see README).
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

checkpointer = AzureBlobCheckpointSaver(container_client=container_client)

# Compile the SAME graph definition WITH durable persistence attached.
compiled_graph = build_graph().compile(checkpointer=checkpointer)

langgraph_app = LangGraphApp(
    platform_compat=_ENABLE_PLATFORM,
    auth_level=func.AuthLevel.FUNCTION,
    # Explicitly protect the health-details inventory in production.
    health_auth_level=func.AuthLevel.FUNCTION,
)

# Optional Platform layer: attach an Azure Table thread store so the
# SDK-compatible run/thread endpoints have durable metadata + run locking.
if _ENABLE_PLATFORM:
    from azure.data.tables import TableClient

    from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

    if _TABLE_ENDPOINT:
        from azure.identity import DefaultAzureCredential

        table_client = TableClient(
            endpoint=_TABLE_ENDPOINT,
            table_name=_THREADS_TABLE,
            credential=DefaultAzureCredential(),
        )
    elif _CONN_STRING:
        table_client = TableClient.from_connection_string(_CONN_STRING, _THREADS_TABLE)
    else:
        raise RuntimeError(
            "LANGGRAPH_ENABLE_PLATFORM=true requires AZURE_TABLE_ENDPOINT "
            "(Managed Identity) or AZURE_STORAGE_CONNECTION_STRING (Azurite)."
        )

    if _AUTO_CREATE_STORAGE:
        from azure.core.exceptions import ResourceExistsError

        try:
            table_client.create_table()
        except ResourceExistsError:
            pass
        except Exception as exc:  # pragma: no cover - transient cold-start tolerance
            logger.warning(
                "Table create skipped at cold start (table=%s): %s.",
                _THREADS_TABLE,
                exc,
            )

    langgraph_app.thread_store = AzureTableThreadStore.from_table_client(table_client=table_client)

langgraph_app.register(
    graph=compiled_graph,
    name="production_persistent_agent",
    description=(
        "Production Azure OpenAI chat agent with durable per-thread memory via "
        "an Azure Blob checkpointer, wired with Managed Identity in production "
        "and a connection-string fallback for local Azurite dev."
    ),
)

app = langgraph_app.function_app
