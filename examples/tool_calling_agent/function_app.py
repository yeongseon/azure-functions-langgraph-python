"""Tool-calling agent - Azure Functions entry point.

Registers the compiled tool-calling graph, then wires the optional OpenAPI
bridge so compatible documentation tooling can consume its route metadata.

Run from this directory:
    func start
"""

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.openapi import register_with_openapi

# Default auth_level is AuthLevel.FUNCTION — deployed endpoints require a
# function key (`?code=<FUNCTION_KEY>` or the `x-functions-key` header).
langgraph_app = LangGraphApp()
langgraph_app.register(
    graph=compiled_graph,
    name="tool_calling_agent",
    description="An Azure OpenAI agent that calls tools (order lookup, weather) in a ToolNode loop",
)

try:
    count = register_with_openapi(langgraph_app)
except ImportError:
    count = 0
else:
    print(f"[tool_calling_agent] forwarded {count} route(s) to azure-functions-openapi-python")

app = langgraph_app.function_app
