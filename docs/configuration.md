# Configuration

## LangGraphApp options

The `LangGraphApp` constructor accepts the following parameters:

### `auth_level`

The Azure Functions authentication level for all registered routes.

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

# Default: FUNCTION (requires an Azure Functions key on every request)
app = LangGraphApp()

# Public / local-dev only — emits UserWarning
app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)

# Require admin (host master) key
app = LangGraphApp(auth_level=func.AuthLevel.ADMIN)
```

Available levels:

| Level | Description |
|-------|-------------|
| `ANONYMOUS` | No authentication (emits `UserWarning` when opted-in) |
| `FUNCTION` | Requires a function-specific API key (**default**) |
| `ADMIN` | Requires the master host key |

### `max_request_body_bytes`

Maximum accepted HTTP request body size before graph execution. Defaults to
`1_048_576` bytes (1 MiB); oversized requests receive HTTP 413.

### `max_stream_response_bytes`

Maximum buffered SSE body size for classic `LangGraphApp` stream routes.
Defaults to `1_048_576` bytes (1 MiB). When the next frame would cross the
limit, the response ends with in-band `error` and `end` SSE events.

### `max_input_depth`

Maximum nesting depth accepted in the request's input and configuration values.
Defaults to `32`; deeper input is rejected before graph execution.

### `max_input_nodes`

Maximum number of mapping keys and sequence elements traversed across the
request's input and configuration values. Defaults to `10_000`; larger input is
rejected before graph execution.

### `max_platform_result_limit`

Maximum caller-supplied `limit` for Platform-compatible assistant search,
thread search, and thread history requests. Defaults to `100`. Omitted request
limits keep their existing default of `10`; values above the configured maximum
receive a structured HTTP 422 response.

```python
app = LangGraphApp(
    max_request_body_bytes=2 * 1024 * 1024,
    max_stream_response_bytes=4 * 1024 * 1024,
    max_input_depth=48,
    max_input_nodes=20_000,
    max_platform_result_limit=100,
)
```

## Graph registration

The `register()` method accepts:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `graph` | Any (must satisfy `InvocableGraph` protocol) | Yes | A compiled LangGraph graph or any object with an `invoke()` method |
| `name` | `str` | Yes | Unique name used in URL routes (`/api/graphs/{name}/invoke`) |
| `description` | `str` or `None` | No | Human-readable description shown in the `/api/health/details` endpoint |

```python
app = LangGraphApp()

# Basic registration
app.register(graph=compiled_graph, name="my_agent")

# With description
app.register(
    graph=compiled_graph,
    name="my_agent",
    description="Customer support agent with RAG",
)
```

### Multiple graphs

You can register multiple graphs on a single `LangGraphApp`:

```python
app = LangGraphApp()
app.register(graph=support_graph, name="support")
app.register(graph=sales_graph, name="sales")
app.register(graph=triage_graph, name="triage")

func_app = app.function_app
```

This creates endpoints for all three:

- `POST /api/graphs/support/invoke`
- `POST /api/graphs/support/stream`
- `POST /api/graphs/sales/invoke`
- `POST /api/graphs/sales/stream`
- `POST /api/graphs/triage/invoke`
- `POST /api/graphs/triage/stream`
- `GET /api/health` (liveness probe; `GET /api/health/details` for the graph inventory, protected by default)

## Checkpointer configuration

### Native client configuration admission

Both `LangGraphApp` and `StreamingLangGraphApp` accept only
`config.configurable`, with `thread_id` as the sole default client-selectable key.
Unapproved keys return sanitized HTTP 400 before locking or graph/observer start.
Explicitly expose safe application fields with the keyword-only registration option:

```python
app.register(compiled_graph, "agent", allowed_configurable_keys={"locale"})
```

The registration snapshots these names as a `frozenset`. Internal `__*` names
remain forbidden even if listed, and the option never permits top-level controls
such as `recursion_limit` or `callbacks`. Bind trusted execution defaults on the
server, for example with `graph.with_config(...)`.

**Breaking true-stream migration:** formerly accepted arbitrary configuration on
`StreamingLangGraphApp` is now rejected, matching the classic native policy.
Move execution controls to trusted server configuration and explicitly allow
only application keys whose values untrusted clients may safely choose.
Opting in to configurable `checkpoint_id`/`checkpoint_ns` grants checkpoint
selection; enforce ownership externally on the streaming transport, which still
has no resource authorizer. An allow-list is not authentication. Platform,
Durable and Service Bus policies are unchanged.

Checkpointers are configured on the LangGraph side, not in `LangGraphApp`. The library passes admitted `config` through to the graph:

```python
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()
graph = builder.compile(checkpointer=checkpointer)

app = LangGraphApp()
app.register(graph=graph, name="stateful_agent")
```

Then pass `thread_id` in requests:

```json
{
    "input": {"messages": [{"role": "human", "content": "Hello"}]},
    "config": {
        "configurable": {"thread_id": "user-session-123"}
    }
}
```

The detailed health endpoint (`GET /api/health/details`, protected by default) reports whether each graph has a checkpointer:

```json
{
    "status": "ok",
    "graphs": [
        {"name": "stateful_agent", "description": null, "has_checkpointer": true}
    ]
}
```

## Azure Functions host configuration

The package works with the standard Azure Functions `host.json`:

```json
{
    "version": "2.0",
    "extensionBundle": {
        "id": "Microsoft.Azure.Functions.ExtensionBundle",
        "version": "[4.*, 5.0.0)"
    }
}
```

No special configuration is required in `host.json` for this package.
