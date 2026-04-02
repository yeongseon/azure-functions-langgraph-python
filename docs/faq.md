# FAQ

## Can I register an uncompiled StateGraph directly?

No. You must call `.compile()` on your `StateGraph` before passing it to `register()`. This is by design — the library is a deployment adapter, not a graph builder. Compiling the graph ensures checkpointers are configured and the graph is validated before registration.

```python
# Correct
graph = builder.compile()
app.register(graph=graph, name="agent")

# Wrong — will raise TypeError
app.register(graph=builder, name="agent")
```

## Does this support true SSE streaming?

Not in v0.1. The stream endpoint collects all chunks from `graph.stream()` into memory, then returns them as a single SSE-formatted HTTP response. This is a limitation of the Azure Functions Python worker, which does not support chunked transfer encoding.

True streaming support is planned for a future release.

## How does thread_id work?

Thread IDs enable conversation state through LangGraph's checkpointer mechanism. Pass `thread_id` in the request body under `config.configurable`:

```json
{
    "input": {"messages": [{"role": "human", "content": "Hello"}]},
    "config": {"configurable": {"thread_id": "session-123"}}
}
```

The library passes `config` directly to `graph.invoke()` or `graph.stream()`. The checkpointer (configured when compiling the graph) handles state persistence.

## Is LangGraph required at import time?

The `LangGraphApp` class uses a lazy import. You can `import azure_functions_langgraph` without `langgraph` installed — the `ImportError` only occurs when you access `LangGraphApp` without the required dependencies.

However, `langgraph` is listed as a dependency and will be installed automatically with `pip install azure-functions-langgraph`.

## Can I use this with any graph, not just LangGraph?

Yes. The library uses `typing.Protocol` for graph acceptance. Any object with an `invoke(input, config)` method satisfies `InvocableGraph` and can be registered. If it also has a `stream(input, config, stream_mode)` method, it satisfies `LangGraphLike` and the stream endpoint works too.

## Can I register multiple graphs?

Yes. Each graph gets its own set of endpoints:

```python
app = LangGraphApp()
app.register(graph=support_graph, name="support")
app.register(graph=sales_graph, name="sales")
```

This creates:

- `POST /api/graphs/support/invoke`
- `POST /api/graphs/support/stream`
- `POST /api/graphs/sales/invoke`
- `POST /api/graphs/sales/stream`
- `GET /api/health` (lists all)

## What authentication options are available?

`LangGraphApp` supports Azure Functions authentication levels:

- `AuthLevel.ANONYMOUS` (default) — no authentication
- `AuthLevel.FUNCTION` — requires function key
- `AuthLevel.ADMIN` — requires master key

```python
import azure.functions as func

app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
```

## How is this different from LangGraph Platform?

| Feature | LangGraph Platform | azure-functions-langgraph |
|---------|-------------------|--------------------------|
| Hosting | LangChain Cloud (paid) | Your Azure subscription |
| Streaming | True SSE | Buffered SSE (v0.1) |
| Threads | Built-in | Via LangGraph checkpointer |
| Infrastructure | Managed | Azure Functions (serverless) |
| Cost model | Per-seat/usage | Azure Functions pricing |

## Does this work with Azure Functions v1?

No. This package requires the Azure Functions Python **v2 programming model** (the decorator-based model using `azure.functions.FunctionApp`).

## What Python versions are supported?

Python 3.10, 3.11, 3.12, 3.13, and 3.14.
