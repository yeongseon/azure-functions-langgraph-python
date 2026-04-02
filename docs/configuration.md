# Configuration

## LangGraphApp options

The `LangGraphApp` constructor accepts the following parameters:

### `auth_level`

The Azure Functions authentication level for all registered routes.

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

# Default: anonymous (no authentication required)
app = LangGraphApp()

# Require function key
app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)

# Require admin key
app = LangGraphApp(auth_level=func.AuthLevel.ADMIN)
```

Available levels:

| Level | Description |
|-------|-------------|
| `ANONYMOUS` | No authentication (default) |
| `FUNCTION` | Requires a function-specific API key |
| `ADMIN` | Requires the master host key |

## Graph registration

The `register()` method accepts:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `graph` | Any (must satisfy `InvocableGraph` protocol) | Yes | A compiled LangGraph graph or any object with an `invoke()` method |
| `name` | `str` | Yes | Unique name used in URL routes (`/api/graphs/{name}/invoke`) |
| `description` | `str` or `None` | No | Human-readable description shown in health endpoint |

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
- `GET /api/health`

## Checkpointer configuration

Checkpointers are configured on the LangGraph side, not in `LangGraphApp`. The library passes `config` through to the graph:

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

The health endpoint reports whether each graph has a checkpointer:

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
