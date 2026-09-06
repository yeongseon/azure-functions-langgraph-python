"""Async agent - Azure Functions entry point.

Run from this directory:
    func start
"""

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

langgraph_app = LangGraphApp()
langgraph_app.register(
    graph=compiled_graph,
    name="async_agent",
    description="A deterministic agent served through the native async (ainvoke/astream) path",
    async_mode=True,
)

app = langgraph_app.function_app
