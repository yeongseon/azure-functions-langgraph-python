# Versioned Output Agent Example

Demonstrates the optional **`version` pass-through** on the native
`invoke`/`stream` endpoints (issue #423). A trivial two-node counter graph
(`increment` → `double`) keeps the focus on the request contract rather than the
graph logic.

`version` is **opt-in per request** and fully backward-compatible — omit it and
you get the current behavior; set it to `"v2"` to receive LangGraph 1.1+'s
unified output shapes.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4+
- Python 3.10+
- `langgraph>=1.1` (required for `version="v2"`)

## Run locally

```bash
cd examples/versioned_output_agent
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

## Test

### Default (no `version`) — legacy output

`invoke` returns the graph's final **state dict** under `output`:

```bash
curl -X POST http://localhost:7071/api/graphs/versioned_output_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"count": 1, "history": []}}'
```

```json
{"output": {"count": 4, "history": [2, 4]}}
```

### `version: "v2"` — unified `GraphOutput` shape

With `"version": "v2"`, `invoke` returns LangGraph's `GraphOutput`, serialized as
`{"value": <state>, "interrupts": [...]}`:

```bash
curl -X POST http://localhost:7071/api/graphs/versioned_output_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"count": 1, "history": []}, "version": "v2"}'
```

```json
{"output": {"value": {"count": 4, "history": [2, 4]}, "interrupts": []}}
```

### Streaming with `version: "v2"` — unified `StreamPart` events

Without `version`, `stream` emits `stream_mode`-shaped events. With `"version":
"v2"`, each SSE `data` event is a unified `StreamPart`
`{"type", "ns", "data", "interrupts"}`:

```bash
curl -X POST http://localhost:7071/api/graphs/versioned_output_agent/stream \
  -H "Content-Type: application/json" \
  -d '{"input": {"count": 1, "history": []}, "stream_mode": "values", "version": "v2"}'
```

```
event: data
data: {"type": "values", "ns": [], "data": {"count": 1, "history": []}, "interrupts": []}

event: data
data: {"type": "values", "ns": [], "data": {"count": 2, "history": [2]}, "interrupts": []}

event: data
data: {"type": "values", "ns": [], "data": {"count": 4, "history": [2, 4]}, "interrupts": []}

event: end
data: {}
```

> **Streaming is buffered SSE.** As with every `/stream` endpoint in this
> package, chunks are collected during execution and flushed after the run
> completes — `version="v2"` changes the **event shape**, not the buffering
> behavior.

### Unsupported `version`

Passing a `version` to a graph whose `invoke`/`stream` does not accept one (e.g.
`langgraph<1.1` or a custom structural graph) returns a clear `422` rather than
an opaque server error:

```json
{"error": "error", "detail": "Graph 'versioned_output_agent' does not accept a 'version' argument (requires langgraph>=1.1)"}
```

### Health check

```bash
curl http://localhost:7071/api/health
```
