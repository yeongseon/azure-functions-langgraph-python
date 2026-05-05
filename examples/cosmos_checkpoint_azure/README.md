# Cosmos DB Checkpointing

This example shows how to persist LangGraph checkpoints in Azure Cosmos DB
from an Azure Functions Python app.

> **Experimental:** Cosmos DB checkpointer support is new and may change
> before v1.0.

## When to use this example

Use this example when you want:

- Azure-native checkpoint persistence
- Key-based Cosmos DB authentication
- A serverless-friendly production backend
- Multi-instance Azure Functions compatibility

## Requirements

- Python 3.10+
- Azure Cosmos DB for NoSQL account
- Cosmos DB database and container
- Container partition key path: `/partition_key`
- Cosmos DB master key (or `COSMOS_KEY` environment variable)

## Files

- `function_app.py` — wires `create_cosmos_checkpointer` with key-based authentication
- `graph.py` — turn-counting echo agent (storage-free, used by smoke tests)
- `host.json`, `local.settings.json.example`, `requirements.txt`

## App Settings

| Setting | Description |
|---|---|
| `AZURE_COSMOS_ENDPOINT` | Cosmos DB account endpoint |
| `COSMOS_KEY` | Cosmos DB master key (wrapper convention — see note below) |
| `LANGGRAPH_COSMOS_DATABASE` | Cosmos DB database name (default: `langgraph`) |
| `LANGGRAPH_COSMOS_CONTAINER` | Cosmos DB container name (default: `checkpoints`) |

> **Note:** `COSMOS_KEY` is a convenience convention defined by this wrapper's
> `create_cosmos_checkpointer()` helper.  It is **not** read by the upstream
> `langgraph-checkpoint-cosmosdb` package directly.  You can also pass `key=`
> explicitly instead of relying on the env var.

## Azure Cosmos DB setup

1. Create an Azure Cosmos DB account (NoSQL API)
2. Create a database (e.g. `langgraph`)
3. Create a container with partition key path `/partition_key`

## Local development

```bash
cp local.settings.json.example local.settings.json
# Edit local.settings.json with your Cosmos DB endpoint and key

pip install -r requirements.txt
func start
```

You can also use the Cosmos DB Emulator for local development (see
`tests/integration/` for emulator-based integration tests).

## Production

Store the Cosmos DB master key in Azure Function App Settings (or Key Vault
reference) and set `AZURE_COSMOS_ENDPOINT` and `COSMOS_KEY` as app settings.

> **Managed Identity note:** The upstream `langgraph-checkpoint-cosmosdb`
> package currently requires a Cosmos DB master key (it reads `COSMOSDB_KEY`
> from the environment internally).  Managed Identity / `DefaultAzureCredential`
> is not supported by the upstream package at this time.  If upstream adds
> `TokenCredential` support in the future, this helper will be updated.

## Verify persistence

```bash
THREAD=$(curl -s -X POST "http://localhost:7071/api/threads" \
  -H "Content-Type: application/json" -d '{}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["thread_id"])')

curl -s -X POST "http://localhost:7071/api/threads/$THREAD/runs/wait" \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"cosmos_agent","input":{"messages":[{"role":"human","content":"first"}]}}'

curl -s -X POST "http://localhost:7071/api/threads/$THREAD/runs/wait" \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"cosmos_agent","input":{"messages":[{"role":"human","content":"second"}]}}'
```

The second response shows `[turn 2]`.

## Notes

- The `cosmos` extra requires Python 3.10+.
- The upstream package uses key-based authentication only.
- The Cosmos DB container must be created with partition key path `/partition_key`.
- You can use the Cosmos DB Emulator for local development and testing.
