"""Standalone local Functions wiring for the official memory graph."""

import azure.functions as func
from graph import graph

from azure_functions_langgraph import LangGraphApp

langgraph_app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
langgraph_app.register(graph=graph, name="official_conversation")
app = langgraph_app.function_app
