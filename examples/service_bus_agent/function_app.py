"""Service Bus agent — Azure Functions entry point.

Drives a LangGraph graph from an Azure Service Bus queue message. Each
delivery is mapped to a single ``invoke`` call. Graph exceptions are **not**
swallowed, so a failing run lets the Service Bus binding abandon/retry the
message according to your queue's delivery policy.

Run from this directory:
    func start
"""

from __future__ import annotations

import os

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

langgraph_app = LangGraphApp()

langgraph_app.register_service_bus(
    graph=compiled_graph,
    name="service_bus_agent",
    connection="ServiceBusConnection",
    queue_name=os.environ.get("SERVICE_BUS_QUEUE_NAME", "langgraph-jobs"),
)


app = langgraph_app.function_app
