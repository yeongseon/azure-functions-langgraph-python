# Azure Functions LangGraph

[![PyPI](https://img.shields.io/pypi/v/azure-functions-langgraph.svg)](https://pypi.org/project/azure-functions-langgraph/)
[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/azure-functions-langgraph/)
[![CI](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/LICENSE)

Deploy [LangGraph](https://github.com/langchain-ai/langgraph) agents as **Azure Functions** HTTP endpoints with zero boilerplate.

> **Rolling 0.x, per surface** — the package is **Beta**. `LangGraphApp`, the
> native invoke/stream/state endpoints and auth levels are Stable; checkpoint
> backends, thread locks, the async runtime and `RunObserver` are Beta; Platform
> compatibility, true HTTP streaming, Service Bus and Durable runs are
> Experimental. See the
> [stability table](https://github.com/yeongseon/azure-functions-langgraph-python#stability-rolling-0x-per-surface)
> in the README for the full contract.

## What it does

- **Zero-boilerplate deployment** — register a compiled graph, get HTTP endpoints automatically
- **Invoke endpoint** — `POST /api/graphs/{name}/invoke` for synchronous execution
- **Stream endpoint** — `POST /api/graphs/{name}/stream` for buffered SSE responses (not incremental tokens; true streaming is the experimental `StreamingLangGraphApp`)
- **Health endpoints** — anonymous `GET /api/health` liveness probe (`{"status": "ok"}`) plus protected `GET /api/health/details` listing registered graphs with checkpointer status
- **Secure by default** — routes use the Azure Functions `FUNCTION` auth level (a function key); the `/api` prefix must match `routePrefix` in `host.json`
- **Protocol-based** — works with any object that has `invoke()` and `stream()` methods
- **Checkpointer pass-through** — thread-based conversation state via LangGraph's native config

## Quick example

```python
from langgraph.graph import END, START, StateGraph
from typing import TypedDict

from azure_functions_langgraph import LangGraphApp


class AgentState(TypedDict):
    messages: list[dict[str, str]]


def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile()

app = LangGraphApp()
app.register(graph=graph, name="echo_agent")
func_app = app.function_app
```

This gives you:

1. `POST /api/graphs/echo_agent/invoke` — invoke the agent
2. `POST /api/graphs/echo_agent/stream` — stream agent responses (buffered SSE)
3. `GET /api/health` — anonymous liveness probe (`GET /api/health/details` for the protected graph inventory)

Python 3.11–3.14 is supported. Optional features (`azure-blob`, `azure-table`,
`azure-identity`, `postgres`, `sqlite`, `cosmos`, `otel`, `streaming`, `durable`,
`openapi`) are installed as extras, for example `pip install "azure-functions-langgraph[azure-blob]"`; the
[adapter reference](adapter-reference.md) and [production guide](production-guide.md) name the extra each feature needs.

## Start here

Follow the pages in this order. You do not need anything under *Maintainers* to
use the package.

1. **Getting started** — [Installation](installation.md), then the [Quickstart](getting-started.md)
2. **5-minute tutorial** — [local invoke and first Azure deployment](tutorial-5-min.md)
3. **Deploy** — [choose a plan](choose-a-plan.md) and the [deployment guide](deployment.md) when you need more than the tutorial
4. **Production checklist** — the [production guide](production-guide.md) and [security boundaries](security.md)
5. **Reference** — [API](api.md), [configuration](configuration.md), [usage](usage.md), [architecture](architecture.md)
6. **Troubleshooting** — [common issues](troubleshooting.md) and the [FAQ](faq.md)

**Examples** are grouped in the navigation as Beginner, Production and Advanced;
the [`simple_agent` walkthrough](examples/simple_agent.md) is the smallest one.

Maintaining or contributing? See the **Maintainers** section in the navigation
([contributing guidelines](contributing.md), [release process](release_process.md), and so on).

## Ecosystem

Part of the **Azure Functions Python DX Toolkit**:

| Package | Role |
|---------|------|
| [azure-functions-validation-python](https://github.com/yeongseon/azure-functions-validation-python) | Request and response validation |
| [azure-functions-openapi-python](https://github.com/yeongseon/azure-functions-openapi-python) | OpenAPI spec and Swagger UI |
| [azure-functions-logging-python](https://github.com/yeongseon/azure-functions-logging-python) | Structured logging and observability |
| **azure-functions-langgraph** | LangGraph agent deployment |
