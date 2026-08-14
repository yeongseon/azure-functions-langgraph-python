"""E2E certification Function App for azure-functions-langgraph.

Deployed to a real Azure Functions Consumption host by the e2e-azure workflow.
Exposes only the NATIVE LangGraph routes (no platform_compat), which require
only AzureWebJobsStorage:

    GET  /api/health
    POST /api/graphs/e2e_agent/invoke
    POST /api/graphs/e2e_agent/stream
"""

import azure.functions as func

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

# Anonymous auth so the certification suite can call invoke/stream without
# extracting a function key. This is a throwaway, single-release e2e host with
# no sensitive data; the explicit ANONYMOUS opt-in emits an intended UserWarning.
langgraph_app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
langgraph_app.register(
    graph=compiled_graph,
    name="e2e_agent",
    description="Two-node greeting agent used for real-Azure e2e certification",
)

app = langgraph_app.function_app
