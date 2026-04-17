# Azure Functions LangGraph

[![PyPI](https://img.shields.io/pypi/v/azure-functions-langgraph-python.svg)](https://pypi.org/project/azure-functions-langgraph-python/)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/azure-functions-langgraph-python/)
[![CI](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/LICENSE)

Deploy [LangGraph](https://github.com/langchain-ai/langgraph) agents as **Azure Functions** HTTP endpoints with zero boilerplate.

> **Alpha Notice** — This package is in early development (`0.1.0a0`). APIs may change without notice between releases.

## What it does

- **Zero-boilerplate deployment** — register a compiled graph, get HTTP endpoints automatically
- **Invoke endpoint** — `POST /api/graphs/{name}/invoke` for synchronous execution
- **Stream endpoint** — `POST /api/graphs/{name}/stream` for buffered SSE responses
- **Health endpoint** — `GET /api/health` listing registered graphs with checkpointer status
- **Protocol-based** — works with any object that has `invoke()` and `stream()` methods
- **Checkpointer pass-through** — thread-based conversation state via LangGraph's native config

## Quick example

```python
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

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
3. `GET /api/health` — health check

## Next steps

- [Installation](installation.md) — install the package
- [Quickstart](getting-started.md) — build your first agent endpoint
- [Usage Guide](usage.md) — detailed endpoint reference
- [API Reference](api.md) — full API documentation

## Ecosystem

Part of the **Azure Functions Python DX Toolkit**:

| Package | Role |
|---------|------|
| [azure-functions-validation-python](https://github.com/yeongseon/azure-functions-validation-python) | Request and response validation |
| [azure-functions-openapi-python](https://github.com/yeongseon/azure-functions-openapi-python) | OpenAPI spec and Swagger UI |
| [azure-functions-logging-python](https://github.com/yeongseon/azure-functions-logging-python) | Structured logging and observability |
| **azure-functions-langgraph-python** | LangGraph agent deployment |
| [azure-functions-durable-graph-python](https://github.com/yeongseon/azure-functions-durable-graph-python) | Manifest-first graph runtime with Durable Functions |
