"""Azure OpenAI agent - Azure Functions entry point.

Run from this directory:
    func start
"""

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

# Default auth_level is AuthLevel.FUNCTION — deployed endpoints require a
# function key. See README for the anonymous local-dev opt-in.
langgraph_app = LangGraphApp()
langgraph_app.register(
    graph=compiled_graph,
    name="azure_openai_agent",
    description="A concise Azure cloud assistant backed by an Azure OpenAI chat deployment",
)

app = langgraph_app.function_app
