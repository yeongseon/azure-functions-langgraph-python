# Deployment Guide

This guide walks through deploying the `simple_agent` example to Azure Functions with full platform-compatible API support enabled. It includes resource provisioning, publish, and endpoint verification for native and platform routes. For auth/observability/timeout tuning, see [`production-guide.md`](./production-guide.md). Outputs are representative examples, not guaranteed byte-for-byte.

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Azure subscription | Active | Use `<YOUR_SUBSCRIPTION_ID>` |
| Azure CLI (`az`) | Current | `az --version` |
| Azure Functions Core Tools (`func`) | v4 | `func --version` |
| Python | 3.10+ | Deploy runtime shown is Python 3.11 |
| Storage Account | StorageV2 | Required for checkpointer + thread store |
| pip packages | Installable | See updated `requirements.txt` |

After applying the code changes below (including the updated `requirements.txt`),
install dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Representative output:

```bash
Requirement already satisfied: pip in ./.venv/lib/python3.11/site-packages (25.0)
Collecting azure-functions
Collecting azure-functions-langgraph[azure-blob,azure-table]
Collecting langgraph
Collecting langchain-core
Successfully installed azure-functions-1.21.0 azure-functions-langgraph-0.5.0 langchain-core-1.0.1 langgraph-1.0.5
```

⚠️ `simple_agent` itself does not use OpenAI. Only set provider keys if your graph actually depends on an LLM API.

## Code changes for Azure deployment

Enable platform compatibility and connect Azure storage backends.

### Modified `function_app.py`

```python
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
```

The example `graph.py` exports both `builder` and `compiled_graph`. The original
`graph.py` compiles the graph without a checkpointer (`builder.compile()`). For Azure
deployment with persistent thread state, import the `builder` and compile with
`checkpointer=checkpointer` in `function_app.py`.

### Updated `requirements.txt`

```text
azure-functions
azure-functions-langgraph[azure-blob,azure-table]
langgraph
langchain-core
```

## Provision Azure resources

```bash
az account set --subscription <YOUR_SUBSCRIPTION_ID>
az group create --name <YOUR_RESOURCE_GROUP> --location eastus
```

Representative output:

```json
{"name":"<YOUR_RESOURCE_GROUP>","location":"eastus","properties":{"provisioningState":"Succeeded"}}
```

```bash
az storage account create   --name <YOUR_STORAGE_ACCOUNT>   --resource-group <YOUR_RESOURCE_GROUP>   --location eastus   --sku Standard_LRS   --kind StorageV2
```

Representative output:

```json
{"name":"<YOUR_STORAGE_ACCOUNT>","kind":"StorageV2","provisioningState":"Succeeded","primaryEndpoints":{"blob":"https://<YOUR_STORAGE_ACCOUNT>.blob.core.windows.net/","table":"https://<YOUR_STORAGE_ACCOUNT>.table.core.windows.net/"}}
```

```bash
az functionapp create   --name <YOUR_FUNCTION_APP_NAME>   --resource-group <YOUR_RESOURCE_GROUP>   --storage-account <YOUR_STORAGE_ACCOUNT>   --consumption-plan-location eastus   --runtime python   --runtime-version 3.11   --functions-version 4
```

Representative output:

```json
{"name":"<YOUR_FUNCTION_APP_NAME>","defaultHostName":"<YOUR_FUNCTION_APP_NAME>.azurewebsites.net","provisioningState":"Succeeded","state":"Running"}
```

```bash
az storage account show-connection-string   --name <YOUR_STORAGE_ACCOUNT>   --resource-group <YOUR_RESOURCE_GROUP>   --query connectionString   --output tsv
```

Representative output:

```text
<YOUR_STORAGE_CONNECTION_STRING>
```

```bash
# Create blob container for checkpoints
az storage container create \
  --name langgraph-checkpoints \
  --account-name <YOUR_STORAGE_ACCOUNT> \
  --connection-string "<YOUR_STORAGE_CONNECTION_STRING>"
```

Representative output:

```json
{"created": true}
```

```bash
# Create table for thread metadata
az storage table create \
  --name langgraphthreads \
  --account-name <YOUR_STORAGE_ACCOUNT> \
  --connection-string "<YOUR_STORAGE_CONNECTION_STRING>"
```

