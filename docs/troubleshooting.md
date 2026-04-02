# Troubleshooting

## Common issues

### ImportError: LangGraphApp requires 'azure-functions' and 'langgraph'

This error occurs when `azure-functions` or `langgraph` is not installed in your environment.

**Fix:**

```bash
pip install azure-functions langgraph
```

Or install everything at once:

```bash
pip install azure-functions-langgraph
```

### TypeError: Graph must have an invoke() method

This error occurs when the object passed to `register()` does not satisfy the `InvocableGraph` protocol. The object must have an `invoke(input, config)` method.

**Common causes:**

- Passing an uncompiled `StateGraph` instead of calling `.compile()` first
- Passing a plain function instead of a compiled graph object

**Fix:**

```python
# Wrong — passing uncompiled graph
builder = StateGraph(AgentState)
builder.add_node("chat", chat)
app.register(graph=builder, name="agent")  # TypeError!

# Correct — compile first
graph = builder.compile()
app.register(graph=graph, name="agent")
```

### ValueError: Graph 'name' is already registered

Each graph name must be unique within a `LangGraphApp` instance.

**Fix:** Use distinct names for each graph:

```python
app.register(graph=graph_a, name="agent_a")
app.register(graph=graph_b, name="agent_b")
```

### 501: Graph does not support streaming

The stream endpoint returns 501 when the registered graph does not have a `stream()` method. This happens with invoke-only graph implementations.

**Fix:** Ensure your graph has both `invoke()` and `stream()` methods. All standard LangGraph `CompiledStateGraph` objects support both.

### Streaming responses arrive all at once

This is expected behavior in v0.1. Streaming is **buffered** — all chunks are collected and returned as a single SSE-formatted response. True chunked streaming is not yet supported by the Azure Functions Python worker.

### 422: Validation error

Request body does not match the expected schema.

**Common causes:**

- Missing `input` field in request body
- `input` is not a dictionary
- `stream_mode` is not a string

**Fix:** Ensure your request matches the expected format:

```json
{
    "input": {"messages": [{"role": "human", "content": "Hello"}]},
    "config": {"configurable": {"thread_id": "abc"}}
}
```

### 400: Invalid JSON body

The request body is not valid JSON.

**Fix:** Ensure your request includes `Content-Type: application/json` header and the body is valid JSON.

### Graph execution fails with 500

The graph raised an exception during `invoke()` or `stream()`. Check the Azure Functions logs for the full traceback.

**Common causes:**

- LLM API key not configured
- Network connectivity issues
- Invalid graph state

Check logs:

```bash
func start --verbose
```

## Getting help

- [GitHub Issues](https://github.com/yeongseon/azure-functions-langgraph/issues) — bug reports and feature requests
- [GitHub Discussions](https://github.com/yeongseon/azure-functions-langgraph/discussions) — questions and community support
