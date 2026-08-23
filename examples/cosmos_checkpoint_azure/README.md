# Cosmos DB Checkpointing

This example shows how to persist LangGraph checkpoints in Azure Cosmos DB
from an Azure Functions Python app.

> **Experimental:** Cosmos DB checkpointer support is new and may change
> before v1.0.

## When to use this example

Use this example when you want:

- Azure-native checkpoint persistence
- Key-based authentication with Cosmos DB
- A serverless-friendly production backend
- Multi-instance Azure Functions compatibility

## Requirements

- Azure Cosmos DB for NoSQL account
- Cosmos DB database and container
- Container partition key path: `/partition_key`
- A Cosmos DB account key (set as `COSMOS_KEY`)

## Files

- `function_app.py` — wires `create_cosmos_checkpointer` with key-based auth
- `graph.py` — turn-counting echo agent (storage-free, used by smoke tests)
- `host.json`, `local.settings.json.example`, `requirements.txt`

## App Settings

| Setting | Description |
|---|---|
| `AZURE_COSMOS_ENDPOINT` | Cosmos DB account endpoint |
| `COSMOS_KEY` | Cosmos DB account key (wrapper convention; helper wires it to upstream's `COSMOSDB_KEY`) |
| `LANGGRAPH_COSMOS_DATABASE` | Cosmos DB database name (default: `langgraph`) |
| `LANGGRAPH_COSMOS_CONTAINER` | Cosmos DB container name (default: `checkpoints`) |

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

## Production

Set `AZURE_COSMOS_ENDPOINT` and `COSMOS_KEY` as App Settings on the Function App.

> **Note:** `COSMOS_KEY` is a wrapper-level convention. The helper
> temporarily wires it to the upstream `COSMOSDB_KEY` environment variable
> during saver creation.

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

- The `create_cosmos_checkpointer` helper currently uses key-based authentication.
- The upstream `langgraph-checkpoint-cosmosdb` package (>= 0.2.8) **does** support
  Managed Identity / `DefaultAzureCredential` — its `CosmosDBSaver` falls back to
  `DefaultAzureCredential` whenever `COSMOSDB_KEY` is unset. To use passwordless
  auth today, instantiate `CosmosDBSaver` directly (only set `COSMOSDB_ENDPOINT`,
  no key) instead of calling this key-based helper, and grant the Function App's
  identity a Cosmos DB data-plane role. A future release may add a first-class
  Managed Identity path to the helper.
- The Cosmos DB container must be created with partition key path `/partition_key`.
- `COSMOS_KEY` is not the same as upstream's `COSMOSDB_KEY`; the helper handles the mapping.
