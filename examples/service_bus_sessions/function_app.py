"""Service Bus session-aware agent — Azure Functions entry point.

The **session-enabled** queue maps each message's ``session_id`` to the graph
checkpoint ``thread_id``. See ``session_demo.py`` for the offline walkthrough
that needs no Service Bus namespace.

Run from this directory (needs a session-enabled queue):
    func start
"""

from __future__ import annotations

import os

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

langgraph_app = LangGraphApp()

langgraph_app.register_service_bus(
    graph=compiled_graph,
    name="service_bus_sessions",
    connection="ServiceBusConnection",
    queue_name=os.environ.get("SERVICE_BUS_QUEUE_NAME", "langgraph-sessions"),
    session=True,
)

app = langgraph_app.function_app
