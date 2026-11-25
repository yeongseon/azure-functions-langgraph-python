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

## Existing-app composition (Experimental)

Keep your existing `azure.functions.FunctionApp` and attach a native HTTP
Blueprint. Do not pass `langgraph_app.function_app` to `register_functions`:
Azure's SDK rejects a FunctionApp as a child. Do not export both FunctionApps;
the Python worker requires exactly one top-level application instance.

```python
import azure.functions as func
from azure_functions_langgraph import LangGraphApp
from my_graph import graph  # your existing compiled graph

app = func.FunctionApp()

@app.route(route="existing", methods=["GET"])
def existing_http(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("existing")

langgraph_app = LangGraphApp()
langgraph_app.register(graph, "agent")
app.register_functions(langgraph_app.to_blueprint())
```

The [runnable example](https://github.com/yeongseon/azure-functions-langgraph-python/tree/main/examples/existing_function_app)
adds the unchanged official basic LangGraph graph next to an existing HTTP and
timer trigger. The timer is ordinary application code, not a new adapter trigger.

### Registration timing and ownership

1. Configure the adapter and register **all** graphs before calling
   `to_blueprint()`. It runs the same topology checks as `.function_app` before
   creating any routes and returns a fresh Blueprint on each call.
2. Call `app.register_functions(blueprint)` **once**, during module import and
   before host indexing. Existing app decorators can run before or after that
   call, provided they also finish before indexing and do not conflict.
3. Late graph registrations, whether before or after `register_functions`, do
   **not** add routes to a previously exported Blueprint or parent app. Azure
   registration copies the current set of builders; it is not a live mount.
   Rebuild the application from scratch after changing graph registrations;
   do not register another full export onto the same app.
4. Handlers retain the adapter and its runtime dependencies. The route set is
   captured, but the adapter is not deep-copied: for example, health details
   reads its live registry. Do not mutate configuration after export. The
   standalone `.function_app` cache and its invalidation behavior are unchanged.

This initial Blueprint API supports **native classic HTTP only**, including
sync/async invocation, buffered SSE, state and health/readiness. Configured
`platform_compat`, `async_runs="durable"` or Service Bus registrations raise
`ValueError` rather than being dropped. This does not expand Platform, Durable,
Service Bus or true-streaming hosting support.

### Generated names and conflicts

These are the same names and paths as the standalone app. Paths are relative to
the existing host's `extensions.http.routePrefix` (`api` by default).

| Function name | Method | Relative route | Included when |
| --- | --- | --- | --- |
| `aflg_health` | GET | `health` | Always |
| `aflg_health_details` | GET | `health/details` | Always |
| `aflg_health_ready` | GET | `health/ready` | Readiness checks configured |
| `aflg_<name>_invoke` | POST | `graphs/<name>/invoke` | Each graph |
| `aflg_<name>_stream` | POST | `graphs/<name>/stream` | `stream=True` (default) |
| `aflg_<name>_state` | GET | `graphs/<name>/threads/{thread_id}/state` | Graph has supported state capability |

Reserve both function names and overlapping method/path combinations, including
parameterized and catch-all routes. Azure's SDK rejects duplicate function
names at indexing; a Blueprint cannot inspect the receiving app, and this API
does **not** detect or resolve HTTP route collisions. Do not rely on route
registration order. Rename or remove an existing conflicting route before
composition, or use a separate app if its public path cannot change. Health
routes cannot currently be disabled or renamed. Multiple complete adapter
Blueprints in one app collide on health function names even with different
graph names; use one adapter with multiple registered graphs instead.

`LangGraphApp(route_prefix=...)` changes **metadata only**: it must match
`host.json`; it does not add a Blueprint mount prefix or avoid collisions.

### Authorization and topology

Adapter auth is explicit on every generated route, so the existing app's
`http_auth_level` does not override it. Graph invoke/stream/state use a
registration-level override when supplied, otherwise `LangGraphApp.auth_level`
(default `FUNCTION`). Liveness uses `health_auth_level` (default `ANONYMOUS`);
health details and readiness default to the adapter's auth level unless their
respective overrides are set. Existing triggers keep their own auth levels.

Function keys do not identify users or authorize thread ownership; the existing
`authorize` callback still applies. Core Tools does not enforce keys by default,
so local successful HTTP requests are not deployed-auth proof. Lock/checkpointer
topology validation is unchanged and re-evaluated at export; it cannot validate
unrelated application triggers that independently write the same checkpoint
storage. Operators remain responsible for coordinating those writers.

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

# Forward route metadata to azure-functions-openapi
count = register_with_openapi(app)
print(f"Registered {count} routes with azure-functions-openapi")
```

The bridge reads route metadata from `LangGraphApp.get_app_metadata()` and forwards it
to `azure_functions_openapi.register_openapi_metadata()`. The Python import package
is `azure_functions_openapi`; the installable distribution is `azure-functions-openapi`
(the project repository retains the `azure-functions-openapi-python` name).
Install the companion through the adapter's extra:

```bash
pip install "azure-functions-langgraph[openapi]"
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
| 500 | Graph execution failed, or `Thread lock could not be acquired` / `Thread lock could not be released` when the thread lock backend raises (see [Troubleshooting](troubleshooting.md#native-invoke-or-stream-returns-500-thread-lock-could-not-be-released)) |
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