Representative output:

```json
{"created": true}
```

## Configure app settings

```bash
az functionapp config appsettings set   --name <YOUR_FUNCTION_APP_NAME>   --resource-group <YOUR_RESOURCE_GROUP>   --settings AZURE_STORAGE_CONNECTION_STRING="<YOUR_STORAGE_CONNECTION_STRING>"
```

Representative output:

```json
[{"name":"AZURE_STORAGE_CONNECTION_STRING","slotSetting":false,"value":""},{"name":"FUNCTIONS_EXTENSION_VERSION","slotSetting":false,"value":"~4"},{"name":"FUNCTIONS_WORKER_RUNTIME","slotSetting":false,"value":"python"}]
```

Values may appear redacted in recent Azure CLI versions.

If your graph uses an LLM provider:

```bash
az functionapp config appsettings set   --name <YOUR_FUNCTION_APP_NAME>   --resource-group <YOUR_RESOURCE_GROUP>   --settings OPENAI_API_KEY="<YOUR_OPENAI_API_KEY>"
```

Representative output:

```json
[{"name":"OPENAI_API_KEY","slotSetting":false,"value":""}]
```

You can use Azure OpenAI settings instead (`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`).

## Publish

```bash
func azure functionapp publish <YOUR_FUNCTION_APP_NAME>
```

Representative output:

```text
Getting site publishing info...
[2025-01-15T10:27:41.021Z] Starting the function app deployment...
Uploading package...
Uploading 8.45 MB [#############################################################################]
Deployment completed successfully.
Syncing triggers...
Functions in <YOUR_FUNCTION_APP_NAME>:
    aflg_health - [httpTrigger]
        Invoke url: https://<YOUR_FUNCTION_APP_NAME>.azurewebsites.net/api/health
    aflg_openapi - [httpTrigger]
        Invoke url: https://<YOUR_FUNCTION_APP_NAME>.azurewebsites.net/api/openapi.json
    aflg_simple_agent_invoke - [httpTrigger]
        Invoke url: https://<YOUR_FUNCTION_APP_NAME>.azurewebsites.net/api/graphs/simple_agent/invoke
    aflg_simple_agent_stream - [httpTrigger]
        Invoke url: https://<YOUR_FUNCTION_APP_NAME>.azurewebsites.net/api/graphs/simple_agent/stream
    aflg_simple_agent_state - [httpTrigger]
        Invoke url: https://<YOUR_FUNCTION_APP_NAME>.azurewebsites.net/api/graphs/simple_agent/state
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

## Verify native routes

```bash
export BASE_URL="https://<YOUR_FUNCTION_APP_NAME>.azurewebsites.net"
```

### `GET /api/health`

```bash
curl -s "$BASE_URL/api/health"
```

Representative response:

```json
{"status":"ok","graphs":[{"name":"simple_agent","description":"A simple two-node greeting agent","has_checkpointer":true}]}
```

### `POST /api/graphs/simple_agent/invoke`

Native routes pass input directly to the compiled graph. When a checkpointer is
attached, LangGraph requires `config.configurable.thread_id`; omit `config`
entirely if the graph has no checkpointer.

```bash
curl -s -X POST "$BASE_URL/api/graphs/simple_agent/invoke" \
  -H "Content-Type: application/json" \
  -d '{"input":{"messages":[{"role":"human","content":"World"}],"greeting":""},"config":{"configurable":{"thread_id":"native-001"}}}'
```

Representative response:

```json
{"output":{"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"}}
```

### `POST /api/graphs/simple_agent/stream`

```bash
curl -s -X POST "$BASE_URL/api/graphs/simple_agent/stream" \
  -H "Content-Type: application/json" \
  -d '{"input":{"messages":[{"role":"human","content":"World"}],"greeting":""},"config":{"configurable":{"thread_id":"native-001"}}}'
