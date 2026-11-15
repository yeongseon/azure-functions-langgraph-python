"""Offline observer proof - Azure Functions entry point.

Wires the package's built-in ``LoggingRunObserver`` exactly as a production app
would. The offline proof of its payload-free output lives in ``proof.py`` and in
``tests/test_run_observer_offline_proof_example.py``; neither needs this file's
Azure Functions host.

Run from this directory:
    func start
"""

from __future__ import annotations

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp, LoggingRunObserver

langgraph_app = LangGraphApp(observer=LoggingRunObserver())
langgraph_app.register(
    graph=compiled_graph,
    name="run_observer_offline_proof",
    description="A deterministic graph used to prove observer output carries no payloads",
)

app = langgraph_app.function_app
