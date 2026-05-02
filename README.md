# Azure Functions LangGraph

> Part of the **Azure Functions Python DX Toolkit** — dogfood-tested by [azure-functions-cookbook-python](https://github.com/yeongseon/azure-functions-cookbook-python).


[![PyPI](https://img.shields.io/pypi/v/azure-functions-langgraph.svg)](https://pypi.org/project/azure-functions-langgraph/)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/azure-functions-langgraph/)
[![CI](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml)
[![Release](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/publish-pypi.yml)
[![Security Scans](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml)
[![codecov](https://codecov.io/gh/yeongseon/azure-functions-langgraph/branch/main/graph/badge.svg)](https://codecov.io/gh/yeongseon/azure-functions-langgraph)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://pre-commit.com/)
[![Docs](https://img.shields.io/badge/docs-gh--pages-blue)](https://yeongseon.github.io/azure-functions-langgraph-python/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Read this in: [한국어](README.ko.md) | [日本語](README.ja.md) | [简体中文](README.zh-CN.md)

> **Beta Notice** — This package is under active development. Core APIs are stabilizing but may still change before v1.0. Please report issues on GitHub.

Deploy [LangGraph](https://github.com/langchain-ai/langgraph) graphs as **Azure Functions** HTTP endpoints with minimal boilerplate.

---

Part of the **Azure Functions Python DX Toolkit**

## Why this exists

Deploying LangGraph on Azure Functions is harder than it should be.

- LangGraph does not provide an Azure Functions-native deployment adapter
- Exposing compiled graphs as HTTP endpoints requires repetitive wiring
- Teams often rebuild the same invoke/stream wrapper for every project

This package provides a focused adapter for serving LangGraph graphs on Azure Functions Python v2.

## What it does

- **Zero-boilerplate deployment** — register a compiled graph, get HTTP endpoints automatically
- **Invoke endpoint** — `POST /api/graphs/{name}/invoke` for synchronous execution
- **Stream endpoint** — `POST /api/graphs/{name}/stream` for buffered SSE responses
- **Health endpoint** — `GET /api/health` listing registered graphs with checkpointer status
- **Checkpointer pass-through** — thread-based conversation state works via LangGraph's native config
- **State endpoint** — `GET /api/graphs/{name}/threads/{thread_id}/state` for thread state inspection (when supported)
- **Per-graph auth** — override app-level auth with `register(..., auth_level=...)`
- **LangGraph Platform API compatibility** — SDK-compatible endpoints for threads, runs, assistants, and state (v0.3+)
- **Persistent storage backends** — Azure Blob Storage checkpointer and Azure Table Storage thread store (v0.4+)

## LangGraph Platform comparison

| Feature | LangGraph Platform | azure-functions-langgraph |
|---------|-------------------|--------------------------|
| Hosting | LangChain Cloud (paid) | Your Azure subscription |
| Assistants | Built-in | SDK-compatible API (v0.3+) |
| Thread lifecycle | Built-in | Create, get, update, delete, search, count (v0.3+) |
| Runs | Built-in | Threaded + threadless runs (v0.4+) |
| State read/update | Built-in | get_state + update_state (v0.4+) |
| State history | Built-in | Checkpoint history with filtering (v0.4+) |
| Streaming | True SSE | Buffered SSE |
| Persistent storage | Built-in | Azure Blob + Table Storage (v0.4+) |
| Infrastructure | Managed | Azure Functions (serverless) |
| Cost model | Per-seat/usage | Azure Functions pricing |

> See [COMPATIBILITY.md](COMPATIBILITY.md) for the per-feature SDK support matrix, including which `RunCreate` fields, thread filters, and SDK calls return `501 Not Implemented`.

## Scope

- Azure Functions Python **v2 programming model**
- LangGraph graph deployment and HTTP exposure
- LangGraph runtime concerns: invoke, stream, threads, runs, and state
- Optional integration points for validation and OpenAPI via companion packages

This package is a **deployment adapter** — it wraps LangGraph, it does not replace it.

> Internally, graph registration remains protocol-based (`LangGraphLike`), so any object satisfying the protocol works — but the package's documentation and examples focus on LangGraph use cases.

## What this package does not do

This package does not own:
- OpenAPI document generation or Swagger UI — use [`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python)
- Request/response validation beyond LangGraph contracts — use [`azure-functions-validation-python`](https://github.com/yeongseon/azure-functions-validation-python)
- Generic graph-serving abstractions beyond LangGraph

> **Note:** For OpenAPI spec generation, use the [`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python) package with the bridge module (`azure_functions_langgraph.openapi.register_with_openapi`).

## Installation

```bash
pip install azure-functions-langgraph
```

For persistent storage with Azure services:

```bash
# Azure Blob Storage checkpointer
pip install azure-functions-langgraph[azure-blob]

# Azure Table Storage thread store
pip install azure-functions-langgraph[azure-table]

# Both
pip install azure-functions-langgraph[azure-blob,azure-table]
```

For database or Cosmos DB checkpointer backends:

```bash
# Postgres checkpointer
pip install azure-functions-langgraph[postgres]

# SQLite checkpointer (local dev)
pip install azure-functions-langgraph[sqlite]

# Cosmos DB checkpointer (requires Python 3.11+)
pip install azure-functions-langgraph[cosmos]
```

Your Azure Functions app should also include:

```text
azure-functions
langgraph
azure-functions-langgraph
```

For local development:

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
cd azure-functions-langgraph
pip install -e .[dev]
```

## Quick Start

```python
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

import azure.functions as func

from azure_functions_langgraph import LangGraphApp


# 1. Define your state
class AgentState(TypedDict):
    messages: list[dict[str, str]]


# 2. Define your nodes
def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


# 3. Build graph
builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile()

# 4. Deploy (ANONYMOUS for local dev; use FUNCTION in production — see below)
app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
app.register(graph=graph, name="echo_agent")
func_app = app.function_app  # ← use this as your Azure Functions app
```

Start the Functions host locally:

```bash
func start
```

### Verify locally and on Azure

After deploying (see [docs/deployment.md](docs/deployment.md)), the same request produces the same response in both environments. Azure requires a function key (`?code=<FUNCTION_KEY>`) when `auth_level` is set to `FUNCTION`.

#### Local

```bash
curl -s http://localhost:7071/api/health
```

```json
{"status": "ok", "graphs": [{"name": "echo_agent", "description": null, "has_checkpointer": false}]}
```

#### Azure

```bash
curl -s "https://<your-app>.azurewebsites.net/api/health?code=<FUNCTION_KEY>"
```

```json
{"status": "ok", "graphs": [{"name": "echo_agent", "description": null, "has_checkpointer": false}]}
```

> Response format verified against a temporary Azure Functions deployment of the `simple_agent` example in koreacentral (Python 3.12, Consumption plan). The Quick Start uses `echo_agent` for illustration; the health endpoint returns the same JSON structure regardless of graph name. URL anonymized.


### Production authentication

> **Important:** `LangGraphApp` defaults to `AuthLevel.ANONYMOUS` for local development
> convenience. This default will change to `AuthLevel.FUNCTION` in v1.0.
> For production deployments, **always** set `auth_level` explicitly:

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

# Production: require function key authentication
app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
```

### Streaming behavior

> **Important:** All `/stream` endpoints (both the native `POST /api/graphs/{name}/stream`
> and the Platform-compatible `POST /threads/{id}/runs/stream` and `POST /runs/stream`)
> return **buffered SSE**. Chunks emitted by the graph are collected during execution
> and flushed as SSE events **after the run completes** — this is **not** true
> token-level streaming, and clients will not receive partial tokens incrementally.
>
> True chunked streaming is on the roadmap and depends on Azure Functions Python v2
> streaming response support. If you need real-time token streaming today, run the
> graph behind a long-running host (e.g. App Service or AKS) instead.

### Per-graph auth

Override app-level auth settings per graph:

```python
# Per-graph authentication override
app.register(graph=public_graph, name="public", auth_level=func.AuthLevel.ANONYMOUS)
app.register(graph=private_graph, name="private", auth_level=func.AuthLevel.FUNCTION)
```

Example request using a function key:

```bash
curl -X POST "https://<app>.azurewebsites.net/api/graphs/echo_agent/invoke?code=<FUNCTION_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Hello!"}]}}'
```

### What you get

1. `POST /api/graphs/echo_agent/invoke` — invoke the agent
2. `POST /api/graphs/echo_agent/stream` — stream agent responses (buffered SSE, not true token streaming)
3. `GET /api/graphs/echo_agent/threads/{thread_id}/state` — inspect thread state
4. `GET /api/health` — health check

With `platform_compat=True`, you also get SDK-compatible endpoints:

6. `POST /assistants/search` — list registered assistants
7. `GET /assistants/{id}` — get assistant details
8. `POST /assistants/count` — count assistants
9. `POST /threads` — create thread
10. `GET /threads/{id}` — get thread
11. `PATCH /threads/{id}` — update thread metadata
12. `DELETE /threads/{id}` — delete thread
13. `POST /threads/search` — search threads
14. `POST /threads/count` — count threads
15. `POST /threads/{id}/runs/wait` — run and wait for result
16. `POST /threads/{id}/runs/stream` — run and stream result (buffered SSE)
17. `POST /runs/wait` — threadless run
18. `POST /runs/stream` — threadless stream (buffered SSE)
19. `GET /threads/{id}/state` — get thread state
20. `POST /threads/{id}/state` — update thread state
21. `POST /threads/{id}/history` — get state history

### Request format

```json
{
    "input": {
        "messages": [{"role": "human", "content": "Hello!"}]
    },
    "config": {
        "configurable": {"thread_id": "conversation-1"}
    }
}
```

### Persistent storage (v0.4+)

Use Azure Blob Storage for checkpoint persistence and Azure Table Storage for thread metadata:

```python
import azure.functions as func

from azure.storage.blob import ContainerClient
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore


class AgentState(TypedDict):
    messages: list[dict[str, str]]


def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


# Build graph with Azure Blob checkpointer
container_client = ContainerClient.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", "checkpoints"
)
saver = AzureBlobCheckpointSaver(container_client=container_client)

builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile(checkpointer=saver)

# Deploy with Azure Table thread store
thread_store = AzureTableThreadStore.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", table_name="threads"
)

# Production: always set auth_level explicitly
app = LangGraphApp(platform_compat=True, auth_level=func.AuthLevel.FUNCTION)
app.thread_store = thread_store
app.register(graph=graph, name="echo_agent")
func_app = app.function_app
```

Checkpoints and thread metadata survive Azure Functions restarts and scale across instances.

#### Persistent storage with Managed Identity

The recommended production wiring uses **Managed Identity** instead of connection strings, so no secrets land in App Settings. Install the `azure-identity` extra and pass `DefaultAzureCredential` to both clients:

```bash
pip install azure-functions-langgraph[azure-blob,azure-table,azure-identity]
```

```python
from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

credential = DefaultAzureCredential()

container_client = ContainerClient(
    account_url="https://<account>.blob.core.windows.net",
    container_name="langgraph-checkpoints",
    credential=credential,
)
table_client = TableClient(
    endpoint="https://<account>.table.core.windows.net",
    table_name="langgraphthreads",
    credential=credential,
)

checkpointer = AzureBlobCheckpointSaver(container_client=container_client)
thread_store = AzureTableThreadStore.from_table_client(table_client=table_client)
```

Required role assignments on the storage account (or narrower scopes):

| Role | Used by |
| --- | --- |
| `Storage Blob Data Contributor` | `AzureBlobCheckpointSaver` |
| `Storage Table Data Contributor` | `AzureTableThreadStore` |

`DefaultAzureCredential` walks a chain of credentials. In Azure Functions it picks up the Function App's Managed Identity; locally it falls back to `AzureCliCredential` (`az login`) — the same code path works in both environments without conditional wiring.

For a complete runnable example (Managed Identity in prod, Azurite + connection string locally), see [`examples/managed_identity_storage/`](examples/managed_identity_storage/).

### Scale envelope

The bundled persistent backends are intended for development and small-to-medium production deployments. Plan ahead before pushing past these limits:

| Backend | Comfortable | Caution zone | Switch backends |
|---|---|---|---|
| `AzureBlobCheckpointSaver` | < 100 checkpoints/thread, < 10K threads | 100–1000 checkpoints/thread | Use Cosmos DB or Redis-backed checkpointer |
| `AzureTableThreadStore` | < 100K threads, light search load | 100K–500K threads | Use a sharded thread store or Cosmos DB |

Notes:

- **Single partition** — `AzureTableThreadStore` keys every thread under a single PartitionKey, capped by Azure Table per-partition throughput (~2000 entities/sec on Standard accounts). Search and count beyond `status` filtering are **client-side**; see [COMPATIBILITY.md](COMPATIBILITY.md).
- **Prefix scans** — `AzureBlobCheckpointSaver` lists checkpoints via blob prefix scans; transaction count and latency grow with checkpoints-per-thread. Use the retention helpers below to keep that bounded.
- **Entity size** — Azure Table entities are capped at 1 MB; the store logs a warning at 90% of the threshold.

#### Retention helpers

`AzureBlobCheckpointSaver` exposes two helpers for scheduled cleanup (e.g. from a Timer-triggered Function):

```python
# Keep only the most recent 50 checkpoints per (thread, namespace)
saver.delete_old_checkpoints(thread_id="conversation-1", keep_last=50)

# Or delete everything older than a known checkpoint id
saver.delete_checkpoints_before(
    thread_id="conversation-1",
    before_checkpoint_id="01HXY...",
)
```

Both helpers only delete checkpoint marker, metadata, and write blobs. They intentionally preserve channel value blobs (under `values/`) and the `latest.json` pointer so retained checkpoints remain fully usable.

> **Note** — These helpers are safe but **not exhaustive**. Channel value blobs that were referenced *only* by the now-deleted checkpoints become orphaned and are not removed. For long-running threads with frequent checkpointing, those orphans can dominate the storage footprint over time. Full value-blob garbage collection is tracked in [#153](https://github.com/yeongseon/azure-functions-langgraph-python/issues/153) as a candidate opt-in helper.

#### DB checkpointer backends

For workloads that already run a managed database (or need state shared across multiple Function instances), thin DX helpers wrap the official LangGraph DB checkpoint packages without reimplementing storage:

| Backend | Helper | Extra | When to use |
| --- | --- | --- | --- |
| Postgres | `create_postgres_checkpointer` | `pip install azure-functions-langgraph[postgres]` | Production, multi-instance, existing Postgres infra |
| SQLite | `create_sqlite_checkpointer` | `pip install azure-functions-langgraph[sqlite]` | Local dev and single-instance deployments |
| Cosmos DB | `create_cosmos_checkpointer` | `pip install azure-functions-langgraph[cosmos]` | Azure-native serverless/global production (Python 3.11+) |

Each helper owns the connection lifetime and emits clear ImportErrors pointing at the right extra. The Postgres and SQLite helpers accept a connection string and (by default) call upstream `setup()` on cold start so the checkpoint tables exist; the Cosmos DB helper accepts an endpoint and credential and enters the upstream context manager at cold start:

```python
import os
from azure_functions_langgraph.checkpointers.postgres import create_postgres_checkpointer

checkpointer = create_postgres_checkpointer(
    os.environ["LANGGRAPH_POSTGRES_CONNECTION_STRING"],
    setup=True,  # set False once your deployment pipeline owns migrations
)
graph = builder.compile(checkpointer=checkpointer)
```

The helpers do not hide `builder.compile(checkpointer=...)` and do not reimplement DB checkpoint storage — they centralize connection conventions and emit clear ImportErrors pointing at the right extra. The Postgres and SQLite helpers run `setup()` once at cold start; the Cosmos DB helper enters the upstream context manager instead (no `setup()` call). See [`examples/postgres_checkpoint_production/`](examples/postgres_checkpoint_production/), [`examples/sqlite_checkpoint_local/`](examples/sqlite_checkpoint_local/), and [`examples/cosmos_checkpoint_azure/`](examples/cosmos_checkpoint_azure/) for full Azure-Functions wiring.

| Backend | Comfortable | Caution zone | Switch backends |
| --- | --- | --- | --- |
| `create_sqlite_checkpointer` | local dev, single-instance prod | multi-process write contention | Use Postgres |
| `create_postgres_checkpointer` | multi-instance Functions, existing Postgres infra | very high write QPS without read replicas | Add connection pooling / read replicas, or shard |
| `create_cosmos_checkpointer` | Azure-native serverless, global distribution | high RU cost with large checkpoints | Tune RU allocation, use provisioned throughput |

### Upgrading

#### v0.3.0 → v0.4.0

Fully backward-compatible. No breaking changes.

- **New optional extras**: `pip install azure-functions-langgraph[azure-blob,azure-table]` for persistent storage
- **New platform endpoints**: thread CRUD, state update/history, threadless runs, assistants count
- **New protocols**: `UpdatableStateGraph`, `StateHistoryGraph` (available from `azure_functions_langgraph.protocols`)

#### v0.4.0 → v0.5.0

Fully backward-compatible. No breaking changes.

- **Metadata API**: `app.get_app_metadata()` returns an immutable snapshot of all registered routes and graph info
- **OpenAPI bridge**: `azure_functions_langgraph.openapi.register_with_openapi` integrates with `azure-functions-openapi-python`
- **CloneableGraph protocol**: thread-isolated graph cloning for safe concurrent execution

## When to use

- You have LangGraph agents and want to deploy them on Azure Functions
- You want serverless deployment without LangGraph Platform costs
- You need HTTP endpoints for your compiled graphs with minimal setup
- You want thread-based conversation state via LangGraph checkpointers
- You need durable state persistence with Azure Blob/Table Storage

## Documentation

- Project docs live under `docs/`
- Smoke-tested examples live under `examples/`
- Product requirements: `PRD.md`
- Design principles: `DESIGN.md`

## Ecosystem

This package is part of the **Azure Functions Python DX Toolkit**.

**Design principle:** `azure-functions-langgraph` owns LangGraph runtime exposure. `azure-functions-validation-python` owns validation. `azure-functions-openapi-python` owns API documentation.

| Package | Role |
|---------|------|
| [azure-functions-openapi-python](https://github.com/yeongseon/azure-functions-openapi-python) | OpenAPI spec generation and Swagger UI |
| [azure-functions-validation-python](https://github.com/yeongseon/azure-functions-validation-python) | Request/response validation and serialization |
| [azure-functions-db-python](https://github.com/yeongseon/azure-functions-db-python) | Database bindings for SQL, PostgreSQL, MySQL, SQLite, and Cosmos DB |
| **azure-functions-langgraph-python** | LangGraph deployment adapter for Azure Functions |
| [azure-functions-scaffold-python](https://github.com/yeongseon/azure-functions-scaffold-python) | Project scaffolding CLI |
| [azure-functions-logging-python](https://github.com/yeongseon/azure-functions-logging-python) | Structured logging and observability |
| [azure-functions-doctor-python](https://github.com/yeongseon/azure-functions-doctor-python) | Pre-deploy diagnostic CLI |
| [azure-functions-durable-graph-python](https://github.com/yeongseon/azure-functions-durable-graph-python) | Manifest-first graph runtime with Durable Functions *(experimental)* |
| [azure-functions-knowledge-python](https://github.com/yeongseon/azure-functions-knowledge-python) | Knowledge retrieval (RAG) decorators |
| [azure-functions-cookbook-python](https://github.com/yeongseon/azure-functions-cookbook-python) | Dogfood examples — runnable recipes that exercise the full toolkit |

## For AI Coding Assistants

If you are an AI coding assistant (Copilot, Cursor, Claude, etc.), see:

- [`llms.txt`](llms.txt) — Concise package summary and API overview
- [`llms-full.txt`](llms-full.txt) — Complete API reference with signatures, patterns, and examples

## Disclaimer

This project is an independent community project and is not affiliated with,
endorsed by, or maintained by Microsoft or LangChain.

Azure and Azure Functions are trademarks of Microsoft Corporation.
LangGraph and LangChain are trademarks of LangChain, Inc.

## License

MIT