```

Representative response (buffered SSE body). The `simple_agent` has two nodes
(`greet` → `farewell`), so `stream_mode="values"` emits the full state three
times — the initial input, then after each node:

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

## Verify platform-compatible routes

### Create thread

Create a thread and capture its ID for subsequent calls:

```bash
THREAD_ID=$(curl -s -X POST "$BASE_URL/api/threads" \
  -H "Content-Type: application/json" \
  -d '{}' | jq -r '.thread_id')
echo "$THREAD_ID"
```

Representative response:

```json
{"thread_id":"550e8400-e29b-41d4-a716-446655440000","created_at":"2025-01-15T10:30:00Z","updated_at":"2025-01-15T10:30:00Z","metadata":null,"status":"idle","values":null,"assistant_id":null,"interrupts":{}}
```

### Invoke agent on thread

```bash
curl -s -X POST "$BASE_URL/api/threads/$THREAD_ID/runs/wait"   -H "Content-Type: application/json"   -d '{"assistant_id":"simple_agent","input":{"messages":[{"role":"human","content":"World"}],"greeting":""}}'
```

Representative response:

```json
{"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"}
```

The run ID is returned in the `Content-Location` header (for example,
`/api/threads/550e8400-e29b-41d4-a716-446655440000/runs/<run-id>`), not in the response body.

### Get thread state

```bash
curl -s "$BASE_URL/api/threads/$THREAD_ID/state"
```

Representative response:

```json
{"values":{"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"},"next":[],"checkpoint":{"thread_id":"550e8400-e29b-41d4-a716-446655440000","checkpoint_ns":"","checkpoint_id":"1efc4c4f-0000-0003-8000-000000000000","checkpoint_map":null},"metadata":{"source":"loop","step":2,"parents":{}},"created_at":"2025-01-15T10:30:05Z","parent_checkpoint":{"thread_id":"550e8400-e29b-41d4-a716-446655440000","checkpoint_ns":"","checkpoint_id":"1efc4c4f-0000-0002-8000-000000000000","checkpoint_map":null},"tasks":[],"interrupts":[]}
```

### List threads

```bash
curl -s -X POST "$BASE_URL/api/threads/search" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Representative response:

```json
[{"thread_id":"550e8400-e29b-41d4-a716-446655440000","created_at":"2025-01-15T10:30:00Z","updated_at":"2025-01-15T10:30:05Z","metadata":null,"status":"idle","values":{"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"},"assistant_id":"simple_agent","interrupts":{}}]
```

### Remaining platform endpoints (compact verification)

#### `GET /api/threads/{id}`

```bash
curl -s "$BASE_URL/api/threads/$THREAD_ID"
```

Representative response:

```json
{"thread_id":"550e8400-e29b-41d4-a716-446655440000","created_at":"2025-01-15T10:30:00Z","updated_at":"2025-01-15T10:30:05Z","metadata":null,"status":"idle","values":{"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"},"assistant_id":"simple_agent","interrupts":{}}
```

#### `POST /api/threads/{id}/runs/stream`

```bash
curl -s -X POST "$BASE_URL/api/threads/$THREAD_ID/runs/stream"   -H "Content-Type: application/json"   -d '{"assistant_id":"simple_agent","input":{"messages":[{"role":"human","content":"World"}],"greeting":""}}'
```

Representative response (buffered SSE body). Like the native stream, `stream_mode="values"`
emits the full state three times (initial input, post-greet, post-farewell):

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

#### `POST /api/runs/wait`

```bash
curl -s -X POST "$BASE_URL/api/runs/wait"   -H "Content-Type: application/json"   -d '{"assistant_id":"simple_agent","input":{"messages":[{"role":"human","content":"World"}],"greeting":""}}'
```

Representative response:

```json
{"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"}
```

The run ID is returned in the `Content-Location` header (for example,
`/api/runs/<run-id>`), not in the response body.

#### `POST /api/runs/stream`

```bash
curl -s -X POST "$BASE_URL/api/runs/stream"   -H "Content-Type: application/json"   -d '{"assistant_id":"simple_agent","input":{"messages":[{"role":"human","content":"World"}],"greeting":""}}'
```

Representative response (buffered SSE body):

```text
event: metadata
data: {"run_id": "92e7ed3d-cb23-4a52-954b-92f634321111"}

event: values
data: {"messages":[{"role":"human","content":"World"}],"greeting":""}

event: values
data: {"messages":[{"role":"human","content":"World"}],"greeting":"Hello, World!"}

event: values
data: {"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"}

event: end
data: null
```

