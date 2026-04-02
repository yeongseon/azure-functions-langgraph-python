# Usage Guide

This guide covers all endpoints, request/response formats, and error handling.

## Endpoints overview

When you register a graph named `my_agent`, the following endpoints are created:

| Method | Route | Description |
|--------|-------|-------------|
| `POST` | `/api/graphs/my_agent/invoke` | Synchronous graph invocation |
| `POST` | `/api/graphs/my_agent/stream` | Buffered SSE streaming |
| `GET` | `/api/health` | Health check with registered graph list |

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

!!! warning "Buffered streaming (v0.1)"
    In v0.1, streaming is **buffered** — all chunks are collected first, then returned as a single SSE-formatted HTTP response. This is not true chunked streaming. True streaming support is planned for a future release.

### Stream error handling

If the graph raises an exception during streaming, an error event is included:

```
event: error
data: {"error": "Graph execution failed: ..."}

event: end
data: {}
```

## Health endpoint

### Request

```
GET /api/health
```

### Response

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
