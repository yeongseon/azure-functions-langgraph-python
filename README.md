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

> **Alpha Notice** — This package is in early development (`0.1.0a0`). APIs may change without notice between releases. Do not use in production without thorough testing.

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

## LangGraph Platform comparison

| Feature | LangGraph Platform | azure-functions-langgraph |
|---------|-------------------|--------------------------|
| Hosting | LangChain Cloud (paid) | Your Azure subscription |
| Invoke | `POST /runs/stream` | `POST /api/graphs/{name}/invoke` |
| Streaming | True SSE | Buffered SSE (v0.1) |
| Threads | Built-in | Via LangGraph checkpointer |
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

### What you get

1. `POST /api/graphs/echo_agent/invoke` — invoke the agent
2. `POST /api/graphs/echo_agent/stream` — stream agent responses (buffered SSE)
3. `GET /api/health` — health check

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

## When to use

- You have LangGraph agents and want to deploy them on Azure Functions
- You want serverless deployment without LangGraph Platform costs
- You need HTTP endpoints for your compiled graphs with minimal setup
- You want thread-based conversation state via LangGraph checkpointers

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
