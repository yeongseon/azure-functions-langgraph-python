# Observability with Application Insights (`LoggingRunObserver`)

This example ships **run telemetry** from your LangGraph endpoints to **Azure
Application Insights** using the package's built-in, dependency-free
[`LoggingRunObserver`](../../src/azure_functions_langgraph/observability.py)
(issue #407).

It is the natural next step after [`run_observer/`](../run_observer/):

| Example | What it shows |
| --- | --- |
| [`run_observer`](../run_observer/) | How to **write your own** `RunObserver` from the lifecycle contract. |
| **`observability_app_insights`** (this one) | How to use the **built-in** `LoggingRunObserver` and query the telemetry in App Insights with KQL. |

## What you get

The built-in observer emits one structured log record per run-lifecycle event
(`started` / `completed` / `failed` / `rejected`). Every record carries a safe,
non-sensitive field set under the `langgraph_run` key:

| Field | Meaning |
| --- | --- |
| `graph_name` | Registered graph name |
| `endpoint` | `invoke` or `stream` |
| `run_id` | Unique per-run correlation id |
| `thread_id` | Thread id (when the request supplies one) |
| `assistant_id` | Assistant id (Platform runs) |
| `stream_mode` | Stream mode for `/stream` runs |
| `transport` | `buffered` (today) or `streaming` (reserved) |
| `has_checkpointer` | Whether the graph has a checkpointer |
| `lock_backend` | Thread-lock backend class name |
| `duration_ms` | Wall time (terminal events only) |
| `status` | `started` / `completed` / `failed` / `rejected` |
| `error_type` | Exception **class name** on failure (never the message) |
| `reason` | Rejection reason on `rejected` |

> **Safety guarantee.** The observer logs **only** the fields above. It never
> logs request input, model messages, tool arguments, checkpoint values,
> config, headers, function keys, or connection strings. Telemetry is
> best-effort and isolated — an observer error can never fail a graph run.

## Boundary — what this package does and does not own

This package owns the **domain signal** (the run lifecycle events and their safe
fields). It does **not** own:

- log formatting or the App Insights connection — that is Azure Functions'
  native Application Insights integration, configured via
  `APPLICATIONINSIGHTS_CONNECTION_STRING`;
- OpenTelemetry exporters, tracer-provider setup, or sampling policy;
- PII redaction policy beyond the no-payload guarantee above.

If you already use the companion
[`azure-functions-logging`](https://github.com/yeongseon/azure-functions-logging-python)
package, keep using it for formatting/routing — this observer simply *produces*
the records; your logging configuration decides where they go.

## Wiring

```python
import logging
from azure_functions_langgraph import LangGraphApp, LoggingRunObserver

logging.getLogger("azure_functions_langgraph.observability.run").setLevel(logging.INFO)

app = LangGraphApp(observer=LoggingRunObserver())
app.register(graph=compiled_graph, name="observability_app_insights")
func_app = app.function_app
```

That's it. On Azure, set `APPLICATIONINSIGHTS_CONNECTION_STRING` in your Function
App settings (the Functions runtime ships Python logs to App Insights
automatically). The structured `langgraph_run` fields surface in the
`customDimensions` column of the `traces` table.

## Run locally

```bash
pip install azure-functions-langgraph "langgraph>=1.1"
func start

curl -X POST http://localhost:7071/api/graphs/observability_app_insights/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"user_text": "Ada"}}'
```

You will see records like:

```
langgraph.run.started {'graph_name': 'observability_app_insights', 'endpoint': 'invoke', 'run_id': '...'}
langgraph.run.completed {'graph_name': 'observability_app_insights', 'endpoint': 'invoke', 'run_id': '...'}
```

## KQL queries

Azure Functions' App Insights integration writes Python log records to the
`traces` table, exposing the observer's structured fields under
`customDimensions`. Depending on your logging configuration the fields land
either as flattened `customDimensions` keys (prefixed with `langgraph_run.`) or
inside the JSON `message`. Both patterns are shown below.

### Run count over time (via `customDimensions`)

```kusto
traces
| where customDimensions["langgraph_run.status"] in ("completed", "failed")
| summarize runs = count() by bin(timestamp, 5m), tostring(customDimensions["langgraph_run.status"])
| render timechart
```

### p50 / p95 latency by graph and endpoint

```kusto
traces
| where customDimensions["langgraph_run.status"] == "completed"
| extend graph = tostring(customDimensions["langgraph_run.graph_name"]),
         endpoint = tostring(customDimensions["langgraph_run.endpoint"]),
         duration_ms = todouble(customDimensions["langgraph_run.duration_ms"])
| summarize p50 = percentile(duration_ms, 50),
            p95 = percentile(duration_ms, 95),
            runs = count()
  by graph, endpoint
| order by p95 desc
```

### Error rate by `error_type`

```kusto
traces
| where customDimensions["langgraph_run.status"] == "failed"
| extend error_type = tostring(customDimensions["langgraph_run.error_type"]),
         graph = tostring(customDimensions["langgraph_run.graph_name"])
| summarize failures = count() by graph, error_type
| order by failures desc
```

### Rejections by reason

```kusto
traces
| where customDimensions["langgraph_run.status"] == "rejected"
| extend reason = tostring(customDimensions["langgraph_run.reason"])
| summarize rejected = count() by reason
| order by rejected desc
```

### Correlate all events for one thread

```kusto
traces
| where customDimensions["langgraph_run.thread_id"] == "conversation-1"
| extend status = tostring(customDimensions["langgraph_run.status"]),
         run_id = tostring(customDimensions["langgraph_run.run_id"]),
         endpoint = tostring(customDimensions["langgraph_run.endpoint"])
| project timestamp, run_id, endpoint, status
| order by timestamp asc
```

### Alternative — fields embedded as JSON in `message`

If your logging setup serialises the `extra` dict into the message instead of
flattening it into `customDimensions`, parse the JSON first:

```kusto
traces
| where message has "langgraph.run"
| extend run = parse_json(tostring(customDimensions["langgraph_run"]))
| where tostring(run.status) == "completed"
| summarize p95 = percentile(todouble(run.duration_ms), 95) by tostring(run.graph_name)
```

## Files

| File | Purpose |
| --- | --- |
| `graph.py` | Deterministic two-node graph (no LLM, CI-friendly). |
| `function_app.py` | Wires the built-in `LoggingRunObserver` onto `LangGraphApp`. |
| `host.json` | Enables the App Insights sampling defaults. |
| `requirements.txt` | Runtime dependencies. |
| `local.settings.json.example` | Local settings template. |
