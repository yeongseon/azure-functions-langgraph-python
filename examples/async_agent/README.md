# Async Agent Example

Demonstrates the native **async execution surface** (issue #422). The graph's
nodes are `async def` coroutines, and the app registers the graph with
`async_mode=True` so the native `invoke`/`stream` endpoints `await`
`graph.ainvoke` / `graph.astream` instead of the synchronous methods.

The node logic is deterministic — no LLM calls — so the example runs in CI
without any cloud credentials. The `await asyncio.sleep(0)` in each node stands
in for real awaitable I/O (an async HTTP client, an async DB driver, etc.).

## Why async?

- **`async_mode=True`** routes the endpoints through async Azure Functions
  handlers that `await` the graph on the worker's event loop.
- The per-thread lock (used when the graph has a checkpointer and the request
  carries a `thread_id`) is offloaded with `asyncio.to_thread`, so a blocking
  distributed lock backend never stalls the event loop.
- A `CompiledStateGraph` exposes **both** sync and async methods, so the same
  graph still works on the default (sync) path — the flag just selects which
  method the handlers call. A graph exposing **only** async methods is served
  async automatically, with or without the flag.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4+
- Python 3.10+
- `langgraph>=1.1`

## Run locally

```bash
cd examples/async_agent
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

## Test

### Invoke

`invoke` awaits `graph.ainvoke` and returns the graph's final state under
`output`:

```bash
curl -X POST http://localhost:7071/api/graphs/async_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"user_text": "Ada", "history": [], "turn_count": 0}}'
```

```json
{"output": {"user_text": "Ada", "history": ["Hello, Ada!"], "turn_count": 1, "last_reply": "Hello, Ada!"}}
```

### Stream

`stream` consumes `graph.astream` with `async for` and flushes buffered SSE
frames after the run completes:

```bash
curl -X POST http://localhost:7071/api/graphs/async_agent/stream \
  -H "Content-Type: application/json" \
  -d '{"input": {"user_text": "Ada", "history": [], "turn_count": 0}, "stream_mode": "values"}'
```

```
event: data
data: {"user_text": "Ada", "history": [], "turn_count": 0}

event: data
data: {"user_text": "Ada", "history": ["Hello, Ada!"], "turn_count": 0, "last_reply": "Hello, Ada!"}

event: data
data: {"user_text": "Ada", "history": ["Hello, Ada!"], "turn_count": 1, "last_reply": "Hello, Ada!"}

event: end
data: {}
```

> **Streaming is buffered SSE.** As with every `/stream` endpoint in this
> package, chunks are collected during execution and flushed after the run
> completes. `async_mode` changes **how the graph is called** (awaited), not the
> buffering behavior.

### Health check

```bash
curl http://localhost:7071/api/health
```
