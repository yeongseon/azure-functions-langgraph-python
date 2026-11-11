"""Streaming authorization example — Azure Functions entry point.

Two separate layers guard the route:

1. **Azure Functions host key** (``auth_level=FUNCTION``): says the caller holds an
   application key. It is NOT a user identity and proves no thread ownership.
2. **``authorize=`` callback** (``policy.py``): runs once per request, after body
   validation and before any lock, checkpoint, or graph work. It decides whether
   *this principal* may run *this graph* on *this thread*.

Requires the ``streaming`` extra and Azure Functions runtime 4.34.1+; see
``README.md``. Run ``python demo.py`` for an offline allowed-versus-denied comparison.
"""

from __future__ import annotations

import azure.functions as func
from graph import compiled_graph
from policy import authorize

from azure_functions_langgraph.streaming import StreamingLangGraphApp

langgraph_app = StreamingLangGraphApp(auth_level=func.AuthLevel.FUNCTION, authorize=authorize)

langgraph_app.register(graph=compiled_graph, name="guarded_agent")

app = langgraph_app.function_app
