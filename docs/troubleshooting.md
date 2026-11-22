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
unqualified success, and so does a release the store refuses as stale: the
update no longer owned the thread because `reset_stale_locks` reclaimed it
while `update_state` was running, or because another run acquired it (and
possibly finished) in the meantime. The bundled stores report exactly whether
the release was applied, so this holds even when the reclaim left the thread at
the status the update asked for; a custom store that cannot say so is judged by
the returned thread alone (see the adapter reference). If the graph update
itself failed, its original error response is preserved and secondary cleanup
failures are logged. State updates do not emit run-lifecycle observer events.

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

### Platform state update returns 409 for a thread that just finished running

`POST /threads/{thread_id}/state` answers 409 `State updates are not supported
while a run is active` when it reads the thread as `busy`, which is what a thread
with an active run gets. The handler reads the thread before it takes any lock
and goes back to the status it read, and a store refuses to release to `busy`. If
the run finished between that read and the moment the update would take the store
lock, the terminal status that run left cannot be recovered (taking the lock
overwrites it), so the update is refused before it takes a lock or runs
`update_state`. This request changes nothing and creates no lock token; unless
another writer changes the thread, it keeps the terminal status and the cleared
token that the finished run left. Retry the request once the thread is available.
A thread that is still busy gets the same 409.

### A second Durable run on the same thread skips the graph

Symptom: two runs on one `thread_id` where the second run returns the first run's
state and its nodes never execute (no tool call, no new checkpoint). Cause: the
create-run body's `config.configurable` carries a `run_id` key (for example
`{"configurable": {"run_id": "job-1"}}`) and both runs send the same value.

This is LangGraph's own reserved-key behaviour, not something this adapter does.
The adapter never writes `run_id` into `config`; it only adds `thread_id`. In
LangGraph, `run_id` (with `thread_id`, `checkpoint_id`, `checkpoint_ns`, `task_id`,
`assistant_id` and `graph_id`) is copied from `configurable` into the run
`metadata`, the checkpointer stores that metadata, and on the next invoke on the
thread LangGraph treats a run whose `metadata.run_id` equals the stored one as a
**re-entry into an ongoing run** (a resume) rather than a fresh run, so the new
input is not applied and the completed checkpoint is returned. Verified against
`langgraph` 1.2.11 (`langgraph/_internal/_config.py` `_PROPAGATE_TO_METADATA`;
`langgraph/pregel/_loop.py` `_first`); `tests/test_durable_replay_idempotency.py`
uses the key `app_run_id` for this reason.

Fix: do not put `run_id` in `config.configurable`. The Durable `run_id` is the
top-level create-run body field (and the Durable instance id), not a
`configurable` entry. For an application-level idempotency key use a name that
LangGraph does not reserve, such as `app_run_id` or `idempotency_key`.

## No-credentials native invocation troubleshooting

Newcomers starting with credential-free examples (such as `examples/simple_agent`) often encounter errors on their first HTTP invocation. Because the Azure Functions runtime abstracts request handling, it can be hard to tell whether an error comes from the HTTP transport envelope, native configuration admission, or the inner LangGraph graph itself.

This walkthrough uses the shipped [`examples/simple_agent`](../examples/simple_agent/) app—a deterministic two-node graph (`greet` → `farewell`) requiring **no Azure subscription, no storage emulator, and no model API keys**.

Platform (`/threads`, `/runs`, `/assistants`) and Durable (`async_runs="durable"`) endpoints use separate state stores, locking protocols, and admission rules and are intentionally out of scope here.

### Setup

