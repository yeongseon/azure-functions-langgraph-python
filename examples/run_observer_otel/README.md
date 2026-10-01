# OpenTelemetry Run Observer Example

Demonstrates the **optional OpenTelemetry observer** (issue #434), which layers
on top of the run-lifecycle observability contract (issues #425 / #407). The app
registers an [`OTelRunObserver`](../../src/azure_functions_langgraph/observability_otel.py)
that maps every native `invoke`/`stream` run to a single OpenTelemetry span:

- `on_run_started` → **starts** a span (`langgraph.run.invoke` / `.stream`).
- `on_run_completed` → ends the span with `StatusCode.OK`.
- `on_run_failed` → records the exception (class/type only), sets
  `StatusCode.ERROR`, and ends the span.
- `on_run_rejected` → a run that never reached the graph opens and immediately
  ends a short span tagged with the rejection reason.

The graph itself is deterministic — no LLM calls — so the example runs in CI
without any cloud credentials.

## Install behind the `otel` extra

The observer is gated behind an optional extra so base installs stay lean:

```bash
pip install "azure-functions-langgraph[otel]"
```

Importing `OTelRunObserver` without the extra raises a friendly error naming it.

## No payloads, no secrets

Span attributes come **only** from the safe `RunContext` correlation fields —
`langgraph.graph_name`, `langgraph.endpoint`, `langgraph.run_id`,
`langgraph.thread_id`, `langgraph.assistant_id`, `langgraph.stream_mode`,
`langgraph.transport`, `langgraph.has_checkpointer`, `langgraph.lock_backend`,
plus a terminal `langgraph.duration_ms` and `langgraph.status`. Never input,
output, config, headers, or secrets.

## The package owns the domain signal only

`OTelRunObserver` acquires a tracer via `opentelemetry.trace.get_tracer(...)` and
emits spans onto **whatever `TracerProvider` you install**. It does **not**
configure a provider, exporter, sampling, or resource attributes — that is
operator-owned. This example's `function_app.py` wires a **console exporter**
locally so `func start` prints spans; in production, use the Azure Functions
Application Insights integration, the OpenTelemetry distro, or an OTLP exporter
you configure.

## Opt-in registration

```python
from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.observability_otel import OTelRunObserver

app = LangGraphApp(observer=OTelRunObserver())
```

Observer callbacks are isolated — an exception raised inside an observer is
swallowed and logged, and never changes the HTTP response or interferes with
thread-lock release.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4+
- Python 3.10+
- `langgraph>=1.1`

## Run locally

```bash
cd examples/run_observer_otel
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

## Test

### Invoke (emits `on_run_started` → `on_run_completed`)

```bash
curl -X POST http://localhost:7071/api/graphs/run_observer_otel/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"user_text": "Ada", "history": [], "turn_count": 0}}'
```

```json
{"output": {"user_text": "Ada", "history": ["Hello, Ada!"], "turn_count": 1, "last_reply": "Hello, Ada!"}}
```

The Functions host log shows the console-exported span, e.g.:

```
{
  "name": "langgraph.run.invoke",
  "status": {"status_code": "OK"},
  "attributes": {
    "langgraph.graph_name": "run_observer_otel",
    "langgraph.endpoint": "invoke",
    "langgraph.run_id": "...",
    "langgraph.status": "completed",
    "langgraph.duration_ms": 1.2
  }
}
```

### Rejected run (emits `on_run_rejected`)

```bash
curl -X POST http://localhost:7071/api/graphs/run_observer_otel/invoke \
  -H "Content-Type: application/json" \
  -d 'not json'
```

The span carries `"langgraph.status": "rejected"` and
`"langgraph.reject_reason": "invalid_request"` with `StatusCode.ERROR`.

### Health check

```bash
curl http://localhost:7071/api/health
```
