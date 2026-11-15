# Usage Guide

This guide covers all endpoints, request/response formats, and error handling.

## Endpoints overview

When you register a graph named `my_agent`, the following endpoints are created:

| Method | Route | Description |
|--------|-------|-------------|
| `POST` | `/api/graphs/my_agent/invoke` | Synchronous graph invocation |
| `POST` | `/api/graphs/my_agent/stream` | Buffered SSE streaming |
| `GET` | `/api/graphs/my_agent/threads/{thread_id}/state` | Thread state inspection |
| `GET` | `/api/health` | Anonymous liveness probe (`{"status": "ok"}`) |
| `GET` | `/api/health/details` | Registered-graph inventory (protected by default) |

## Invoke endpoint

### Request

```
POST /api/graphs/{name}/invoke
Content-Type: application/json
```

```json
{
    "input": {
        "messages": [{"role": "human", "content": "Hello!"}]
    },
    "config": {
        "configurable": {"thread_id": "conversation-1"}
    },
    "metadata": {
        "user_id": "abc-123"
    }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `input` | `dict` | Yes | Input state for the graph |
| `config` | `dict` or `null` | No | LangGraph config (e.g., thread_id for checkpointer) |
| `metadata` | `dict` or `null` | No | Additional metadata passed to the run |

### Response

```json
{
    "output": {
        "messages": [
            {"role": "human", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"}
        ]
    }
}
```

The `output` field contains the full graph output state.

## Stream endpoint

### Request

```
POST /api/graphs/{name}/stream
Content-Type: application/json
```

```json
{
    "input": {
        "messages": [{"role": "human", "content": "Hello!"}]
    },
    "config": {
        "configurable": {"thread_id": "conversation-1"}
    },
    "stream_mode": "values"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `input` | `dict` | Yes | — | Input state for the graph |
| `config` | `dict` or `null` | No | `null` | LangGraph config |
| `stream_mode` | `str` | No | `"values"` | Stream mode: `"values"`, `"updates"`, `"messages"`, or `"custom"` |
| `metadata` | `dict` or `null` | No | `null` | Additional metadata |

### Response

The response is formatted as Server-Sent Events (SSE):

```
Content-Type: text/event-stream
Cache-Control: no-cache
```

```
event: data
data: {"messages": [{"role": "human", "content": "Hello!"}]}

event: data
data: {"messages": [{"role": "human", "content": "Hello!"}, {"role": "assistant", "content": "Hi!"}]}

event: end
data: {}
```

!!! warning "Buffered streaming"
    `LangGraphApp` collects all chunks first, then returns one SSE-formatted response. For true incremental delivery, install `azure-functions-langgraph[streaming]` and use `StreamingLangGraphApp`. Its FastAPI/ASGI transport is an app-wide choice and cannot be mixed with classic routes in the same function app; see the package README's **Streaming behavior** section.

### Stream error handling

If the graph raises an exception during streaming, an error event is included:

```
event: error
data: {"error": "Graph execution failed: ..."}

event: end
data: {}
```

## Output versions: v1 and v2

Both `invoke` and `stream` accept an optional `version` field (`"v1"` or `"v2"`). It is
forwarded to LangGraph's `graph.invoke(..., version=...)` / `graph.stream(..., version=...)`.
Omit it and the default shape is unchanged; `"v2"` is opt-in per request and requires
`langgraph>=1.1`.

| | Default (v1) | `"version": "v2"` |
|---|---|---|
| `invoke` | `{"output": {"count": 4, "history": [2, 4]}}` | `{"output": {"value": {"count": 4, "history": [2, 4]}, "interrupts": []}}` |
| `stream` event (`stream_mode: "values"`) | `data: {"count": 2, "history": [2]}` | `data: {"type": "values", "ns": [], "data": {"count": 2, "history": [2]}, "interrupts": []}` |

Migration notes:

- Read `output["value"]` instead of `output` for `invoke`, and `event["data"]` instead of the bare
  snapshot for each `stream` event. The state inside is identical.
- `LangGraphApp` `/stream` is still buffered SSE (`StreamingLangGraphApp` is the incremental option); `version` changes the event shape, not the delivery.
- A graph whose `invoke`/`stream` does not accept `version` (e.g. `langgraph<1.1`) returns `422`; an
  unknown value such as `"v9"` is also rejected with `422`.

A runnable, offline comparison (no Azure host, network, credentials or model calls) is in
[`examples/versioned_output_agent`](https://github.com/yeongseon/azure-functions-langgraph-python/tree/main/examples/versioned_output_agent):
`python compare_shapes.py`.

## Health endpoint

### Request

```
GET /api/health
```

### Response

```json
{"status": "ok"}
```

The anonymous liveness probe returns only `{"status": "ok"}` — it never
enumerates registered graphs.

### Detailed inventory

```
GET /api/health/details
```

Returns the registered-graph inventory. This surface is protected by
`health_details_auth_level` (defaults to the app-level `auth_level`), so it
requires a function key unless you opt it into `ANONYMOUS` explicitly.

```json
{
    "status": "ok",
    "graphs": [
        {
            "name": "my_agent",
            "description": "Customer support agent",
            "has_checkpointer": true
        }
    ]
}
```

## State endpoint

### Request

```
GET /api/graphs/{name}/threads/{thread_id}/state
```

Returns the current state of a thread for graphs compiled with a checkpointer (graphs satisfying the `StatefulGraph` protocol).
Registrations with `async_mode=True` retrieve state with LangGraph's
`aget_state`, so async-only checkpointers such as `AsyncPostgresSaver` do not
fall back to synchronous checkpoint I/O. Sync registrations continue to use
`get_state`. State values must be JSON-serializable and finite; serialization
failures return HTTP 500 instead of emitting non-standard JSON.

This async routing applies to the native state endpoint above. The experimental
Platform-compatible state and history endpoints still use synchronous graph
state methods; async-only Platform state/history support remains follow-up work.

### Response

```json
{
    "values": {"messages": [...]},
    "next": [],
    "metadata": {"source": "loop", "step": 2},
    "config": {"configurable": {"thread_id": "session-123"}},
    "created_at": "2025-01-01T00:00:00Z",
    "parent_config": null
}
```

| Status Code | Condition |
|-------------|----------|
| 200 | Thread state found |
| 400 | Missing or invalid thread ID |
| 409 | Registered graph does not expose the state method required by its mode |
| 404 | Thread not found or graph is not stateful |
| 500 | Internal error |

## OpenAPI generation

Use [`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python) with
`azure_functions_langgraph.openapi.register_with_openapi`. See
[Migrating to azure-functions-openapi-python](#migrating-to-azure-functions-openapi-python) below.

## Migrating to azure-functions-openapi-python

Starting with v0.5.0, OpenAPI spec generation is moving to the dedicated
[`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python) package.

### Using the bridge

```python
from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.openapi import register_with_openapi

app = LangGraphApp()
app.register(graph=graph, name="agent", request_model=MyRequest)

# Forward route metadata to azure-functions-openapi-python
count = register_with_openapi(app)
print(f"Registered {count} routes with azure-functions-openapi-python")
```

The bridge reads route metadata from `LangGraphApp.get_app_metadata()` and forwards it
to `azure-functions-openapi-python`'s `register_openapi_metadata()` programmatic API.
Requires `azure-functions-openapi-python >= 0.16.0`.

## Error responses

All error responses follow a consistent format:

```json
{
    "error": "error",
    "detail": "Description of what went wrong"
}
```

| Status Code | Condition |
|-------------|-----------|
| 400 | Invalid JSON body |
| 422 | Request validation error (missing/invalid fields) |
| 500 | Graph execution failed |
| 501 | Stream requested on a graph that does not support `stream()` |
| 404 | Thread not found (state endpoint) |

## Using with checkpointers

Thread-based conversation state is managed through LangGraph's checkpointer mechanism. The library passes the `config` from the request body directly to `graph.invoke()` or `graph.stream()`.

```python
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()
graph = builder.compile(checkpointer=checkpointer)

app = LangGraphApp()
app.register(graph=graph, name="stateful_agent")
```

Then in requests, include `thread_id`:

```json
{
    "input": {"messages": [{"role": "human", "content": "Remember my name is Alice"}]},
    "config": {"configurable": {"thread_id": "session-abc"}}
}
```

Subsequent requests with the same `thread_id` will resume the conversation:

```json
{
    "input": {"messages": [{"role": "human", "content": "What is my name?"}]},
    "config": {"configurable": {"thread_id": "session-abc"}}
}
```

## Invoke-only graphs

If your graph only supports `invoke()` (no `stream()` method), you can still register it. The invoke endpoint works, but the stream endpoint returns a `501` error:

```json
{
    "error": "error",
    "detail": "Graph 'my_agent' does not support streaming"
}
```

## Per-graph authentication

Override the app-level auth for individual graphs:

```python
import azure.functions as func

app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)

# Public graph — no auth required
app.register(graph=public_graph, name="public", auth_level=func.AuthLevel.ANONYMOUS)

# Admin graph — requires master key
app.register(graph=admin_graph, name="admin", auth_level=func.AuthLevel.ADMIN)
```

When `auth_level` is passed to `register()`, it overrides the app-level setting for that graph's native endpoints only. Platform-compatible (`platform_compat=True`) and Durable (`async_runs="durable"`) routes are shared by every graph and use the app-level `auth_level`; a graph *stricter* than the app level is rejected with `ValueError` when those routes are enabled (see [security](security.md#mixed-per-graph-auth-with-platform-or-durable-routes)).
