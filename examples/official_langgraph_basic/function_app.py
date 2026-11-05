"""Official LangGraph graph hosted on Azure Functions - registration shim.

Run from this directory:
    func start
"""

from upstream_graph import graph

from azure_functions_langgraph import LangGraphApp

langgraph_app = LangGraphApp()
langgraph_app.register(
    graph=graph,
    name="official_basic",
    description="Official LangGraph 'Define and update state' graph, unmodified",
)

app = langgraph_app.function_app
