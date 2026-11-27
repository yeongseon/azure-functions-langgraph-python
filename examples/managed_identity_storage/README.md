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

> The **runnable example** branches on env vars to support both modes from
> a single `function_app.py`. The standalone snippet in the README's
> *Persistent storage with Managed Identity* section uses
> `DefaultAzureCredential` directly — that wiring works in Azure Functions
> and locally with `az login` without any conditional code.

## Run locally with Azurite

Start Azurite (Docker), exposing **all three** ports — port `10002` is
required for the Table service:

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

### 4. Pre-create the container and table (recommended)

Pre-creating storage resources avoids cold-start side effects and avoids
RBAC propagation timing issues:

```bash
az storage container create \
  --account-name <storage-account> --name langgraph-checkpoints \
  --auth-mode login

az storage table create \
  --account-name <storage-account> --name langgraphthreads \
  --auth-mode login
```

`AzureTableThreadStore` does **not** create the table on first write —
calls will fail with `ResourceNotFoundError` if the table is missing,
so the table must exist before the Function App handles its first
request. The same applies to `AzureBlobCheckpointSaver` and the
container.

For local Azurite runs, the example sets
`LANGGRAPH_AUTO_CREATE_STORAGE=true` in `local.settings.json.example`
so it bootstraps both resources at cold start. **Do not set this in
production** — pre-create instead.

### 5. Note on RBAC propagation

Azure RBAC role assignments can take a few minutes to propagate. If you
deploy and immediately see `403 Forbidden` from Blob or Table operations,
wait 1–5 minutes and retry before debugging further.

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

The example deploys with `auth_level=FUNCTION`, so a function key is
required when hitting a deployed app. Local `func start` may also accept
requests without a key depending on host version — supply one if you
have it:

