# Managed Identity (Blob + Table)

End-to-end example using **Managed Identity** for `AzureBlobCheckpointSaver`
and `AzureTableThreadStore` in production, with a connection-string fallback
for local development against [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite).

This is the recommended production wiring for the bundled persistent backends:
no secrets in App Settings, role-based access only.

## Files

- `function_app.py` — switches between `DefaultAzureCredential` (prod) and connection string (Azurite/local)
- `graph.py` — turn-counting echo agent
- `host.json`, `local.settings.json.example`, `requirements.txt`

## How the wiring works

`function_app.py` looks at env vars on cold start:

| Mode | Env vars set | Storage clients |
| --- | --- | --- |
| Production / Managed Identity | `AZURE_STORAGE_BLOB_ACCOUNT_URL` + `AZURE_TABLE_ENDPOINT` | `ContainerClient(account_url=..., credential=DefaultAzureCredential())` and `TableClient(endpoint=..., credential=DefaultAzureCredential())` |
| Local dev / CI | `AZURE_STORAGE_CONNECTION_STRING` (e.g. Azurite) | `ContainerClient.from_connection_string(...)` / `TableClient.from_connection_string(...)` |

The Table client is then handed to the new
[`AzureTableThreadStore.from_table_client()`](../../README.md#persistent-storage-v04)
factory so the credential flows through unchanged.

## Run locally with Azurite

Start Azurite (Docker):

```bash
docker run -d --name azurite \
  -p 10000:10000 -p 10001:10001 -p 10002:10002 \
  mcr.microsoft.com/azure-storage/azurite
```

Then:

```bash
cd examples/managed_identity_storage
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

The example ships `AZURE_STORAGE_CONNECTION_STRING=UseDevelopmentStorage=true`
in `local.settings.json.example`, so it runs hermetically against Azurite
without needing real Azure credentials.

## Production deploy with Managed Identity

### 1. Enable a Managed Identity on the Function App

System-assigned (simplest) or user-assigned. Examples below assume
system-assigned.

```bash
az functionapp identity assign \
  --name <function-app-name> \
  --resource-group <rg>
```

### 2. Grant role assignments on the storage account

The bundled backends need data-plane access to **both** Blob and Table services:

| Role | Scope | Used by |
| --- | --- | --- |
| `Storage Blob Data Contributor` | the storage account (or the container) | `AzureBlobCheckpointSaver` |
| `Storage Table Data Contributor` | the storage account (or the table) | `AzureTableThreadStore` |

```bash
PRINCIPAL_ID=$(az functionapp identity show -n <function-app> -g <rg> --query principalId -o tsv)
STORAGE_ID=$(az storage account show -n <storage-account> -g <rg> --query id -o tsv)

az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee "$PRINCIPAL_ID" \
  --scope "$STORAGE_ID"

az role assignment create \
  --role "Storage Table Data Contributor" \
  --assignee "$PRINCIPAL_ID" \
  --scope "$STORAGE_ID"
```

For tighter scoping, target the container and table individually
(`/blobServices/default/containers/<name>` and
`/tableServices/default/tables/<name>`).

### 3. Set App Settings (no secrets)

```bash
az functionapp config appsettings set \
  --name <function-app> --resource-group <rg> \
  --settings \
    AZURE_STORAGE_BLOB_ACCOUNT_URL="https://<storage-account>.blob.core.windows.net" \
    AZURE_TABLE_ENDPOINT="https://<storage-account>.table.core.windows.net" \
    LANGGRAPH_BLOB_CONTAINER="langgraph-checkpoints" \
    LANGGRAPH_THREADS_TABLE="langgraphthreads"
```

Do **not** set `AZURE_STORAGE_CONNECTION_STRING` in production — its presence
would also work, but it puts a credential into App Settings and defeats the
point of using Managed Identity.

### 4. Pre-create the container (one-time)

`function_app.py` calls `container.create_container()` if missing, which
requires the role assignment above to be already in place. Alternatively,
create it once via the portal or CLI:

```bash
az storage container create \
  --account-name <storage-account> --name langgraph-checkpoints \
  --auth-mode login
```

The Table is created lazily by the store on first write, so no extra step is
required for it.

## Local-dev fallback to Azure CLI credential

`DefaultAzureCredential` walks a chain of credentials in order. The two
relevant for local dev are:

1. **Environment variables** (`AZURE_CLIENT_ID` / `AZURE_TENANT_ID` /
   `AZURE_CLIENT_SECRET`) — only if you set them.
2. **`AzureCliCredential`** — uses your `az login` session.

To run this example against a real Azure storage account from your
workstation (instead of Azurite):

```bash
az login

export AZURE_STORAGE_BLOB_ACCOUNT_URL="https://<storage-account>.blob.core.windows.net"
export AZURE_TABLE_ENDPOINT="https://<storage-account>.table.core.windows.net"
unset AZURE_STORAGE_CONNECTION_STRING   # force the MI/credential branch

func start
```

Make sure your user account has `Storage Blob Data Contributor` and
`Storage Table Data Contributor` on the account — RBAC applies the same way
to your `az login` identity as it does to a Function App's Managed Identity.

## Verify persistence

```bash
KEY="<function-key-or-leave-blank-for-anonymous>"
THREAD=$(curl -s -X POST "http://localhost:7071/api/threads?code=$KEY" \
  -H "Content-Type: application/json" -d '{}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["thread_id"])')

curl -s -X POST "http://localhost:7071/api/threads/$THREAD/runs/wait?code=$KEY" \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"managed_identity_agent","input":{"messages":[{"role":"human","content":"first"}]}}'

curl -s -X POST "http://localhost:7071/api/threads/$THREAD/runs/wait?code=$KEY" \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"managed_identity_agent","input":{"messages":[{"role":"human","content":"second"}]}}'
```

The second response shows `[turn 2]`. Restart `func start` and the counter
still increments because state lives in storage.

## See also

- [`examples/persistent_agent_blob_table/`](../persistent_agent_blob_table/) — same wiring driven by a connection string only.
- [README → Persistent storage](../../README.md#persistent-storage-v04) — narrative docs and scale envelope.
- [`docs/production-guide.md`](../../docs/production-guide.md) — Key Vault references and other production concerns.
