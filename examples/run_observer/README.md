# Run Observer Example

Demonstrates the **run-lifecycle observability contract** (issue #425). The app
registers a pluggable [`RunObserver`](../../src/azure_functions_langgraph/observability.py)
that is notified across every native `invoke`/`stream` run:

- `on_run_started` — after all pre-execution validation passes, just before the
  graph is called.
- `on_run_completed` — after the graph returns successfully.
- `on_run_failed` — when the graph raises (or a stream exceeds the buffered-size
  cap).
- `on_run_rejected` — when a run never reaches the graph (bad request/config,
  unsupported version, streaming unsupported, or lock contention).

The graph itself is deterministic — no LLM calls — so the example runs in CI
without any cloud credentials. The interesting part is `function_app.py`, which
wires a small `LoggingRunObserver` into `LangGraphApp(observer=...)`.

## No payloads, no secrets

The `RunContext` handed to the observer carries **only** correlation identifiers
(graph name, endpoint, run id, thread id) and monotonic timings — never input,
output, config, or headers. The example observer therefore logs run metadata
that is safe to ship to any sink. Concrete exporters (Application Insights,
OpenTelemetry, KQL) layer on top of this same contract and are tracked
separately (issue #407).

## Opt-in registration

The observer is an app-level, opt-in kwarg. Omit it and the app uses a no-op
observer, so wiring an observer never changes HTTP behaviour:

```python
langgraph_app = LangGraphApp(observer=LoggingRunObserver())
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
cd examples/run_observer
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

## Test

### Invoke (emits `on_run_started` → `on_run_completed`)

```bash
curl -X POST http://localhost:7071/api/graphs/run_observer/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"user_text": "Ada", "history": [], "turn_count": 0}}'
```

```json
{"output": {"user_text": "Ada", "history": ["Hello, Ada!"], "turn_count": 1, "last_reply": "Hello, Ada!"}}
```

The Functions host log shows the observer output:

```
run.started   graph=run_observer endpoint=invoke run_id=... thread_id=None
run.completed graph=run_observer endpoint=invoke run_id=... thread_id=None elapsed_ms=...
```

### Rejected run (emits `on_run_rejected`)

Send a malformed body to see the rejection path — the graph is never invoked:

```bash
curl -X POST http://localhost:7071/api/graphs/run_observer/invoke \
  -H "Content-Type: application/json" \
  -d 'not json'
```

```
run.rejected graph=run_observer endpoint=invoke run_id=... thread_id=None reason=invalid_request
```

### Stream (emits `on_run_started` → `on_run_completed`)

```bash
curl -X POST http://localhost:7071/api/graphs/run_observer/stream \
  -H "Content-Type: application/json" \
  -d '{"input": {"user_text": "Ada", "history": [], "turn_count": 0}, "stream_mode": "values"}'
```

### Health check

```bash
curl http://localhost:7071/api/health
```
