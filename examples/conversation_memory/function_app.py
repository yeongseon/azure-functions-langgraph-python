"""Conversation memory agent - Azure Functions entry point.

Run from this directory:
    func start
"""

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

# Default auth_level is AuthLevel.FUNCTION — deployed endpoints require a
# function key (`?code=<FUNCTION_KEY>` or the `x-functions-key` header).
langgraph_app = LangGraphApp()
langgraph_app.register(
    graph=compiled_graph,
    name="conversation_agent",
    description="A stateful Azure cloud assistant that remembers a thread's prior turns via a checkpointer",
)

app = langgraph_app.function_app