```bash
KEY="<function-key>"   # required for deployed apps; may be optional locally
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

## Certification storage migration evidence — 2026-11-27 UTC

For [issue #1013](https://github.com/yeongseon/azure-functions-langgraph-python/issues/1013).
**Partial evidence, not a successful keyless certification.** No infrastructure,
example behavior, workflow, release gate or default changed. All temporary Azure
resources were deleted and deletion verified before this section was written.

### Identity and least-privilege mapping

The certification topology in `infra/main.bicep` / `e2e-azure.yml` has independent
App A, App B and true-streaming apps, each with its own plan, sharing a storage
account. A staged identity migration needs a distinct managed identity on **each**
app; enabling one app's identity does not authorize the others.

| Consumer | Identity settings / credential | Minimum built-in role and scope |
| --- | --- | --- |
| Host storage, A/B/streaming | Each app's system-assigned identity; `AzureWebJobsStorage__accountName=<account>` and `AzureWebJobsStorage__credential=managedidentity`; remove exact `AzureWebJobsStorage` setting | Storage Blob Data Owner on its host account, for host containers and function-key storage |
| Flex deployment package, each app | `functionAppConfig.deployment.storage.authentication.type=SystemAssignedIdentity`; separate deployment container per app | Storage Blob Data Contributor on the pre-created deployment container |
| Y1 external package download, each app | Private package URL in `WEBSITE_RUN_FROM_PACKAGE`; default system identity, optionally explicit `WEBSITE_RUN_FROM_PACKAGE_BLOB_MI_RESOURCE_ID=SystemAssigned` | Storage Blob Data Reader for package access; container scope is a practical assignment boundary |
| A/B checkpoint writes and lease markers | Explicit Blob endpoint plus `DefaultAzureCredential`, not a lookup of the host connection string | Storage Blob Data Contributor on `langgraph-checkpoints` and `langgraph-locks` containers, for **both** identities |
| True-streaming application | No checkpoint/lease backend wired in `examples/true_streaming_agent` | No application Blob role; host/deployment roles still apply |
| Optional Platform thread records | `AZURE_TABLE_ENDPOINT` with the app's SDK credential | Storage Table Data Contributor on the pre-created table; not part of the native certification topology |
| Optional host diagnostic Table events | Host identity | Storage Table Data Contributor on host storage; not exercised in these runs |

On one shared account, the host's Blob Data Owner assignment subsumes deployment,
checkpoint and lease permissions: redundant Contributor/Reader assignments do not
reduce privilege. Separate host/application/package accounts would make the narrower
roles meaningful, but were not provisioned or verified here. Blob Data Owner is a
storage data role, **not** the subscription/resource-group Owner role. No Queue role
is needed for the HTTP-only topology; Blob triggers or Durable extensions have
additional requirements and are out of scope. Public storage endpoints are used;
anonymous Blob access is disabled. Function keys do not establish thread ownership.

### Compatibility boundary and staged migration

Microsoft documents identity-based host storage on Functions v4 for both plans,
with additional Consumption deployment constraints:

- Flex supports managed-identity deployment storage without Azure Files. Existing
  `infra/flex.bicep` already has `identityBasedStorage=false` by default; the true
  branch selects identity host/deployment settings and disables Shared Key.
- Linux Y1 uses host storage for remote-build artifacts. With identity host storage,
  Microsoft requires an **external, ready-to-run package** instead of that remote-build
  path. Azure Files content storage does not support MI; an app without Azure Files
  or a separately assessed Key Vault compatibility path is needed. No fully keyless
  Y1 external-package run was completed here.
- Changing host settings is insufficient: `examples/e2e_app/function_app.py` reads
  `os.environ["AzureWebJobsStorage"]` for its lease client, and `lock_graph.py` does
  the same for its checkpointer. The outer exception handler can leave the baseline
  graph running without registering `e2e_lock_agent`. Its requirements also omit
  `azure-identity`. Both client seams and dependencies must gain an explicit opt-in
  MI path before a two-app keyless smoke can certify checkpoint/lease behavior.
- Preserve the current Y1 and optional Flex/Python 3.13 lanes while developing that
  opt-in path. Pre-create both application containers, grant every app identity its
  scoped roles, allow propagation, deploy the candidate wheel, require the complete
  route inventory, then run native, two-app lock and true-streaming smoke. Do not
  replace current gates or disable Shared Key on an existing deployment based on
  these partial observations.

The [production reference](../../docs/production-reference.md) already records a
successful MI Blob/lease run for **one Flex/Python 3.11 app using the published
wheel**, including its limitations. That is prior evidence, not a candidate-wheel
or three-app certification result for this issue.

### Manual candidate experiments

Source: clean `origin/main` at `7776a5bfa1eebe64fda44b73126d0fdfb1fc47e6`.
Adapter `0.14.2` built with `uv build --wheel`, bundled under `wheels/` and referenced
by local path in copied payload requirements. Wheel SHA-256:
`fca98285bc97dd2929674e329f3343ba8eb3404a4512eceb2ab433102dd98be3`.
Tools: Azure CLI 2.83.0, Bicep 0.41.2, Core Tools 4.8.0, uv 0.11.7.
Core Tools detected local Python 3.10.12 and warned about the remote-version mismatch;
the configured Azure versions below were not confirmed by a working runtime route.
All groups used Korea Central, `purpose=1013-verify owner=opencode-temp`, Standard_LRS,
no telemetry/model/database resources and no always-ready capacity.

| Experiment | Accepted configuration and actual outcome |
| --- | --- |
| Flex setup attempt, 23:03 UTC | FC1, Python 3.13, 512 MB, max 40; provisioned in 68 s. Wrapper stopped before publish because the copied payload already contained `wheels/`. Deleted immediately; no runtime evidence. |
| Flex native candidate, 23:06–23:10 UTC | Same configuration; provision 68 s. Account Shared Key false, public Blob false, TLS1_2; exactly one Blob Data Owner role, case-insensitive scope match **true**, app-principal match **true**. Identity-only host settings, deployment auth `SystemAssignedIdentity`. Remote Oryx build/upload accepted, publish exit 0 in 169 s (176 s after role inventory). **Zero indexed functions**, `/api/e2e/runtime` returned “Function host is not running.” Publisher's host-status response reported Running, host `4.1055.300.26527`, which did not prove indexing. Local pytest launch then failed because the worktree Hatch interpreter did not exist: **no tests ran**. Exact indexing failure was not captured; the connection-string client mismatch above is a separate code-confirmed migration blocker, not an established explanation for zero functions. |
| Y1 boundary, 23:11–23:13 UTC | Linux Y1, Python 3.12, Functions v4; `az functionapp create --disable-app-insights true`. Provision/configuration 78 s; removed exact host connection string and Azure Files content settings, set MI host settings and Oryx build flags, disabled Shared Key. Account policy and role/principal/scope matches as above. `AzureWebJobsDashboard` remained in the setting-name inventory; its value was not inspected, so absence of every secret setting is **not** claimed. Core Tools rejected remote publish (exit 1, 5 s), saying the newly created app predates August 2019 and does not support remote build. Treat that misleading date as the actual CLI diagnostic, not the app's creation date or proof that Y1 rejects MI host settings. No indexing or HTTP smoke. |
| Flex existing MI reference candidate, 23:17 UTC | Reused `production_persistent_agent`, fake model and Blob/identity wheel extras; FC1/Python 3.11, 512 MB, max 40. Provision 99 s; same verified role/policy/configuration. Core Tools could not find the newly created app, exit 1 in 2 s (8 s after role inventory). No publish, indexing or runtime smoke. Deleted immediately; no retries or privileges expanded. |

Propagation is **not measured independently**: ARM role inventory proves assignments,
not data-plane readiness. The successful Flex package upload shows deployment storage
authorization worked by publication completion, at the recorded interval; no 403
retry sequence or precise propagation delay was observed. No two-app contention,
cross-app checkpoint round trip, true-streaming smoke, external-package Y1 smoke,
runtime distribution inventory, scale-out, lease-loss injection or long-running
workflow behavior was established. No latency, cost, fencing or exactly-once claim.

### Verified cleanup

Each experiment had an EXIT trap, synchronous `az group delete --yes`, followed by
`az group wait --deleted`. All four groups were deleted within minutes. After **each**:

```text
az group exists -n <owned-group>: false
az resource list --tag purpose=1013-verify --query '[].name': []
az group list --query "[?starts_with(name,'DefaultResourceGroup-')].name": []
```

The default-group list was also empty before provisioning. Group deletion removed
the apps/system identities, plans, accounts/containers and resource-scoped role
assignments. No separately created Entra object or subscription-wide assignment.
No unrelated resource group was modified or used for deployment.

### Official references

- [Identity host settings, roles, Consumption remote-build limitation](https://learn.microsoft.com/azure/azure-functions/manage-connections?tabs=host&pivots=functions-auth-identity)
- [Package deployment by plan and MI external package access](https://learn.microsoft.com/azure/azure-functions/deployment-zip-push)
- [Flex deployment authentication configuration](https://learn.microsoft.com/azure/azure-functions/flex-consumption-how-to#configure-deployment-settings)

## See also

- [`examples/persistent_agent_blob_table/`](../persistent_agent_blob_table/) — same wiring driven by a connection string only.
- [README → Persistent storage](../../README.md#persistent-storage-v04) — narrative docs and scale envelope.
- [`docs/production-guide.md`](../../docs/production-guide.md) — Key Vault references and other production concerns.
- [Troubleshooting → Storage and persistence settings](../../docs/troubleshooting.md#storage-and-persistence-settings) — verified startup and first-request storage failures.
