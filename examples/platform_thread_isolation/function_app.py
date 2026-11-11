from graph import compiled_graph

import azure.functions as func

from azure_functions_langgraph import LangGraphApp

langgraph_app = LangGraphApp(
    platform_compat=True,
    # Local-dev opt-in to ANONYMOUS: emits an unconditional UserWarning.
    # For production, drop this kwarg to use the FUNCTION default.
    auth_level=func.AuthLevel.ANONYMOUS,
)
langgraph_app.register(
    graph=compiled_graph,
    name="notebook_agent",
    description="Deterministic notebook agent for per-thread isolation demo.",
)

app = langgraph_app.function_app
