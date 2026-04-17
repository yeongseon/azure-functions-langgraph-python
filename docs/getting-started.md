# Quickstart

This guide walks you through deploying a LangGraph agent as Azure Functions HTTP endpoints in under 5 minutes.

## Prerequisites

- Python 3.10+
- Azure Functions Core Tools installed
- `azure-functions-langgraph-python` installed (see [Installation](installation.md))

## Step 1: Define your graph state

```python
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: list[dict[str, str]]
```

## Step 2: Define node functions

```python
def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {
        "messages": state["messages"] + [
            {"role": "assistant", "content": f"Echo: {user_msg}"}
        ]
    }
```

## Step 3: Build and compile the graph

```python
from langgraph.graph import END, START, StateGraph

builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)

graph = builder.compile()
```

## Step 4: Register with LangGraphApp

```python
from azure_functions_langgraph import LangGraphApp

app = LangGraphApp()
app.register(graph=graph, name="echo_agent")
```

## Step 5: Export the function app

In your `function_app.py`:

```python
from azure_functions_langgraph import LangGraphApp
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: list[dict[str, str]]


def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {
        "messages": state["messages"] + [
            {"role": "assistant", "content": f"Echo: {user_msg}"}
        ]
    }


builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile()

langgraph_app = LangGraphApp()
langgraph_app.register(graph=graph, name="echo_agent")

app = langgraph_app.function_app
```

## Step 6: Run locally

```bash
func start
```

## Step 7: Test the endpoints

**Invoke the agent:**

```bash
curl -X POST http://localhost:7071/api/graphs/echo_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Hello!"}]}}'
```

Response:

```json
{
    "output": {
        "messages": [
            {"role": "human", "content": "Hello!"},
            {"role": "assistant", "content": "Echo: Hello!"}
        ]
    }
}
```

**Stream the agent:**

```bash
curl -X POST http://localhost:7071/api/graphs/echo_agent/stream \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Hello!"}]}, "stream_mode": "values"}'
```

**Check health:**

```bash
curl http://localhost:7071/api/health
```

Response:

```json
{
    "status": "ok",
    "graphs": [
        {"name": "echo_agent", "description": null, "has_checkpointer": false}
    ]
}
```

## Next steps

- [Configuration](configuration.md) — configure auth levels and graph options
- [Usage Guide](usage.md) — detailed endpoint reference with all request/response formats
- [Examples](examples/simple_agent.md) — more complete examples
