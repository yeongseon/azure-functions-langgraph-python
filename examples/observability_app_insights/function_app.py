"""App Insights observability example - Azure Functions entry point.

Demonstrates shipping run telemetry to **Azure Application Insights** using the
package's built-in, dependency-free
:class:`~azure_functions_langgraph.observability.LoggingRunObserver` (issue
#407).

Unlike ``examples/run_observer/`` — which shows how to *write your own*
:class:`RunObserver` — this example uses the observer that ships **inside** the
package. You wire it once and Azure Functions' native Application Insights
integration picks up the structured log records automatically; the ``README.md``
next to this file shows the KQL queries that turn those records into
dashboards.

Key properties (all guaranteed by the built-in observer):

- Emits **only** correlation identifiers, timing, and run metadata — never
  input, output, config, headers, or secrets.
- Observer failures are isolated and never fail the graph run.
- Structured fields are attached under a single ``extra`` key
  (``langgraph_run``) so they surface in App Insights ``customDimensions``.

This package owns the *domain signal* (the run lifecycle events). It does not
own log formatting, App Insights ingestion, sampling policy, or PII redaction —
those remain the platform's / your logging configuration's responsibility.

Run from this directory:
    func start
"""

from __future__ import annotations

import logging

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp, LoggingRunObserver

# The built-in observer logs on ``azure_functions_langgraph.observability.run``
# by default. Emit INFO so started/completed/rejected records reach App
# Insights (failures are always logged at ERROR regardless of this level).
logging.getLogger("azure_functions_langgraph.observability.run").setLevel(logging.INFO)

langgraph_app = LangGraphApp(observer=LoggingRunObserver())
langgraph_app.register(
    graph=compiled_graph,
    name="observability_app_insights",
    description="A deterministic agent shipping run telemetry to App Insights (issue #407)",
)

app = langgraph_app.function_app