#### `POST /api/threads/{id}/state`

```bash
curl -s -X POST "$BASE_URL/api/threads/$THREAD_ID/state"   -H "Content-Type: application/json"   -d '{"values":{"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"}}'
```

Representative response:

```json
{"checkpoint":{"thread_id":"550e8400-e29b-41d4-a716-446655440000","checkpoint_ns":"","checkpoint_id":"1efc4c4f-0000-0004-8000-000000000000","checkpoint_map":null}}
```

#### `POST /api/threads/{id}/history`

```bash
curl -s -X POST "$BASE_URL/api/threads/$THREAD_ID/history" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Representative response (truncated — the full history contains one snapshot per
graph step plus any manual state updates, in reverse chronological order):

```json
[
  {
    "values": {"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"},
    "next": [],
    "checkpoint": {"thread_id":"550e8400-e29b-41d4-a716-446655440000","checkpoint_ns":"","checkpoint_id":"1efc4c4f-0000-0004-8000-000000000000","checkpoint_map":null},
    "metadata": {"source":"update","step":3,"parents":{}},
    "created_at": "2025-01-15T10:30:10Z",
    "parent_checkpoint": {"thread_id":"550e8400-e29b-41d4-a716-446655440000","checkpoint_ns":"","checkpoint_id":"1efc4c4f-0000-0003-8000-000000000000","checkpoint_map":null},
    "tasks": [],
    "interrupts": []
  },
  {
    "values": {"messages":[{"role":"human","content":"World"},{"role":"assistant","content":"Hello, World! Goodbye!"}],"greeting":"Hello, World!"},
    "next": [],
    "checkpoint": {"thread_id":"550e8400-e29b-41d4-a716-446655440000","checkpoint_ns":"","checkpoint_id":"1efc4c4f-0000-0003-8000-000000000000","checkpoint_map":null},
    "metadata": {"source":"loop","step":2,"parents":{}},
    "created_at": "2025-01-15T10:30:05Z",
    "parent_checkpoint": {"thread_id":"550e8400-e29b-41d4-a716-446655440000","checkpoint_ns":"","checkpoint_id":"1efc4c4f-0000-0002-8000-000000000000","checkpoint_map":null},
    "tasks": [],
    "interrupts": []
  },
  {
    "values": {"messages":[{"role":"human","content":"World"}],"greeting":"Hello, World!"},
    "next": ["farewell"],
    "checkpoint": {"thread_id":"550e8400-e29b-41d4-a716-446655440000","checkpoint_ns":"","checkpoint_id":"1efc4c4f-0000-0002-8000-000000000000","checkpoint_map":null},
    "metadata": {"source":"loop","step":1,"parents":{}},
    "created_at": "2025-01-15T10:30:04Z",
    "parent_checkpoint": {"thread_id":"550e8400-e29b-41d4-a716-446655440000","checkpoint_ns":"","checkpoint_id":"1efc4c4f-0000-0001-8000-000000000000","checkpoint_map":null},
    "tasks": [],
    "interrupts": []
  }
]
```

> The full response includes additional earlier snapshots (`step: 0` for graph
> entry, `step: -1` for the initial input). Use the `before` parameter in the
> request body to paginate through history.

#### `POST /api/assistants/search`

```bash
curl -s -X POST "$BASE_URL/api/assistants/search" -H "Content-Type: application/json" -d '{}'
```

Representative response:

```json
[{"assistant_id":"simple_agent","graph_id":"simple_agent","config":{},"created_at":"2025-01-15T10:27:41Z","metadata":null,"version":1,"name":"simple_agent","description":"A simple two-node greeting agent","updated_at":"2025-01-15T10:27:41Z","context":{}}]
```

#### `GET /api/openapi.json`

```bash
curl -s "$BASE_URL/api/openapi.json"
```

Representative response (truncated):

```json
{"openapi":"3.0.3","info":{"title":"azure-functions-langgraph","version":"0.5.0"},"paths":{"/health":{"get":{}},"/graphs/simple_agent/invoke":{"post":{}},"/graphs/simple_agent/stream":{"post":{}}}}
```

> ⚠️ The built-in OpenAPI endpoint is deprecated since v0.5.0 and will be removed in v1.0. Use [`azure-functions-openapi`](https://github.com/yeongseon/azure-functions-openapi) with `register_with_openapi()` instead. The response includes an `X-Deprecation` header.

The built-in `openapi.json` includes native routes only (such as `invoke`/`stream`), not platform-compatible routes.

For errors and recovery paths, use [`troubleshooting.md`](./troubleshooting.md).

## Monitoring

```bash
func azure functionapp logstream <YOUR_FUNCTION_APP_NAME>
```

Representative output:

```text
Connecting to log-streaming service...
2025-01-15T10:34:41Z   [Information]   Executing 'Functions.aflg_health' (Reason='This function was programmatically called via the host APIs.', Id=ab2a5eb3-1f80-46ea-a818-601ca6ed1111)
2025-01-15T10:34:41Z   [Information]   Executed 'Functions.aflg_health' (Succeeded, Id=ab2a5eb3-1f80-46ea-a818-601ca6ed1111, Duration=12ms)
2025-01-15T10:34:48Z   [Information]   Executing 'Functions.aflg_platform_runs_wait' (Reason='This function was programmatically called via the host APIs.', Id=4d5267c7-f7dc-4f42-97de-1023d4e92222)
2025-01-15T10:34:48Z   [Information]   Executed 'Functions.aflg_platform_runs_wait' (Succeeded, Id=4d5267c7-f7dc-4f42-97de-1023d4e92222, Duration=41ms)
```

```bash
APP_INSIGHTS_APP_ID=$(az monitor app-insights component show   --app <YOUR_FUNCTION_APP_NAME>   --resource-group <YOUR_RESOURCE_GROUP>   --query appId -o tsv)

