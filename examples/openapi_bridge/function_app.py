import azure.functions as func
from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.openapi import register_with_openapi

langgraph_app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
langgraph_app.register(
    graph=compiled_graph,
    name="echo_agent",
    description="Echo agent with OpenAPI metadata forwarded via the bridge.",
)

count = register_with_openapi(langgraph_app)
print(f"[openapi_bridge] forwarded {count} route(s) to azure-functions-openapi-python")

app = langgraph_app.function_app
