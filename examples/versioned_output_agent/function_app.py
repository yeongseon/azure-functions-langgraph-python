"""Versioned-output agent - Azure Functions entry point.

Run from this directory:
    func start
"""

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

langgraph_app = LangGraphApp()
langgraph_app.register(
    graph=compiled_graph,
    name="versioned_output_agent",
    description="A counter agent demonstrating the optional version='v2' pass-through",
)

app = langgraph_app.function_app
