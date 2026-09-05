# Production Persistent Agent (Azure OpenAI + Blob checkpointer)

**The capstone example — the fourth and final step of the adoption learning
path.** It composes everything the three preceding examples introduced into one
production-oriented Azure Functions app:

| Step | Example | What it adds |
| --- | --- | --- |
| 1 | [`azure_openai_agent`](../azure_openai_agent/) | A **real** Azure OpenAI chat agent (API-key or Managed Identity auth). |
| 2 | [`conversation_memory`](../conversation_memory/) | **State across requests** via a checkpointer keyed by `thread_id`. |
| 3 | [`managed_identity_storage`](../managed_identity_storage/) | Azure-native **durable** storage + Managed Identity wiring. |
| **4** | **`production_persistent_agent`** *(this example)* | A **real agent whose memory survives restarts and scale-out**, on Azure Blob, with Managed Identity in prod and Azurite locally. |

The result: a genuine Azure OpenAI agent that **remembers each conversation
across process restarts and across Function instances**, because its state lives
in Azure Blob Storage rather than in memory.

> Prefer to keep learning with the deterministic building blocks? This example
> **adds** a live-model composition; it does not replace
> [`persistent_agent_blob_table`](../persistent_agent_blob_table/) or
> [`managed_identity_storage`](../managed_identity_storage/), which stay
> hermetic (echo, no LLM) for storage-focused testing.

## Architecture at a glance

```text
HTTP request  (POST /api/graphs/production_persistent_agent/invoke)
  + config.configurable.thread_id
      -> LangGraphApp loads prior state for thread_id from Azure Blob
      -> chat node (Azure OpenAI sees the whole conversation)
      -> LangGraphApp checkpoints the new state back to Azure Blob
  -> HTTP response
```

`graph.py` is **normal LangGraph code** — a message-state `StateGraph` with one
Azure OpenAI chat node and a `build_graph()` factory. It is deliberately
checkpointer-agnostic. `function_app.py` owns all the wiring: it builds the Blob
`ContainerClient`, wraps it in `AzureBlobCheckpointSaver`, and compiles the graph
*with* that checkpointer. That separation is why the same graph can be
smoke-tested with no storage at all.

## Terminology: three different "state" layers

These are easy to conflate. This example uses **only the first**; the other two
are optional:

| Layer | What it stores | In this example |
| --- | --- | --- |
| **Checkpointer** (LangGraph `BaseCheckpointSaver`) | Per-`thread_id` **graph execution state** (the messages/channels). This is what gives the agent memory. | **`AzureBlobCheckpointSaver`** — the core of this example. |
| **Platform `ThreadStore`** (`AzureTableThreadStore`) | Thread **metadata** + run locks for the SDK-compatible Platform endpoints (`/threads`, `/runs`). Not the conversation itself. | **Optional**, behind `LANGGRAPH_ENABLE_PLATFORM=true`. |
| **LangGraph `BaseStore`** | Long-term, **cross-thread** memory (facts shared between conversations). | **Out of scope** — see [Out of scope](#out-of-scope). |

The key takeaway: **graph-state persistence does not require the Platform layer.**
The native `invoke`/`stream`/`state` endpoints persist per-thread memory using
the checkpointer alone.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4+
- Python 3.10+
- [Docker](https://www.docker.com/) (for Azurite locally)
- *(Optional)* an **Azure OpenAI** resource with a chat deployment. Without one,
  the example runs against a deterministic fake model, so you can still observe
  persistence end-to-end with zero credentials.

## Run locally with Azurite

Start Azurite (only the Blob port `10000` is required for this example; expose
`10002` too if you enable the optional Platform/Table layer):

```bash
docker run -d --name azurite \
  -p 10000:10000 -p 10001:10001 -p 10002:10002 \
  mcr.microsoft.com/azure-storage/azurite
```

Then start the app:

```bash
cd examples/production_persistent_agent
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

`local.settings.json.example` ships
`AZURE_STORAGE_CONNECTION_STRING=UseDevelopmentStorage=true` and
`LANGGRAPH_AUTO_CREATE_STORAGE=true`, so it runs hermetically against Azurite and
bootstraps the checkpoint container on first start. Fill in the `AZURE_OPENAI_*`
values to use a real deployment (same variables and auth paths as the
[`azure_openai_agent`](../azure_openai_agent/#configuration) example); leave them
blank to use the fake model.

## Prove persistence in 6 steps

This is the whole point of the example. State is keyed by `thread_id`, so pass
one in `config.configurable.thread_id`:

**1. Start the host** (`func start`, as above).

**2. Tell the agent a fact on thread `demo-user`:**

```bash
curl -s -X POST http://localhost:7071/api/graphs/production_persistent_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "My name is Ada and I work on Azure Functions."}]},
       "config": {"configurable": {"thread_id": "demo-user"}}}'
```

**3. Ask it to recall — same `thread_id`:**

```bash
curl -s -X POST http://localhost:7071/api/graphs/production_persistent_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "What is my name and what do I work on?"}]},
       "config": {"configurable": {"thread_id": "demo-user"}}}'
