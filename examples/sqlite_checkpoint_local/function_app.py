from __future__ import annotations

import os

import azure.functions as func
from graph import build_graph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.sqlite import create_sqlite_checkpointer

_DB_PATH = os.environ.get("LANGGRAPH_SQLITE_PATH", "/tmp/langgraph_checkpoints.sqlite")
_RUN_SETUP = os.environ.get("LANGGRAPH_SQLITE_SETUP", "true").lower() == "true"

checkpointer = create_sqlite_checkpointer(_DB_PATH, setup=_RUN_SETUP)
compiled_graph = build_graph().compile(checkpointer=checkpointer)

langgraph_app = LangGraphApp(
    platform_compat=True,
    auth_level=func.AuthLevel.ANONYMOUS,
)
langgraph_app.register(
    graph=compiled_graph,
    name="sqlite_agent",
    description="Echo agent persisted with the bundled SQLite checkpointer DX helper.",
)

app = langgraph_app.function_app
