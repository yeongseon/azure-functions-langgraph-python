# Azure Functions LangGraph

[![PyPI](https://img.shields.io/pypi/v/azure-functions-langgraph.svg)](https://pypi.org/project/azure-functions-langgraph/)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/azure-functions-langgraph/)
[![CI](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/ci-test.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/ci-test.yml)
[![Release](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/publish-pypi.yml)
[![Security Scans](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/security.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/security.yml)
[![codecov](https://codecov.io/gh/yeongseon/azure-functions-langgraph/branch/main/graph/badge.svg)](https://codecov.io/gh/yeongseon/azure-functions-langgraph)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://pre-commit.com/)
[![Docs](https://img.shields.io/badge/docs-gh--pages-blue)](https://yeongseon.github.io/azure-functions-langgraph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Read this in: [한국어](README.ko.md) | [日本語](README.ja.md) | [简体中文](README.zh-CN.md)

> **Beta Notice** — This package is under active development (`0.4.0`). Core APIs are stabilizing but may still change between minor releases. Please report issues on GitHub.

Deploy [LangGraph](https://github.com/langchain-ai/langgraph) agents as **Azure Functions** HTTP endpoints with zero boilerplate.

---

Part of the **Azure Functions Python DX Toolkit**
→ Bring FastAPI-like developer experience to Azure Functions

## Why this exists

Deploying LangGraph agents to Azure is harder than it should be:

- **No Azure-native deployment** — LangGraph Platform is hosted by LangChain, not Azure
- **Manual HTTP wiring** — bridging `graph.invoke()` / `graph.stream()` to Azure Functions requires repetitive boilerplate
- **No standard pattern** — each team builds its own HTTP wrapper around compiled graphs

## What it does

- **Zero-boilerplate deployment** — register a compiled graph, get HTTP endpoints automatically
- **Invoke endpoint** — `POST /api/graphs/{name}/invoke` for synchronous execution
- **Stream endpoint** — `POST /api/graphs/{name}/stream` for buffered SSE responses
- **Health endpoint** — `GET /api/health` listing registered graphs with checkpointer status
- **Protocol-based** — works with any object that has `invoke()` and `stream()` methods, not just LangGraph
- **Checkpointer pass-through** — thread-based conversation state works via LangGraph's native config
- **State endpoint** — `GET /api/graphs/{name}/threads/{thread_id}/state` for thread state inspection (StatefulGraph only)
- **Per-graph auth** — Override app-level auth per graph with `register(graph, name, auth_level=...)`
- **OpenAPI endpoint** — `GET /api/openapi.json` auto-generated API specification
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

## Scope

- Azure Functions Python **v2 programming model**
- Any graph satisfying the `LangGraphLike` protocol (invoke + stream)
- Pydantic v2-based request/response contracts

This package is a **deployment adapter** — it wraps LangGraph, it does not replace it.

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

Your Azure Functions app should also include:

```text
azure-functions
langgraph
azure-functions-langgraph
```

For local development:

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph.git
cd azure-functions-langgraph
pip install -e .[dev]
```

## Quick Start

```python
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

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

# 4. Deploy
app = LangGraphApp()
app.register(graph=graph, name="echo_agent")
func_app = app.function_app  # ← use this as your Azure Functions app
```

### Production authentication

`LangGraphApp` defaults to `AuthLevel.ANONYMOUS` for local development convenience.
For production deployments, prefer `FUNCTION` or `ADMIN` auth and send the Azure Functions key.

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
```

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
2. `POST /api/graphs/echo_agent/stream` — stream agent responses (buffered SSE)
3. `GET /api/graphs/echo_agent/threads/{thread_id}/state` — inspect thread state
4. `GET /api/health` — health check
5. `GET /api/openapi.json` — OpenAPI specification

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
16. `POST /threads/{id}/runs/stream` — run and stream result
17. `POST /runs/wait` — threadless run
18. `POST /runs/stream` — threadless stream
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
thread_store = AzureTableThreadStore.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", table_name="threads"
)

app = LangGraphApp(platform_compat=True)
app.thread_store = thread_store
app.register(graph=graph, name="echo_agent")
func_app = app.function_app
```

Checkpoints and thread metadata survive Azure Functions restarts and scale across instances.

### Upgrading from v0.3.0

v0.4.0 is fully backward-compatible with v0.3.0. No breaking changes.

- **New optional extras**: `pip install azure-functions-langgraph[azure-blob,azure-table]` for persistent storage
- **New platform endpoints**: thread CRUD, state update/history, threadless runs, assistants count
- **New protocols**: `UpdatableStateGraph`, `StateHistoryGraph` (available from `azure_functions_langgraph.protocols`)
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

Part of the **Azure Functions Python DX Toolkit**:

| Package | Role |
|---------|------|
| [azure-functions-validation](https://github.com/yeongseon/azure-functions-validation) | Request and response validation |
| [azure-functions-openapi](https://github.com/yeongseon/azure-functions-openapi) | OpenAPI spec and Swagger UI |
| [azure-functions-logging](https://github.com/yeongseon/azure-functions-logging) | Structured logging and observability |
| [azure-functions-doctor](https://github.com/yeongseon/azure-functions-doctor) | Pre-deploy diagnostic CLI |
| [azure-functions-scaffold](https://github.com/yeongseon/azure-functions-scaffold) | Project scaffolding |
| **azure-functions-langgraph** | LangGraph agent deployment |
| [azure-functions-durable-graph](https://github.com/yeongseon/azure-functions-durable-graph) | Manifest-first graph runtime with Durable Functions |
| [azure-functions-python-cookbook](https://github.com/yeongseon/azure-functions-python-cookbook) | Recipes and examples |

## Disclaimer

This project is an independent community project and is not affiliated with,
endorsed by, or maintained by Microsoft or LangChain.

Azure and Azure Functions are trademarks of Microsoft Corporation.
LangGraph and LangChain are trademarks of LangChain, Inc.

## License

MIT
