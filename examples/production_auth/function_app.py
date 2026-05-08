from graph import private_graph, public_graph

import azure.functions as func

from azure_functions_langgraph import LangGraphApp

# Production: default to FUNCTION; override per-graph as needed
langgraph_app = LangGraphApp(
    auth_level=func.AuthLevel.FUNCTION,
    # Explicitly protect health endpoint in production; defaults to ANONYMOUS
    health_auth_level=func.AuthLevel.FUNCTION,
)

langgraph_app.register(
    graph=private_graph,
    name="private_agent",
    description="Requires a function key (?code=...).",
    auth_level=func.AuthLevel.FUNCTION,
)
langgraph_app.register(
    graph=public_graph,
    name="public_agent",
    description="Anonymous access for public/demo use.",
    auth_level=func.AuthLevel.ANONYMOUS,
)

app = langgraph_app.function_app
