"""Platform-compat Functions app for the local host lanes (a test app, not an example).

It wires the topology the package validates when the app is built: an Azure Blob checkpointer, an
Azure Blob lease lock in a container of its own, and an Azure Table thread store, all on the storage
account in ``AzureWebJobsStorage``. ``LANE_CHECKPOINTER=memory``, ``LANE_LOCK=inprocess`` and
``LANE_STORE=memory`` replace one piece with its process-local default, so a lane can show the host
refusing that topology at indexing.
"""

from __future__ import annotations

import operator
import os
from typing import Annotated, Any, TypedDict

from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableClient
import azure.functions as func
from azure.storage.blob import ContainerClient
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore


class ChatState(TypedDict, total=False):
    user_text: str
    history: Annotated[list[str], operator.add]
    turn_count: int
    last_reply: str


def greet(state: ChatState) -> dict[str, Any]:
    text = state.get("user_text", "")
    reply = f"Hello, {text}!" if text else "Hello!"
    return {"history": [reply], "last_reply": reply}


def count(state: ChatState) -> dict[str, Any]:
    return {"turn_count": (state.get("turn_count") or 0) + 1}


_connection = os.environ["AzureWebJobsStorage"]
_checkpoints = ContainerClient.from_connection_string(_connection, "lane-checkpoints")
_locks = ContainerClient.from_connection_string(_connection, "lane-locks")
_table = TableClient.from_connection_string(_connection, "lanethreads")
for _create in (_checkpoints.create_container, _locks.create_container, _table.create_table):
    try:
        _create()
    except ResourceExistsError:
        pass

checkpointer: BaseCheckpointSaver[Any] = (
    InMemorySaver()
    if os.environ.get("LANE_CHECKPOINTER") == "memory"
    else AzureBlobCheckpointSaver(container_client=_checkpoints)
)
builder = StateGraph(ChatState)
builder.add_node("greet", greet)
builder.add_node("count", count)
builder.add_edge(START, "greet")
builder.add_edge("greet", "count")
builder.add_edge("count", END)

if os.environ.get("LANE_LOCK") == "inprocess":
    langgraph_app = LangGraphApp(platform_compat=True, auth_level=func.AuthLevel.ANONYMOUS)
else:
    langgraph_app = LangGraphApp(
        platform_compat=True,
        auth_level=func.AuthLevel.ANONYMOUS,
        thread_lock=AzureBlobLeaseThreadLock(container_client=_locks),
    )
if os.environ.get("LANE_STORE") != "memory":
    langgraph_app.thread_store = AzureTableThreadStore.from_table_client(table_client=_table)
langgraph_app.register(graph=builder.compile(checkpointer=checkpointer), name="agent")
app = langgraph_app.function_app
