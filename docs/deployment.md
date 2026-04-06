# Deploy to Azure

This guide walks you through deploying the `simple_agent` example to Azure Functions, **step by step**.
No Azure experience required — every command is explained and copy-paste ready.

## Who this guide is for

You know Python and pip. You have cloned this repo and run the `simple_agent` example locally.
Now you want to deploy it to Azure so it runs in the cloud. This guide assumes you have **never used Azure before**.

## What you are deploying

`azure-functions-langgraph` turns [LangGraph](https://langchain-ai.github.io/langgraph/) agents into Azure Functions HTTP endpoints.
After deployment, your agent exposes:

- **Native routes** — `/api/health`, `/api/graphs/{name}/invoke`, `/api/graphs/{name}/stream`
- **Platform-compatible routes** (opt-in) — `/api/threads`, `/api/runs/wait`, `/api/runs/stream`, and the full [LangGraph Platform API](https://langchain-ai.github.io/langgraph/cloud/reference/api/api_ref.html) surface

The `simple_agent` example is a two-node greeting graph (`greet` → `farewell`) that does **not** call any LLM.
If your own graph uses OpenAI or another provider, you will set those API keys in [Step 8](#step-8--configure-app-settings).

## Azure concepts you need for this guide

> New to Azure? Read [Choose an Azure Functions Hosting Plan](choose-a-plan.md) for a visual decision tree, plan comparison, and cost guidance.

| Term | What it means |
|---|---|
| **Function App** | Your deployed application. Like a Flask/FastAPI app running in the cloud. |
| **Hosting plan** | Controls how your app scales, how fast it responds, and how much it costs. |
| **Resource Group** | A folder for Azure resources. Delete it to clean up everything at once. |
| **Storage Account** | Required by Azure Functions for internal state. Also used by this package for checkpoints and thread metadata. |

## Recommended plan for this repo

| | |
|---|---|
| **Default plan** | **Premium (EP1)** |
| **Why** | LangGraph agents often call LLM APIs that take several seconds to respond. Premium provides always-warm instances that eliminate cold starts, faster dependency builds during deployment, VNet support for private endpoints, and reliable Server-Sent Events (SSE) streaming — instances stay alive for the full response. |
| **Switch to Flex Consumption if** | Your graph is lightweight (no LLM calls), response times are predictable, and you want to minimize idle cost. The `simple_agent` example works fine on Flex Consumption. |
| **Switch to Dedicated (B1) if** | You need fixed monthly cost with no per-execution billing. |

> ⚠️ **Premium plans have an always-on cost** even when idle. See [Choose an Azure Functions Hosting Plan](choose-a-plan.md) for pricing details. For testing, Flex Consumption is cheaper.

## Before you start

| Requirement | How to check | Install if missing |
|---|---|---|
| Azure account | [portal.azure.com](https://portal.azure.com) | [Create free account](https://azure.microsoft.com/free/) |
| Azure CLI | `az --version` | [Install Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| Azure Functions Core Tools v4 | `func --version` | [Install Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local#install-the-azure-functions-core-tools) |
| Python 3.10–3.13 | `python --version` | [python.org](https://www.python.org/downloads/) |
| `jq` (for JSON parsing in curl examples) | `jq --version` | [stedolan.github.io/jq](https://stedolan.github.io/jq/download/) |
| Local example working | `func start` → `curl /api/health` returns OK | See [examples/simple_agent](../examples/simple_agent/) |

> ⚠️ **Verify locally first.** If your project doesn't work with `func start`, it won't work on Azure.

## Read these warnings before provisioning

1. **Storage account names must be globally unique** across all of Azure. Use a name like `stlanggraph` + a random suffix. Only lowercase letters and numbers, 3–24 characters.
2. **Use one region for all resources.** Mixing regions adds latency and can cause failures.
3. **Local `.env` values don't automatically appear on Azure.** You must set app settings separately via `az functionapp config appsettings set` (see [Step 8](#step-8--configure-app-settings)).
4. **First deploy takes longer than expected.** Azure runs a remote build to install your Python dependencies (including LangGraph, langchain-core, etc.). Wait for the "Deployment successful" message.
5. **Deleting local files does not delete Azure resources.** You must explicitly delete the resource group to stop billing (see [Clean up resources](#clean-up-resources)).
6. **LangGraph dependencies are large (~150+ MB).** Remote build times can be 2–5 minutes on first deploy. Premium plans use express build which is faster.
7. **The `simple_agent` example does NOT need an LLM API key.** It uses a hardcoded greeting graph. Only set `OPENAI_API_KEY` if your own graph actually calls an LLM provider.
8. **Platform-compatible routes require Azure Storage backends.** The threads API needs a blob container (checkpoints) and a table (thread metadata). These are created in [Step 7](#step-7--create-storage-backends-for-langgraph).

---

## Deploy `simple_agent` with platform-compatible routes

This example deploys with `platform_compat=True`, which enables the full threads/runs API alongside native routes. This requires Azure Blob Storage (checkpoints) and Azure Table Storage (thread metadata).

### Step 1 — Copy the example project

```bash
cp -r examples/simple_agent my-langgraph-deploy
cd my-langgraph-deploy
```

### Step 2 — Update `requirements.txt`

Replace the contents of `requirements.txt` with:

```bash
cat > requirements.txt << 'EOF'
azure-functions
azure-functions-langgraph[azure-blob,azure-table]
langgraph>=1.0,<2.0
langchain-core>=1.0,<2.0
EOF
```

> The `[azure-blob,azure-table]` extras install `azure-storage-blob` and `azure-data-tables` for persistent checkpointer and thread store.

### Step 3 — Modify `function_app.py` for Azure

Replace `function_app.py` with the Azure-ready version that connects storage backends:

```bash
cat > function_app.py << 'PYEOF'
"""Simple agent — Azure Functions entry point (Azure deployment)."""

import os

from azure.storage.blob import BlobServiceClient
from graph import builder  # Import the builder, NOT compiled_graph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

# Storage backends
conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
blob_service = BlobServiceClient.from_connection_string(conn_str)
container = blob_service.get_container_client("langgraph-checkpoints")
checkpointer = AzureBlobCheckpointSaver(container_client=container)
thread_store = AzureTableThreadStore.from_connection_string(
    connection_string=conn_str,
    table_name="langgraphthreads",
)

# Compile with checkpointer for persistent state
compiled_graph = builder.compile(checkpointer=checkpointer)

langgraph_app = LangGraphApp(platform_compat=True)
langgraph_app.thread_store = thread_store  # Set via property, NOT register()
langgraph_app.register(
    graph=compiled_graph,
    name="simple_agent",
    description="A simple two-node greeting agent",
)

app = langgraph_app.function_app
PYEOF
```

**Key differences from the local version:**

| Local (`function_app.py`) | Azure (`function_app.py`) |
|---|---|
| `from graph import compiled_graph` | `from graph import builder` — import the builder and compile with a checkpointer |
| `LangGraphApp()` | `LangGraphApp(platform_compat=True)` — enables threads/runs API |
| No storage | Blob checkpointer + Table thread store connected via `AZURE_STORAGE_CONNECTION_STRING` |

### Step 4 — Verify locally

```bash
cp local.settings.json.example local.settings.json 2>/dev/null || true
func start
```

In another terminal:

```bash
curl -s http://localhost:7071/api/health | jq .
```

Expected output:

```json
{
  "status": "ok",
  "graphs": [
    {
      "name": "simple_agent",
      "description": "A simple two-node greeting agent",
      "has_checkpointer": true
    }
  ]
}
```

> ⚠️ Platform routes (`/api/threads`, `/api/runs/...`) won't work locally without Azure Storage. That's expected — they will work after deployment.

Stop the local server with `Ctrl+C`.

### Step 5 — Sign in to Azure

```bash
az login
az account set --subscription "<YOUR_SUBSCRIPTION_ID>"
```

> **How to find your subscription ID**: Run `az account list --output table` and look for the `SubscriptionId` column.

### Step 6 — Create Azure resources

```bash
RESOURCE_GROUP="rg-langgraph-agent"
LOCATION="koreacentral"
STORAGE_ACCOUNT="stlanggraph$(date +%s | tail -c 6)"
FUNCTIONAPP_NAME="func-langgraph-agent"
PLAN_NAME="plan-langgraph-agent"
```

Create the resource group:

```bash
az group create --name "$RESOURCE_GROUP" --location "$LOCATION"
```

Create a storage account:

```bash
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS
```

> This takes about 10 seconds. Look for `"provisioningState": "Succeeded"` in the output.

Create the Premium plan:

```bash
az functionapp plan create \
  --name "$PLAN_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku EP1 \
  --is-linux
```

Create the Function App on that plan:

```bash
az functionapp create \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --storage-account "$STORAGE_ACCOUNT" \
  --plan "$PLAN_NAME" \
  --runtime python \
  --runtime-version 3.11 \
  --os-type Linux
```

Output includes:

```text
Application Insights "func-langgraph-agent" was created for this Function App.
```

### Step 7 — Create storage backends for LangGraph

The platform-compatible routes need a blob container (for checkpoints) and a table (for thread metadata).

Get the storage connection string:

```bash
STORAGE_CONN_STR=$(az storage account show-connection-string \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query connectionString \
  --output tsv)
```

Create the blob container:

```bash
az storage container create \
  --name langgraph-checkpoints \
  --connection-string "$STORAGE_CONN_STR"
```

Expected output:

```json
{"created": true}
```

Create the table:

```bash
az storage table create \
  --name langgraphthreads \
  --connection-string "$STORAGE_CONN_STR"
```

Expected output:

```json
{"created": true}
```

### Step 8 — Configure app settings

Set the storage connection string so `function_app.py` can read it at runtime:

```bash
az functionapp config appsettings set \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings AZURE_STORAGE_CONNECTION_STRING="$STORAGE_CONN_STR"
```

**If your graph uses an LLM provider** (the `simple_agent` example does NOT):

```bash
# OpenAI
az functionapp config appsettings set \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings OPENAI_API_KEY="sk-..."

# Or Azure OpenAI
az functionapp config appsettings set \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings \
    AZURE_OPENAI_API_KEY="..." \
    AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/" \
    AZURE_OPENAI_DEPLOYMENT="gpt-4o"
```

### Step 9 — Deploy the code

```bash
func azure functionapp publish "$FUNCTIONAPP_NAME"
```

Output:

```text
Getting site publishing info...
Starting the function app deployment...
Creating archive for current directory...
Performing remote build for functions project.
...
Deployment completed successfully.
Functions in func-langgraph-agent:
    aflg_health - [httpTrigger]
    aflg_simple_agent_invoke - [httpTrigger]
    aflg_simple_agent_stream - [httpTrigger]
    aflg_simple_agent_state - [httpTrigger]
    aflg_platform_threads_create - [httpTrigger]
    aflg_platform_threads_get - [httpTrigger]
    aflg_platform_threads_update - [httpTrigger]
    aflg_platform_threads_delete - [httpTrigger]
    aflg_platform_threads_search - [httpTrigger]
    aflg_platform_threads_count - [httpTrigger]
    aflg_platform_threads_state_get - [httpTrigger]
    aflg_platform_threads_state_update - [httpTrigger]
    aflg_platform_threads_history - [httpTrigger]
    aflg_platform_runs_wait - [httpTrigger]
    aflg_platform_runs_stream - [httpTrigger]
    aflg_platform_runs_wait_threadless - [httpTrigger]
    aflg_platform_runs_stream_threadless - [httpTrigger]
    aflg_platform_assistants_search - [httpTrigger]
    aflg_platform_assistants_count - [httpTrigger]
    aflg_platform_assistants_get - [httpTrigger]
Deployment successful.
```

> ⚠️ **First deploy takes 2–5 minutes** because of the large dependency tree (LangGraph, langchain-core, etc.). Premium plans use express build which is faster than Consumption.

### Step 10 — Set the base URL

```bash
export BASE_URL="https://$FUNCTIONAPP_NAME.azurewebsites.net"
```

### Step 11 — Verify native routes

#### Health check

```bash
curl -s "$BASE_URL/api/health" | jq .
```

Expected output:

```json
{
  "status": "ok",
  "graphs": [
    {
      "name": "simple_agent",
      "description": "A simple two-node greeting agent",
      "has_checkpointer": true
    }
  ]
}
```

#### Invoke the agent

```bash
curl -s -X POST "$BASE_URL/api/graphs/simple_agent/invoke" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "messages": [{"role": "human", "content": "World"}],
      "greeting": ""
    },
    "config": {"configurable": {"thread_id": "native-001"}}
  }' | jq .
```

Expected output:

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

#### Stream the agent

```bash
curl -s -X POST "$BASE_URL/api/graphs/simple_agent/stream" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "messages": [{"role": "human", "content": "World"}],
      "greeting": ""
    },
    "config": {"configurable": {"thread_id": "native-002"}}
  }'
```

Expected output (Server-Sent Events). The graph has two nodes (`greet` → `farewell`), so three state snapshots are emitted — initial input, after greet, after farewell:

```text
event: data
data: {"messages":[{"role":"human","content":"World"}],"greeting":""}

event: data
data: {"messages":[{"role":"human","content":"World"}],"greeting":"Hello, World!"}

event: data
data: {"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"}

event: end
data: {}
```

### Step 12 — Verify platform-compatible routes

These routes follow the [LangGraph Platform API](https://langchain-ai.github.io/langgraph/cloud/reference/api/api_ref.html) specification.

#### Create a thread

```bash
THREAD_ID=$(curl -s -X POST "$BASE_URL/api/threads" \
  -H "Content-Type: application/json" \
  -d '{}' | jq -r '.thread_id')
echo "Thread ID: $THREAD_ID"
```

Expected output:

```json
{
  "thread_id": "550e8400-e29b-41d4-a716-446655440000",
  "created_at": "2026-04-06T10:30:00Z",
  "updated_at": "2026-04-06T10:30:00Z",
  "metadata": null,
  "status": "idle",
  "values": null
}
```

#### Run the agent on the thread

```bash
curl -s -X POST "$BASE_URL/api/threads/$THREAD_ID/runs/wait" \
  -H "Content-Type: application/json" \
  -d '{
    "assistant_id": "simple_agent",
    "input": {
      "messages": [{"role": "human", "content": "World"}],
      "greeting": ""
    }
  }' | jq .
```

Expected output:

```json
{
  "messages": [
    {"role": "human", "content": "World"},
    {"role": "assistant", "content": "Hello, World! Goodbye!"}
  ],
  "greeting": "Hello, World!"
}
```

#### Stream the agent on the thread

```bash
curl -s -X POST "$BASE_URL/api/threads/$THREAD_ID/runs/stream" \
  -H "Content-Type: application/json" \
  -d '{
    "assistant_id": "simple_agent",
    "input": {
      "messages": [{"role": "human", "content": "World"}],
      "greeting": ""
    }
  }'
```

Expected output:

```text
event: metadata
data: {"run_id": "1f8a6f2d-3304-4556-89ce-37d4cabc1234"}

event: values
data: {"messages":[{"role":"human","content":"World"}],"greeting":""}

event: values
data: {"messages":[{"role":"human","content":"World"}],"greeting":"Hello, World!"}

event: values
data: {"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"}

event: end
data: null
```

#### Get thread state

```bash
curl -s "$BASE_URL/api/threads/$THREAD_ID/state" | jq .
```

Expected output (truncated):

```json
{
  "values": {
    "messages": [
      {"role": "human", "content": "World"},
      {"role": "assistant", "content": "Hello, World! Goodbye!"}
    ],
    "greeting": "Hello, World!"
  },
  "next": [],
  "checkpoint": {
    "thread_id": "550e8400-...",
    "checkpoint_ns": "",
    "checkpoint_id": "1efc4c4f-..."
  }
}
```

#### Search threads

```bash
curl -s -X POST "$BASE_URL/api/threads/search" \
  -H "Content-Type: application/json" \
  -d '{}' | jq .
```

#### Threadless run (no thread required)

```bash
curl -s -X POST "$BASE_URL/api/runs/wait" \
  -H "Content-Type: application/json" \
  -d '{
    "assistant_id": "simple_agent",
    "input": {
      "messages": [{"role": "human", "content": "World"}],
      "greeting": ""
    }
  }' | jq .
```

#### List registered assistants

```bash
curl -s -X POST "$BASE_URL/api/assistants/search" \
  -H "Content-Type: application/json" \
  -d '{}' | jq .
```

Expected output:

```json
[
  {
    "assistant_id": "simple_agent",
    "graph_id": "simple_agent",
    "name": "simple_agent",
    "description": "A simple two-node greeting agent"
  }
]
```

### Step 13 — Watch logs

```bash
func azure functionapp logstream "$FUNCTIONAPP_NAME"
```

You will see entries like:

```text
Executing 'Functions.aflg_health' (Reason='...', Id=ab2a5eb3-...)
Executed 'Functions.aflg_health' (Succeeded, Duration=12ms)
Executing 'Functions.aflg_platform_runs_wait' (Reason='...', Id=4d5267c7-...)
Executed 'Functions.aflg_platform_runs_wait' (Succeeded, Duration=41ms)
```

Press `Ctrl+C` to stop the log stream.

---

## If you need a different plan

The example above uses **Premium (EP1)**. If you want a different plan, only the plan + Function App creation commands change. Everything else stays the same.

See [Choose an Azure Functions Hosting Plan](choose-a-plan.md) for complete per-plan commands.

### Flex Consumption — for lowest cost

Replace [Step 6](#step-6--create-azure-resources) plan and Function App creation with:

```bash
# No separate plan needed — Flex Consumption is serverless
az functionapp create \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --storage-account "$STORAGE_ACCOUNT" \
  --flexconsumption-location "$LOCATION" \
  --runtime python \
  --runtime-version 3.11
```

> ⚠️ **Timeout limit**: Flex Consumption has a 30-minute function timeout (configurable via `functionTimeout` in `host.json`), but cold starts can be slower than Premium. Streaming responses may be less reliable under high latency.

### Dedicated (B1) — for fixed monthly cost

Replace [Step 6](#step-6--create-azure-resources) plan and Function App creation with:

```bash
# Create the App Service plan
az appservice plan create \
  --name "$PLAN_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku B1 \
  --is-linux

# Create the Function App on that plan
az functionapp create \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --storage-account "$STORAGE_ACCOUNT" \
  --plan "$PLAN_NAME" \
  --runtime python \
  --runtime-version 3.11 \
  --os-type Linux
```

> ⚠️ **Slow builds**: B1 Dedicated plans have limited compute. First deployment with LangGraph dependencies can take 5+ minutes and may time out. If deployment fails, retry `func azure functionapp publish`.

---

## Troubleshooting

### Provisioning failed

| Symptom | Usually means | How to fix |
|---|---|---|
| `StorageAccountAlreadyTaken` | Storage account name not globally unique | Add a random suffix: `stlanggraph$(date +%s \| tail -c 6)` |
| `LocationNotAvailableForResourceType` | Region doesn't support your plan | Use `az functionapp list-flexconsumption-locations -o table` or pick a major region (`eastus`, `westeurope`, `koreacentral`) |
| `SubscriptionNotFound` | Wrong subscription selected | Run `az account list -o table` and `az account set --subscription <ID>` |
| `SkuNotAvailable` for EP1 | Premium not available in your region | Try a different region or use Flex Consumption |

### Deployment failed

| Symptom | Usually means | How to fix |
|---|---|---|
| `ModuleNotFoundError` in build logs | Missing or wrong `requirements.txt` | Ensure `azure-functions-langgraph[azure-blob,azure-table]` is in `requirements.txt` |
| Build timeout (>10 minutes) | Large dependencies on slow plan | Use Premium (faster express build) or retry |
| `Can't find app with name` | Function App not fully provisioned | Wait 30 seconds and retry |
| `ImportError: azure.storage.blob` | Missing extras in requirements | Use `azure-functions-langgraph[azure-blob,azure-table]`, not just `azure-functions-langgraph` |

### The app deployed but does not behave correctly

| Symptom | Usually means | How to fix |
|---|---|---|
| `/api/health` returns 404 | Functions not registered | Check `func azure functionapp publish` output — should list `aflg_health` |
| `/api/health` returns 500 | Missing `AZURE_STORAGE_CONNECTION_STRING` | Set it: `az functionapp config appsettings set --settings AZURE_STORAGE_CONNECTION_STRING="..."` |
| `/api/threads` returns 500 | Blob container or table doesn't exist | Run [Step 7](#step-7--create-storage-backends-for-langgraph) commands |
| Invoke returns `KeyError: OPENAI_API_KEY` | Your graph expects an LLM key | Set it via `az functionapp config appsettings set --settings OPENAI_API_KEY="sk-..."` |
| Invoke returns correct data but streaming hangs | SSE connection interrupted | Check timeout settings; Premium is more reliable for streaming |
| Thread state is empty after invoke | Using `compiled_graph` instead of `builder` | Import `builder` from `graph.py` and compile with `checkpointer` (see [Step 3](#step-3--modify-function_apppy-for-azure)) |

### Logs and monitoring

```bash
# Live log stream (real-time)
func azure functionapp logstream "$FUNCTIONAPP_NAME"

# Recent invocations via Application Insights
az monitor app-insights query \
  --app "$FUNCTIONAPP_NAME" \
  --analytics-query "requests | where timestamp > ago(30m) | project timestamp, name, resultCode, duration | order by timestamp desc | take 10"
```

### Before opening an issue

If you're stuck, please include the following when opening a GitHub issue:

```bash
# 1. Azure CLI version
az --version

# 2. Functions Core Tools version
func --version

# 3. Python version
python --version

# 4. Package version
pip show azure-functions-langgraph

# 5. Function App status
az functionapp show \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query "{state:state, runtime:siteConfig.linuxFxVersion}"

# 6. App settings (secrets will be redacted)
az functionapp config appsettings list \
  --name "$FUNCTIONAPP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[].name"

# 7. Recent logs
func azure functionapp logstream "$FUNCTIONAPP_NAME"
```

---

## Clean up resources

> ⚠️ **Premium plans cost money even when idle.** Always clean up after testing.

```bash
az group delete --name "$RESOURCE_GROUP" --yes --no-wait
```

The `--no-wait` flag returns immediately. Deletion happens in the background and takes 1–2 minutes.

To verify deletion:

```bash
az group list --query "[?starts_with(name, 'rg-langgraph')]" -o table
```

---

## Sources

- [Azure Functions Python quickstart](https://learn.microsoft.com/azure/azure-functions/create-first-function-cli-python) — Official getting-started guide
- [Azure Functions Core Tools reference](https://learn.microsoft.com/azure/azure-functions/functions-core-tools-reference) — CLI command reference
- [Azure Functions app settings](https://learn.microsoft.com/azure/azure-functions/functions-how-to-use-azure-function-app-settings) — Environment variables and configuration
- [Azure Functions hosting plans](https://learn.microsoft.com/azure/azure-functions/functions-scale) — Plan comparison and limits
- [Premium plan](https://learn.microsoft.com/azure/azure-functions/functions-premium-plan) — Premium plan details
- [Azure Blob Storage documentation](https://learn.microsoft.com/azure/storage/blobs/) — Blob storage for checkpoints
- [Azure Table Storage documentation](https://learn.microsoft.com/azure/storage/tables/) — Table storage for thread metadata
- [Functions monitoring and telemetry](https://learn.microsoft.com/azure/azure-functions/analyze-telemetry-data) — Application Insights and monitoring

## See Also

- [Choose an Azure Functions Hosting Plan](choose-a-plan.md) — Plan selection guide with decision tree
- [`production-guide.md`](./production-guide.md) — Auth, observability, and production hardening
- [`configuration.md`](./configuration.md) — Configuration reference
- [`architecture.md`](./architecture.md) — Package architecture
- [`troubleshooting.md`](./troubleshooting.md) — Extended troubleshooting guide
- [`azure-functions-scaffold`](https://github.com/yeongseon/azure-functions-scaffold)
- [`azure-functions-validation`](https://github.com/yeongseon/azure-functions-validation)
- [`azure-functions-openapi`](https://github.com/yeongseon/azure-functions-openapi)
- [`azure-functions-logging`](https://github.com/yeongseon/azure-functions-logging)
- [`azure-functions-doctor`](https://github.com/yeongseon/azure-functions-doctor)
