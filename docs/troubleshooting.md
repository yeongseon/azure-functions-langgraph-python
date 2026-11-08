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

This is expected for `LangGraphApp`, whose classic routes buffer all chunks before returning one SSE-formatted response. For incremental arrival, install `azure-functions-langgraph[streaming]` and deploy a separate `StreamingLangGraphApp`; its FastAPI/ASGI transport cannot be mixed with classic routes in one function app.

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

### 400: Native client configuration is rejected

Since v0.13.0, native invoke and stream routes on both `LangGraphApp` and
`StreamingLangGraphApp` accept only `config.configurable` from clients, with
`thread_id` as the sole default key. Unapproved configurable keys, unknown
configuration fields and client-supplied execution controls such as
`recursion_limit` produce a sanitized HTTP 400 before authorization, locking or
graph execution. This is distinct from malformed JSON or a graph failure.
Platform, Durable and Service Bus transports do not use this admission policy.

**Fix:** Bind trusted execution defaults on the server, and explicitly register
only application fields whose values are safe for clients to choose. For an
existing compiled `graph` and app:

```python
graph = graph.with_config({"recursion_limit": 25})
app.register(graph, "agent", allowed_configurable_keys=frozenset({"locale"}))
```

A client can then send:

```json
{
  "input": {"messages": [{"role": "human", "content": "Hello"}]},
  "config": {"configurable": {"thread_id": "abc", "locale": "en"}}
}
```

Do not work around the rejection by broadly trusting request configuration.
Internal `__*` keys remain forbidden. Allowing `checkpoint_id` or `checkpoint_ns`
grants checkpoint selection and requires corresponding ownership checks in the
transport's authorizer. An allowlist, a thread ID and an Azure Functions key do
not establish per-thread ownership.

See [native configuration admission](configuration.md#native-client-configuration-admission),
[Upgrading](adapter-reference.md#upgrading) and the
[v0.13.0 release notes](https://github.com/yeongseon/azure-functions-langgraph-python/releases/tag/v0.13.0).

### RuntimeError at startup: Platform thread records are process-local

With `platform_compat=True`, accessing `app.function_app` raises `RuntimeError`
if the thread store is still `InMemoryThreadStore` and either
`AZFUNC_LANGGRAPH_THREAD_STORE` has a non-empty value other than `inmemory`, or
`AZFUNC_LANGGRAPH_LOCK_BACKEND` has a non-empty value other than `inprocess`
(for example `distributed`). The lock-backend check was
added in v0.13.0: a distributed lock alone does not share thread records across
workers. This startup failure is separate from a request-time HTTP 400.

**Fix for scale-out:** Configure a shared thread store such as
`AzureTableThreadStore` on `app.thread_store` before accessing
`app.function_app`, together with a suitable distributed thread lock and
checkpoint store. Follow the [persistent storage setup](adapter-reference.md#persistent-storage-v04)
and [production locking guide](production-guide.md#distributed-thread-locking).
Do not disable the guard to hide a multi-instance configuration problem.

**Fix for single-process local development:** Use the local in-process setup
and unset both guard variables. This is a local development path, not a safe
scale-out configuration. Shared stores and locks do not provide exactly-once
execution or replace application authorization and per-thread ownership checks.

See [Upgrading](adapter-reference.md#upgrading) and the
[v0.13.0 release notes](https://github.com/yeongseon/azure-functions-langgraph-python/releases/tag/v0.13.0).

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

### Platform state update returns 500 during lock release

`POST /threads/{thread_id}/state` returns the sanitized error
`Internal error releasing thread state lock` if the update succeeded but the
store-backed or outer thread lock could not be released. A deleted thread or a
store release that leaves another owner's lock intact also prevents an
unqualified success. If the graph update itself failed, its original error
response is preserved and secondary cleanup failures are logged. State updates
do not emit run-lifecycle observer events.

This response does **not** imply rollback: the checkpoint may already be
committed. Before retrying, read the thread state and checkpoint/history to
reconcile the intended change. Do not blindly repeat a potentially
non-idempotent update; the adapter does not promise exactly-once updates.

Inspect server logs for the failing cleanup boundary. While the store row is
still `busy`, a subsequent update is rejected with 409. Recover ownership only
after confirming the old writer has stopped, using the captured owner token or
the Table store's expired-lock recovery procedure. Never clear a newer owner's
lock to make a retry succeed. Successful cleanup restores the prior terminal
status (`idle`, `interrupted`, or `error`); a failed graph update targets `error`.

## Getting help

- [GitHub Issues](https://github.com/yeongseon/azure-functions-langgraph-python/issues) — questions, bug reports, and feature requests