az monitor app-insights query   --app "$APP_INSIGHTS_APP_ID"   --analytics-query "requests | where timestamp > ago(30m) | project timestamp, name, resultCode, duration, success | order by timestamp desc | take 10"
```

Representative output:

```json
{"tables":[{"name":"PrimaryResult","columns":[{"name":"timestamp","type":"datetime"},{"name":"name","type":"string"},{"name":"resultCode","type":"string"},{"name":"duration","type":"real"},{"name":"success","type":"string"}],"rows":[["2025-01-15T10:34:48.552Z","POST /api/threads/{thread_id}/runs/wait","200",41.0,"True"],["2025-01-15T10:34:41.884Z","GET /api/health","200",12.0,"True"]]}]}
```

## Cleanup

```bash
az group delete --name <YOUR_RESOURCE_GROUP> --yes --no-wait
```

Representative output:

```text
{"status":"Accepted"}
```

## Sources

- [Azure Functions Python quickstart](https://learn.microsoft.com/en-us/azure/azure-functions/create-first-function-cli-python)
- [Azure Functions Core Tools publish reference](https://learn.microsoft.com/en-us/azure/azure-functions/functions-core-tools-reference#func-azure-functionapp-publish)
- [Function App settings](https://learn.microsoft.com/en-us/azure/azure-functions/functions-how-to-use-azure-function-app-settings)
- [Create a storage account](https://learn.microsoft.com/en-us/azure/storage/common/storage-account-create)
- [Azure Blob Storage documentation](https://learn.microsoft.com/en-us/azure/storage/blobs/)
- [Azure Table Storage documentation](https://learn.microsoft.com/en-us/azure/storage/tables/)
- [Functions monitoring and telemetry](https://learn.microsoft.com/en-us/azure/azure-functions/analyze-telemetry-data)

## See Also

- [`production-guide.md`](./production-guide.md)
- [`configuration.md`](./configuration.md)
- [`examples/simple_agent.md`](./examples/simple_agent.md)
- [`architecture.md`](./architecture.md)
- [`troubleshooting.md`](./troubleshooting.md)
- [`azure-functions-logging`](https://github.com/yeongseon/azure-functions-logging)
- [`azure-functions-doctor`](https://github.com/yeongseon/azure-functions-doctor)