```

With a real deployment the reply references *Ada* and *Azure Functions* — the
prior turn was loaded from Blob and fed to the model. (With the fake model the
recall text is canned, but the **state** is still round-tripped through Blob;
inspect it via step 5.)

**4. Restart the host** — stop `func start` (Ctrl-C) and run it again. This
throws away all in-process memory; only Azure Blob survives.

**5. Show the state survived the restart** — read it straight from the
checkpointer via the native state endpoint (no model call needed):

```bash
curl -s "http://localhost:7071/api/graphs/production_persistent_agent/threads/demo-user/state"
```

The returned `values.messages` still contains the *"My name is Ada…"* turn from
**before** the restart. Re-run step 3 and the agent still recalls the fact.

**6. Show cross-thread isolation** — a different `thread_id` starts empty:

```bash
curl -s "http://localhost:7071/api/graphs/production_persistent_agent/threads/someone-else/state"
```

`someone-else` has no memory of Ada — each `thread_id` is an isolated
conversation. That is the same mechanism you would use to separate users or
sessions in production.

> **Response envelope.** `invoke` returns `{"output": <graph result>}`. Because
> the graph uses LangGraph's `add_messages` state, each item in `messages` is a
> **serialized LangChain message** (`content`, `type`, `additional_kwargs`, …),
> not a bare `{"role", "content"}` pair. The state endpoint returns
> `{"values", "next", "metadata"}`.

## Multi-instance & concurrency in production

Durable state is necessary but not sufficient for horizontal scale-out. On
Consumption / Elastic Premium your app runs on **many instances**, so also mind:

- **Single-writer checkpointer.** `AzureBlobCheckpointSaver` is a single-writer
  store. Concurrent writes to the **same `thread_id`** from different instances
  can race. The native endpoints take an **in-process** per-thread lock, which
  does **not** coordinate across instances.
- **Distributed locking.** For multi-instance safety, either wire a distributed
  `ThreadLock` (the package ships `AzureBlobLeaseThreadLock`, backed by Azure
  Blob leases) **or** route through Platform-compatible runs
  (`LANGGRAPH_ENABLE_PLATFORM=true`) with `AzureTableThreadStore`, which uses
  ETag-based atomic run locks. Set `AZFUNC_LANGGRAPH_LOCK_BACKEND=distributed`
  to fail-fast at startup if you forgot. See
  [README → Distributed thread locking](../../README.md#distributed-thread-locking-v06)
  and [`docs/production-guide.md`](../../docs/production-guide.md#distributed-thread-locking).
- **Functions timeout.** A graph run must finish inside the Functions execution
  timeout (Consumption default 5 min, max 10; Premium/Dedicated configurable).
  Long tool loops or slow model calls can hit it — budget accordingly.
- **Buffered SSE, not token streaming.** The `/stream` endpoints return
  **buffered** SSE: chunks are collected during the run and flushed *after* it
  completes. This is the adapter's current design, not a platform limit. For
  real-time token streaming, run behind a long-lived host. See
  [README → Streaming behavior](../../README.md#streaming-behavior).

## Storage scale envelope & retention

`AzureBlobCheckpointSaver` lists checkpoints via blob prefix scans, so cost and
latency grow with checkpoints-per-thread. Keep it bounded:

- **Comfortable:** < 100 checkpoints/thread, < 10K threads.
- **Retention helpers** — prune from a Timer-triggered Function:
  `saver.delete_old_checkpoints(thread_id=..., keep_last=50)` and, as a second
  pass, `saver.collect_orphaned_values(thread_id=..., dry_run=False)`.

Full details: [README → Scale envelope](../../README.md#scale-envelope) and
[README → Retention helpers](../../README.md#retention-helpers).

### When to switch backends

Blob is the right default for development and small-to-medium production. Reach
for a database checkpointer when you outgrow it — **don't duplicate; reuse the
existing DX helpers and examples**:

| Situation | Backend | Example |
| --- | --- | --- |
| Very high write QPS / existing Postgres infra | `create_postgres_checkpointer` | [`postgres_checkpoint_production`](../postgres_checkpoint_production/) |
| Azure-native, globally distributed, serverless RU | `create_cosmos_checkpointer` | [`cosmos_checkpoint_azure`](../cosmos_checkpoint_azure/) |
| Local dev / single instance | `create_sqlite_checkpointer` | [`sqlite_checkpoint_local`](../sqlite_checkpoint_local/) |

See [README → DB checkpointer backends](../../README.md#db-checkpointer-backends).

## Checkpoint store security

Checkpoint blobs can restore **arbitrary Python types** via LangGraph's default
serializer — treat the store as part of your threat model. Restrict access with
RBAC / Managed Identity and private endpoints, and set
`LANGGRAPH_STRICT_MSGPACK=true` (an **app setting**, read at import time). See
[README → Checkpoint store security](../../README.md#checkpoint-store-security).

## Deploy to Azure with Managed Identity

Application code does not change between local and Azure — you switch from a
connection string to Managed Identity purely via app settings. This is the
recommended production wiring: **no secrets in App Settings.**

### 1. Enable a Managed Identity

```bash
az functionapp identity assign --name <function-app> --resource-group <rg>
```

### 2. Grant role assignments

```bash
PRINCIPAL_ID=$(az functionapp identity show -n <function-app> -g <rg> --query principalId -o tsv)
STORAGE_ID=$(az storage account show -n <storage-account> -g <rg> --query id -o tsv)