Before starting the host, ensure dependencies are installed (see the
[`simple_agent` Run locally section](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/simple_agent/README.md#run-locally)):

```bash
cd examples/simple_agent
pip install -r requirements.txt
func start
```

### Step 1: Baseline valid invocation (200 OK)

In a second terminal, send a valid invocation request:

```bash
curl -X POST http://localhost:7071/api/graphs/simple_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""}}'
```

**Observed Response (`200 OK`):**

```json
{
  "output": {
    "messages": [
      {"role": "human", "content": "World"},
      {"role": "assistant", "content": "Hello, World! Goodbye!"}
    ],
    "greeting": "Hello, World!"
  }
}
```

The response includes the header `X-Request-ID: <request-id>` (a 32-character hex string)
matching the internal run ID.

### Step 2: Request schema error — missing input envelope (422 Unprocessable Entity)

A common mistake when transitioning from standalone Python `graph.invoke({...})` is sending
the bare state payload without the transport envelope:

```bash
curl -X POST http://localhost:7071/api/graphs/simple_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "human", "content": "World"}], "greeting": ""}'
```

**Observed Response (`422 Unprocessable Entity`):**

```json
{
  "error": "error",
  "detail": [
    {
      "loc": ["input"],
      "type": "missing"
    }
  ],
  "request_id": "<request-id>"
}
```

**Root cause & Fix:** The native adapter expects an `InvokeRequest` envelope:
`{"input": {...}, "config": {...}}`. Wrap your state dict inside `"input"`.

### Step 3: Configuration admission error — unapproved controls (400 Bad Request)

Native invoke and stream routes enforce an admission policy before execution, locking, or
authorization. Passing execution controls like `recursion_limit` or unapproved configurable
keys results in immediate rejection:

```bash
curl -X POST http://localhost:7071/api/graphs/simple_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{
    "input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""},
    "config": {"recursion_limit": 25}
  }'
```

**Observed Response (`400 Bad Request`):**

```json
{
  "error": "error",
  "detail": "Only config.configurable is client configurable",
  "request_id": "<request-id>"
}
```

Similarly, sending an unapproved key inside `config.configurable` returns:

```json
{
  "error": "error",
  "detail": "Unapproved config.configurable field",
  "request_id": "<request-id>"
}
```

**Root cause & Fix:** Client requests cannot override server execution limits. Bind defaults
on the server using `graph = graph.with_config({"recursion_limit": 25})`, or register
allowed keys via `app.register(graph, "agent", allowed_configurable_keys=frozenset({"locale"}))`.

### Step 4: Distinguishing adapter rejections from graph failures

| Status | Where failure occurs | Error shape in HTTP response | Where to look |
| --- | --- | --- | --- |
| **`400`** | Admission policy | `{"error": "error", "detail": "Only config.configurable is client configurable", "request_id": "..."}` | HTTP response body. Fix client payload or bind config on server. |
| **`422`** | Pydantic envelope validation | `{"error": "error", "detail": [{"loc": ["input"], "type": "missing"}], "request_id": "..."}` | HTTP response body. Ensure payload is wrapped in `{"input": {...}}`. |
| **`500`** | Graph execution inside a node | `{"error": "error", "detail": "Graph execution failed", "request_id": "..."}` | **Functions host console log**. Internal stack traces are intentionally redacted from the HTTP body to prevent data leaks. |

### Focused validation & Out-of-scope checks

- **Reproduced locally with:** Azure Functions Core Tools 4.8.0, Python 3.11/3.14, `azure-functions-langgraph` 0.13.0, and `examples/simple_agent`.
- **Intentionally not run:** Live Azure cloud deployments, Azurite storage emulator tests, Durable run orchestrations (`async_runs="durable"`), and Platform SDK routes (`/threads`, `/runs`), which are outside the native invocation scope.

## Storage and persistence settings

The Functions host has its own storage (`AzureWebJobsStorage`), which is separate from graph persistence (the checkpointer, the thread lock and the thread store). A failure at import or indexing time stops the host from registering routes: the host logs `Worker failed to index functions` and every route answers 404. A failure at the first request is different: `/api/health` answers 200 and only the first request that touches persistence fails. `/api/health` is a liveness check; it proves neither that storage nor the lock is usable.

| What you see | When it appears | What the host or Python shows (observed) | What to check |
| --- | --- | --- | --- |
| Postgres setting missing | Import, host indexing fails | Before the guard in `postgres_checkpoint_production`: `KeyError: 'LANGGRAPH_POSTGRES_CONNECTION_STRING'`. With the guard: `RuntimeError` saying `LANGGRAPH_POSTGRES_CONNECTION_STRING is not set or is empty.` | Set `LANGGRAPH_POSTGRES_CONNECTION_STRING` in `local.settings.json` or the Function App settings. |
| Postgres setting empty | Import, host indexing fails | Before the guard, the empty string reached libpq defaults and a local socket attempt: `connection failed: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: FATAL:  role "<your OS user name>" does not exist` (the role is the operating-system user name of the machine that runs the host). With the guard in this example, missing and empty now give the same message. | Same as above. |
| Postgres unreachable | Import, host indexing fails | `connection failed: connection to server at "127.0.0.1", port 1 failed: Connection refused`; the password does not appear. Setting `LANGGRAPH_POSTGRES_SETUP=true` failed at the connection, before any setup. | Host, port, firewall and `connect_timeout` in the connection string. |
| Neither blob URL nor connection string (`production_persistent_agent`) | Import, host indexing fails | `Set AZURE_STORAGE_BLOB_ACCOUNT_URL for Managed Identity, or AZURE_STORAGE_CONNECTION_STRING for Azurite/local dev.` | Set one of the two settings. |
| Malformed storage connection string | Client construction | `Connection string is either blank or malformed.` for both the Blob and Table SDK clients. Constructors also accepted trailing semicolons and an endpoint-only Blob string (an anonymous client); the Table client rejected a Blob-only string with `Connection string missing required connection details.` Accepting a string at construction does not prove it can authenticate. | Check the string format and that it carries the details for the service in use. |
| Unreachable Blob endpoint with `LANGGRAPH_AUTO_CREATE_STORAGE=true` | Import | 79 to 84 s at import with default SDK retries, then the host worker-metadata request timed out at about 60 s (`Requesting metadata from worker failed.`), the host recycled and no HTTP response was seen. | Pre-create the container and leave `LANGGRAPH_AUTO_CREATE_STORAGE` unset in production, as the example README says. |
| Missing container | First request | Health 200; the first native invoke returned 500 with an empty body. Host log: `ResourceNotFoundError: The specified container does not exist.` with `ErrorCode:ContainerNotFound`. | Create the checkpoint container, or enable auto-create in development. |
| Missing table | First request | Health 200; the first `POST /api/threads` returned 500 with an empty body. Host log: `ResourceNotFoundError` with `"code":"TableNotFound"`. | Create the thread table, or enable auto-create in development. |
| Table bootstrap warning | Import, first request may fail later | `Table create skipped at cold start (table=langgraphthreads): ... First real operation may fail if the table is missing.` It is emitted through Python logging and, in the observed run, was not present in the default host log. | Check that the Table endpoint is reachable and the table exists. |

### Where to look

The host log is the only place that names the cause of an empty 500 today (tracked in #928). Under the example's static startup error, the chained exception holds the SDK's own message and is the only place that names the cause of the startup failure; for an unreachable Blob endpoint it was observed in Python as `ServiceRequestError` with `Failed to establish a new connection: [Errno 111] Connection refused`. In the experiment, the synthetic credentials placed in the settings did not appear in the captured exception text, the logging output or the host log, for these versions and configurations; this is not a guarantee.

### Not reproduced

These could cause failures but were not observed, so they are possible causes, not observed symptoms:

- A missing managed-identity data role. A `403` alone does not prove a missing role: identity selection, role scope, propagation delay and network rules all produce one.
- A deployed app with an empty `AzureWebJobsStorage`.
- SAS-authenticated recovery.
- Storage URL userinfo variants.

These are local observations, not Azure certification: Core Tools 4.8.0, `azure-functions-langgraph` 0.13.0, `azure-storage-blob` 12.31.1, `azure-data-tables` 12.7.0, `azure-core` 1.41.0, `psycopg` 3.3.9 and Azurite 3.35.0, with local HTTP-only host runs.

## Getting help

- [GitHub Issues](https://github.com/yeongseon/azure-functions-langgraph-python/issues) — questions, bug reports, and feature requests
