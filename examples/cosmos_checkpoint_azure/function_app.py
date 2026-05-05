from __future__ import annotations

import os

import azure.functions as func
from graph import build_graph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.cosmos import create_cosmos_checkpointer

checkpointer = create_cosmos_checkpointer(
    endpoint=os.environ["AZURE_COSMOS_ENDPOINT"],
    database_name=os.environ.get("LANGGRAPH_COSMOS_DATABASE", "langgraph"),
    container_name=os.environ.get("LANGGRAPH_COSMOS_CONTAINER", "checkpoints"),
    # key= is read from COSMOS_KEY env var when not passed explicitly
)

compiled_graph = build_graph().compile(checkpointer=checkpointer)

langgraph_app = LangGraphApp(
    platform_compat=True,
    auth_level=func.AuthLevel.FUNCTION,
)

langgraph_app.register(
    graph=compiled_graph,
    name="cosmos_agent",
    description="Echo agent persisted with Azure Cosmos DB checkpoint saver.",
)

app = langgraph_app.function_app
