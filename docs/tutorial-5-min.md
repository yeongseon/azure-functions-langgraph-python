# Deploy a LangGraph agent to Azure Functions in 5 minutes

This tutorial takes you from a compiled LangGraph graph to a running set of HTTP
endpoints — first **locally**, then **on Azure**. Every command below is
copy-pasteable and the request/response contracts are smoke-tested in CI (see
[How this tutorial stays honest](#how-this-tutorial-stays-honest)).

It uses the shipped [`examples/simple_agent`](../examples/simple_agent/) app — a
minimal two-node graph (`greet` → `farewell`) — so there is nothing to hand-copy.

> **Experimental / Alpha.** APIs may change between minor versions before v1.0.
> This tutorial tracks the current `main`.

## What you'll build

A LangGraph graph exposed as Azure Functions HTTP endpoints:

- `POST /api/graphs/simple_agent/invoke` — run the graph synchronously
- `POST /api/graphs/simple_agent/stream` — buffered SSE response
- `GET  /api/health` — anonymous liveness probe (`{"status": "ok"}`)

## Prerequisites

- **Python 3.10+**
- **[Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4** (`func`)
- For the deploy step only: an **Azure subscription** and the **[Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)** (`az`)

Verify the tooling is present:

```bash
python --version   # 3.10 or newer
func --version     # 4.x
```

## Step 1 — Get the example (30s)

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
cd azure-functions-langgraph-python/examples/simple_agent
```

The app is three small files:

- `graph.py` — defines and compiles the `StateGraph` (nodes `greet` → `farewell`)
- `function_app.py` — registers the compiled graph with `LangGraphApp` and exposes `app`
- `requirements.txt` — `azure-functions`, `azure-functions-langgraph`, `langgraph`, `langchain-core`

The registration is the entire integration — no per-endpoint wiring:

```python
# function_app.py
from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

langgraph_app = LangGraphApp()
langgraph_app.register(
    graph=compiled_graph,
    name="simple_agent",
    description="A simple two-node greeting agent",
)

app = langgraph_app.function_app
```

## Step 2 — Install dependencies (1–2 min)

```bash
pip install -r requirements.txt
```

## Step 3 — Run locally (30s)

```bash
func start
```

You should see the routes registered, including
`graphs/simple_agent/invoke`, `graphs/simple_agent/stream`, and `health`.

## Step 4 — Call it (30s)

In a second terminal:

```bash
# Liveness probe (anonymous)
curl -s http://localhost:7071/api/health
```

```json
{"status": "ok"}
```

```bash
# Invoke the agent
curl -s -X POST http://localhost:7071/api/graphs/simple_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""}}'
```

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

```bash
# Stream the agent (buffered SSE — frames flush after the run completes)
curl -sN -X POST http://localhost:7071/api/graphs/simple_agent/stream \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""}}'
```

> The bundled [`examples/local_curl/`](../examples/local_curl/) scripts wrap these
> same calls (`./health.sh`, `./invoke.sh`, `./stream.sh`) and also work against a
> deployed app via `BASE=` and `FUNCTION_KEY=`.

That's the local loop — **compile a graph → expose as HTTP → run → call** — in
under 5 minutes.

## Step 5 — Deploy to Azure (2 min)

Create the Function App infrastructure and publish. Pick globally-unique names:

```bash
# 1. Sign in
az login

# 2. Set names (RESOURCE_GROUP + STORAGE + APP must be unique)
RESOURCE_GROUP="rg-langgraph-demo"
LOCATION="koreacentral"
STORAGE="stlanggraph$RANDOM"
APP="func-langgraph-$RANDOM"

# 3. Create the resource group, storage account, and a Python 3.11 Function App
az group create --name "$RESOURCE_GROUP" --location "$LOCATION"

az storage account create --name "$STORAGE" --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" --sku Standard_LRS

az functionapp create --name "$APP" --resource-group "$RESOURCE_GROUP" \
  --storage-account "$STORAGE" --consumption-plan-location "$LOCATION" \
  --runtime python --runtime-version 3.11 --functions-version 4 --os-type Linux

# 4. Publish this example (run from examples/simple_agent)
func azure functionapp publish "$APP"
```

When publishing finishes, `func` prints the deployed function URLs.

### Call the deployed app

`LangGraphApp` defaults to `AuthLevel.FUNCTION`, so the invoke/stream endpoints
require a function key (`?code=<FUNCTION_KEY>` or the `x-functions-key` header).
The liveness probe stays anonymous.

```bash
# Liveness (anonymous)
curl -s "https://$APP.azurewebsites.net/api/health"
```

```json
{"status": "ok"}
```

```bash
# Fetch a function key, then invoke
FUNCTION_KEY=$(az functionapp keys list --name "$APP" \
  --resource-group "$RESOURCE_GROUP" --query "functionKeys.default" -o tsv)

curl -s -X POST \
  "https://$APP.azurewebsites.net/api/graphs/simple_agent/invoke?code=$FUNCTION_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""}}'
```

The response matches the local one — same request, same shape.

### Clean up

```bash
az group delete --name "$RESOURCE_GROUP" --yes --no-wait
```

## How this tutorial stays honest

The invoke request/response contract and the health-probe shape shown above are
asserted against the **real `examples/simple_agent` graph** in CI by
[`tests/test_tutorial_smoke.py`](../tests/test_tutorial_smoke.py), which drives
the documented payload through the same native HTTP handler the deployed app
uses. If the example or the endpoint contract changes, that test fails — so the
commands here cannot silently drift out of date.

For a full local host boot (Core Tools + curl), run
[`tools/smoke_tutorial.sh`](../tools/smoke_tutorial.sh).

## Next steps

- [Configuration](configuration.md) — auth levels, per-graph overrides, route prefix
- [Usage Guide](usage.md) — full endpoint reference and request/response formats
- [Deployment](deployment.md) — deeper Azure deployment guidance
- [Production Guide](production-guide.md) — persistence, locking, and scale-out