# Checkpointer (Blob) — always required
az role assignment create --role "Storage Blob Data Contributor" \
  --assignee "$PRINCIPAL_ID" --scope "$STORAGE_ID"

# Azure OpenAI — for the Entra ID auth path
OPENAI_ID=$(az cognitiveservices account show -n <openai-resource> -g <rg> --query id -o tsv)
az role assignment create --role "Cognitive Services OpenAI User" \
  --assignee "$PRINCIPAL_ID" --scope "$OPENAI_ID"

# Only if you enable the optional Platform/Table layer
az role assignment create --role "Storage Table Data Contributor" \
  --assignee "$PRINCIPAL_ID" --scope "$STORAGE_ID"
```

### 3. Pre-create the container (recommended)

```bash
az storage container create --account-name <storage-account> \
  --name langgraph-checkpoints --auth-mode login
```

Pre-creating avoids cold-start side effects and RBAC-propagation timing issues,
so you can leave `LANGGRAPH_AUTO_CREATE_STORAGE` **unset** in production.

### 4. Set App Settings (no secrets)

```bash
az functionapp config appsettings set --name <function-app> --resource-group <rg> --settings \
  AZURE_STORAGE_BLOB_ACCOUNT_URL="https://<storage-account>.blob.core.windows.net" \
  LANGGRAPH_BLOB_CONTAINER="langgraph-checkpoints" \
  AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com/" \
  AZURE_OPENAI_DEPLOYMENT="<chat-deployment>" \
  AZURE_OPENAI_USE_ENTRA_ID="true"
```

Do **not** set `AZURE_STORAGE_CONNECTION_STRING` or `AZURE_OPENAI_API_KEY` in
production — presence of `AZURE_STORAGE_BLOB_ACCOUNT_URL` selects the Managed
Identity branch, and leaving the key unset selects the Entra ID auth path. RBAC
role assignments can take 1–5 minutes to propagate; a first-request
`403 Forbidden` usually just means you deployed too soon.

> **Production auth:** `LangGraphApp` defaults to `AuthLevel.FUNCTION`, so the
> deployed endpoints require a function key (`?code=<FUNCTION_KEY>` or the
> `x-functions-key` header).

> **Cost:** this example invokes a **billable** Azure OpenAI deployment — every
> request consumes tokens against your quota.

### Local dev against real Azure (instead of Azurite)

`DefaultAzureCredential` also picks up your `az login` session, so the exact
production wiring works from your workstation:

```bash
az login
export AZURE_STORAGE_BLOB_ACCOUNT_URL="https://<storage-account>.blob.core.windows.net"
unset AZURE_STORAGE_CONNECTION_STRING   # force the Managed Identity branch
func start
```

Your `az login` identity needs the same `Storage Blob Data Contributor` role.

## Optional: enable the Platform-compatible layer

Set `LANGGRAPH_ENABLE_PLATFORM=true` to also expose the SDK-compatible
`/threads`, `/runs`, and `/assistants` endpoints backed by an
`AzureTableThreadStore` (which additionally provides ETag-based **distributed run
locking**). This is off by default because the native endpoints already deliver
durable per-thread memory — enable it only if you use the `langgraph-sdk` client
or want SDK-compatible thread/run lifecycle management. See
[`managed_identity_storage`](../managed_identity_storage/) and
[`platform_compat_sdk`](../platform_compat_sdk/).

## CI / credential-free runs

Unless Azure OpenAI is **fully** configured — endpoint, deployment, **and** an
auth method — `graph.py` builds a deterministic `GenericFakeChatModel` instead of
calling Azure OpenAI, and never imports `langchain_openai`. The automated test
(`tests/test_production_persistent_agent_example.py`) uses a deterministic fake
model and an Azurite-backed checkpointer to prove state **survives recreation of
the app/checkpointer object** against the same storage; it skips cleanly when
Azurite is unavailable.

## Out of scope

Intentionally excluded (kept simple, or belongs elsewhere):

- **RAG / Azure AI Search.**
- **Long-term cross-thread `BaseStore` memory** (facts shared across
  conversations) — this example persists per-`thread_id` graph state only.
- **True token streaming** — the adapter uses buffered SSE.
- **Durable async run lifecycle** and **Service Bus trigger integration.**

## See also

- [`managed_identity_storage`](../managed_identity_storage/) — Blob **and** Table
  wiring for the full Platform layer; the deterministic sibling of this example.
- [`persistent_agent_blob_table`](../persistent_agent_blob_table/) — same
  persistence driven by a connection string only.
- [README → Persistent storage](../../README.md#persistent-storage-v04) and
  [`docs/production-guide.md`](../../docs/production-guide.md) — narrative docs.
