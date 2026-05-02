# Cosmos DB Checkpointing

This example shows how to persist LangGraph checkpoints in Azure Cosmos DB
from an Azure Functions Python app.

> **Experimental:** Cosmos DB checkpointer support is new and may change
> before v1.0.

## When to use this example

Use this example when you want:

- Azure-native checkpoint persistence
- Managed Identity / DefaultAzureCredential
- A serverless-friendly production backend
- Multi-instance Azure Functions compatibility

## Requirements

- Python 3.11+
- Azure Cosmos DB for NoSQL account
- Cosmos DB database and container
- Container partition key path: `/partition_key`
- Managed Identity or local `az login`

## Files

- `function_app.py` — wires `create_cosmos_checkpointer` with default credential resolution (`DefaultAzureCredential`)
- `graph.py` — turn-counting echo agent (storage-free, used by smoke tests)
- `host.json`, `local.settings.json.example`, `requirements.txt`

## App Settings

| Setting | Description |
|---|---|
| `AZURE_COSMOS_ENDPOINT` | Cosmos DB account endpoint |
| `LANGGRAPH_COSMOS_DATABASE` | Cosmos DB database name (default: `langgraph`) |
| `LANGGRAPH_COSMOS_CONTAINER` | Cosmos DB container name (default: `checkpoints`) |

## Azure Cosmos DB setup

1. Create an Azure Cosmos DB account (NoSQL API)
2. Create a database (e.g. `langgraph`)
3. Create a container with partition key path `/partition_key`

## Local development

Run against a real Cosmos DB account using Azure CLI credentials:

```bash
az login

cp local.settings.json.example local.settings.json
# Edit local.settings.json with your Cosmos DB endpoint

pip install -r requirements.txt
func start
```

> **Note:** This example does not use the Cosmos DB Emulator.
> A real Cosmos DB account is required for local testing.

## Production

Enable Managed Identity on the Function App and grant the required
Cosmos DB data-plane role:

```bash
PRINCIPAL_ID=$(az functionapp identity show -n <function-app> -g <rg> --query principalId -o tsv)

az cosmosdb sql role assignment create \
  --account-name <cosmos-account> \
  --resource-group <rg> \
  --scope "/" \
  --principal-id "$PRINCIPAL_ID" \
  --role-definition-id "00000000-0000-0000-0000-000000000002"  # Cosmos DB Built-in Data Contributor
```

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

- This example does not use the Cosmos DB Emulator.
- This example does not use connection strings by default.
- The `cosmos` extra requires Python 3.11+.
- The Cosmos DB container must be created with partition key path `/partition_key`.
