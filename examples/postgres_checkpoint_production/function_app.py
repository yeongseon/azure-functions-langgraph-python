from __future__ import annotations

import os

from graph import build_graph

import azure.functions as func

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.postgres import create_postgres_checkpointer

_CONN_STRING = os.environ["LANGGRAPH_POSTGRES_CONNECTION_STRING"]
_RUN_SETUP = os.environ.get("LANGGRAPH_POSTGRES_SETUP", "true").lower() == "true"

checkpointer = create_postgres_checkpointer(_CONN_STRING, setup=_RUN_SETUP)
compiled_graph = build_graph().compile(checkpointer=checkpointer)

langgraph_app = LangGraphApp(
    platform_compat=True,
    auth_level=func.AuthLevel.FUNCTION,
)
langgraph_app.register(
    graph=compiled_graph,
    name="postgres_agent",
    description="Echo agent persisted with the bundled Postgres checkpointer DX helper.",
)

app = langgraph_app.function_app
