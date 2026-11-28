"""Local durable checkpoint wiring; no graph logic or Functions registration."""

import os

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContainerClient
from upstream_graph import builder

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver

container = ContainerClient.from_connection_string(
    os.environ["AZURE_STORAGE_CONNECTION_STRING"],
    os.environ.get("LANGGRAPH_BLOB_CONTAINER", "official-conversation"),
)
try:
    container.create_container()
except ResourceExistsError:
    pass
checkpointer = AzureBlobCheckpointSaver(container_client=container)
graph = builder.compile(checkpointer=checkpointer)
