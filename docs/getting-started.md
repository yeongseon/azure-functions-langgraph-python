# Quickstart

This guide walks you through deploying a LangGraph agent as Azure Functions HTTP endpoints in under 5 minutes.

## Prerequisites

- Python 3.11+
- Azure Functions Core Tools installed
- `azure-functions-langgraph` installed (see [Installation](installation.md))

## Start from a new Functions project

This section creates the project that the later steps fill in; skip it if you already have a Functions app.

```bash
mkdir my-langgraph-app
cd my-langgraph-app
python -m venv .venv
source .venv/bin/activate
func init . --worker-runtime python --model V2
```

In Core Tools 4.8.0, `func init` writes:

- `function_app.py`
- `host.json`
- `local.settings.json`
- `requirements.txt`
- `.gitignore`
- `.vscode/extensions.json`

Keep `host.json` and `local.settings.json`. Replace `function_app.py`, because Step 5 below produces its full contents.

Exactly one top-level function app may exist in `function_app.py`. If you keep the scaffolded `app = func.FunctionApp()` next to the library's function app, the host logs `Worker failed to index functions` and `ValueError: More than one FunctionApp or other top level function app instances are defined.`, and serves nothing. Delete the scaffolded `app = func.FunctionApp()` and export the library's app as `app`.

In `requirements.txt`, `azure-functions` is already listed. Add the remaining lines from [Installation](installation.md#add-to-your-requirements), then install them:

```bash
pip install -r requirements.txt
```

The scaffold's `local.settings.json` sets `AzureWebJobsStorage` to `UseDevelopmentStorage=true`. This setting is for the Functions host's own internal storage, not LangGraph checkpoint persistence, and it expects a storage emulator. Without an emulator running, HTTP-only local runs still keep answering requests. With Core Tools 4.8.0 the host logs `Process reporting unhealthy` for the `azure.functions.webjobs.storage` entry, first with `"A timeout occurred while running check."` and later with `"Unable to access AzureWebJobsStorage"`, while the web host and script host lifecycle entries stay Healthy. The [5-minute tutorial](tutorial-5-min.md) example leaves `AzureWebJobsStorage` empty instead.

Follow Steps 1 to 5 below to build the graph and export `app`, then run `func start` and test the endpoints in Steps 6 and 7.

## Step 1: Define your graph state

```python
from typing import TypedDict


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

In your `function_app.py` (replacing the scaffolded file if you started from a new project):

```python
from azure_functions_langgraph import LangGraphApp
from langgraph.graph import END, START, StateGraph
from typing import TypedDict


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

For troubleshooting invocation rejections (such as missing the `input` wrapper or passing client configuration) versus graph failures, see the [no-credentials troubleshooting walkthrough](troubleshooting.md#no-credentials-native-invocation-troubleshooting).

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
{"status": "ok"}
```

The anonymous liveness endpoint intentionally returns no graph inventory. Use
`GET /api/health/details` with the app's function key to inspect registered
graphs and checkpointer status.

## Next steps

- [5-Minute Tutorial](tutorial-5-min.md) — the default path from local invoke to Azure
- [Configuration](configuration.md) — configure auth levels and graph options
- [Usage Guide](usage.md) — detailed endpoint reference with all request/response formats
- [Examples](examples/simple_agent.md) — more complete examples
