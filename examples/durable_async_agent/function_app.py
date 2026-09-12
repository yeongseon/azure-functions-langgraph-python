"""Durable async agent — Azure Functions entry point.

Exposes a LangGraph graph as an **asynchronous** run lifecycle backed by Azure
Durable Functions (issue #408). Instead of blocking on a synchronous
``/invoke``, callers:

1. ``POST /api/graphs/durable_async_agent/runs`` — start a run, get ``202`` with
   a ``run_id`` and ``status: "pending"``.
2. ``GET  /api/runs/{run_id}`` — poll the normalized status
   (``pending`` → ``running`` → ``success`` / ``error`` / ``cancelled``); the
   ``output`` appears once the run completes.
3. ``POST /api/runs/{run_id}/cancel`` — terminate an in-flight run.

The Durable orchestrator stays deterministic — all graph execution happens in a
Durable activity — so replay is safe and the existing checkpointer / thread-lock
semantics are reused unchanged.

Requires the optional ``durable`` extra:
    pip install azure-functions-langgraph[durable]

Run from this directory:
    func start
"""

from __future__ import annotations

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

langgraph_app = LangGraphApp(async_runs="durable")

langgraph_app.register(graph=compiled_graph, name="durable_async_agent")

app = langgraph_app.function_app
