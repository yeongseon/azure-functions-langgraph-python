# Azure OpenAI Agent Example

**Start here after the Quick Start.** This is the primary real-world example: a
minimal LangGraph agent backed by a **real Azure OpenAI** chat deployment,
deployed as Azure Functions HTTP endpoints.

The package stays a *deployment adapter* — `graph.py` is a normal LangGraph
`StateGraph`, and `function_app.py` only calls `register()`:

```python
# function_app.py
app = LangGraphApp()
app.register(graph=compiled_graph, name="azure_openai_agent")
func_app = app.function_app
```

> **Why not `simple_agent`?** [`simple_agent`](../simple_agent/) stays
> deterministic (echo, no LLM) so it can be smoke-tested without a model. This
> example supports a real Azure OpenAI deployment and an explicit deterministic
> fake mode for credential-free learning and CI.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4+
- Python 3.11+
- An **Azure OpenAI** resource with a **chat model deployment** (e.g. `gpt-4o-mini`)

## Configuration

Copy the settings template and fill in your Azure OpenAI values:

```bash
cd examples/azure_openai_agent
cp local.settings.json.example local.settings.json
```

| Environment variable | Required | Description |
| --- | --- | --- |
| `AGENT_MODEL_MODE` | Yes | `fake` or `azure_openai`; no implicit default |
| `AZURE_OPENAI_ENDPOINT` | Yes | e.g. `https://<resource>.openai.azure.com/` |
| `AZURE_OPENAI_DEPLOYMENT` | Yes | Your chat deployment name |
| `AZURE_OPENAI_API_VERSION` | No | Defaults to `2024-10-21` |
| `AZURE_OPENAI_API_KEY` | API-key path | The resource key |
| `AZURE_OPENAI_USE_ENTRA_ID` | Entra ID path | Set `true` and leave the key unset |

### Authentication paths

1. **API key (easiest local onboarding).** Set `AZURE_OPENAI_API_KEY`.
2. **Managed Identity / Entra ID (recommended for Azure production).** Leave
   `AZURE_OPENAI_API_KEY` unset and set `AZURE_OPENAI_USE_ENTRA_ID=true`. The
   model authenticates with `DefaultAzureCredential` and the
   `https://cognitiveservices.azure.com/.default` scope — the Function App's
   Managed Identity in Azure, or `az login` locally. No secret lands in App
   Settings.

   Grant the identity the **Cognitive Services OpenAI User** role on the Azure
   OpenAI resource.

## Run locally

```bash
cd examples/azure_openai_agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
func start
```

`local.settings.json.example` ships with `AGENT_MODEL_MODE=fake`. For a live
model, edit your copied `local.settings.json` and set
`AGENT_MODEL_MODE=azure_openai` plus the endpoint, deployment and one
authentication path from the table above. The mode is never inferred: an
incomplete `azure_openai` configuration fails at cold start, it does not switch
to the fake model.

Local `az login` is enough for the Entra ID path (`AZURE_OPENAI_USE_ENTRA_ID=true`);
your signed-in user then needs the same role as the deployed identity (below).

## Test

```bash
# Invoke the agent (native endpoint — the package's primary route)
curl -X POST http://localhost:7071/api/graphs/azure_openai_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "What is Azure Functions in one sentence?"}]}}'

# Health check
curl http://localhost:7071/api/health
```

Expected response shape (model text is illustrative and nondeterministic):

```json
{
  "output": {
    "messages": [
      {"content": "What is Azure Functions in one sentence?", "type": "human", "...": "..."},
      {"content": "Azure Functions is a serverless compute service that runs event-driven code without managing infrastructure.", "type": "ai", "...": "..."}
    ]
  }
}
```

> The response envelope is `{"output": <graph result>}`. Because this graph uses
> LangGraph's `add_messages` state, each item in `messages` is a **serialized
> LangChain message** (fields such as `content`, `type`, `additional_kwargs`,
> `response_metadata`, `id`), not a bare `{"role", "content"}` pair. The exact
> fields and the assistant text vary per model response.

**How to tell fake from real:** in `fake` mode the assistant message is always
`[fake model] deterministic offline response` and carries empty
`response_metadata`. Treat any response containing that marker as **not** a live
model result. A live response has model-generated text and non-empty
`response_metadata`.

## Deploy to Azure

Deploy **this same directory** — `graph.py` and `function_app.py` do not change.
The Azure resources and commands are identical to the default path, so they are
not repeated here. Follow
[Step 5 of the 5-minute tutorial](../../docs/tutorial-5-min.md#step-5-deploy-to-azure-2-min)
(resource group, storage account, Flex Consumption Function App, publish), with
the deltas below, then
[call the deployed app](../../docs/tutorial-5-min.md#call-the-deployed-app) and
[clean up](../../docs/tutorial-5-min.md#clean-up).

### What stays the same as the tutorial

| Step | Same as tutorial |
| --- | --- |
| Resource group, storage account, Function App (Flex Consumption, Python 3.11) | Yes |
| Publish command | Yes — but run it from `examples/azure_openai_agent`, not `simple_agent` |
| Function key retrieval and `?code=` / `x-functions-key` | Yes |
| Cleanup (`az group delete`) | Yes |

### What changes for this example

1. **Azure OpenAI prerequisite.** An existing Azure OpenAI resource with a chat
   deployment. Creating or deploying models is not covered here and is billable.
2. **Publish this directory** (`requirements.txt` here adds `langchain-openai`
   and `azure-identity`):

    ```bash
    cd examples/azure_openai_agent
    func azure functionapp publish "$APP"
    ```

    `func azure functionapp publish` does **not** upload `local.settings.json`
    unless you pass `--publish-local-settings`; do not pass it, so local keys
    never reach the cloud.
3. **Set the model app settings.** Cloud settings replace `local.settings.json`
   (which is local only). The names are exactly those the code reads
   (`model_config.py`).

Set these variables once; both authentication choices below use them:

```bash
RESOURCE_GROUP="rg-langgraph-demo"   # same values as the tutorial
APP="<FUNCTION_APP_NAME>"
AOAI_NAME="<AZURE_OPENAI_RESOURCE_NAME>"
AOAI_RG="<AZURE_OPENAI_RESOURCE_GROUP>"
```

**Managed Identity (recommended for production, no secret in App Settings):**

```bash
# 1. System-assigned identity on the Function App (no role is assigned here)
PRINCIPAL_ID=$(az functionapp identity assign --name "$APP" \
  --resource-group "$RESOURCE_GROUP" --identities "[system]" \
  --query principalId --output tsv)

# 2. Least privilege: inference-only role, scoped to the one Azure OpenAI resource
AOAI_ID=$(az cognitiveservices account show --name "$AOAI_NAME" \
  --resource-group "$AOAI_RG" --query id --output tsv)
az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" --scope "$AOAI_ID"

# 3. App settings; no key is set. --output none avoids echoing the settings back.
az functionapp config appsettings set --name "$APP" \
  --resource-group "$RESOURCE_GROUP" --output none --settings \
  AGENT_MODEL_MODE=azure_openai \
  AZURE_OPENAI_ENDPOINT="<AZURE_OPENAI_ENDPOINT>" \
  AZURE_OPENAI_DEPLOYMENT="<DEPLOYMENT_NAME>" \
  AZURE_OPENAI_USE_ENTRA_ID=true

# 4. `appsettings set` merges. The app uses Entra ID only when no API key is set
#    (`graph.py`), so remove a key left over from the API-key quick test. This is
#    a no-op when no key was ever set.
az functionapp config appsettings delete --name "$APP" \
  --resource-group "$RESOURCE_GROUP" --output none \
  --setting-names AZURE_OPENAI_API_KEY
```

**API key (quick tests only; the key becomes an App Setting):**

```bash
AOAI_KEY=$(az cognitiveservices account keys list --name "$AOAI_NAME" \
  --resource-group "$AOAI_RG" --query key1 --output tsv)
az functionapp config appsettings set --name "$APP" \
  --resource-group "$RESOURCE_GROUP" --output none --settings \
  AGENT_MODEL_MODE=azure_openai \
  AZURE_OPENAI_ENDPOINT="<AZURE_OPENAI_ENDPOINT>" \
  AZURE_OPENAI_DEPLOYMENT="<DEPLOYMENT_NAME>" \
  AZURE_OPENAI_API_KEY="$AOAI_KEY"
unset AOAI_KEY
```

Role assignments can take a few minutes to take effect; restart the app
(`az functionapp restart --name "$APP" --resource-group "$RESOURCE_GROUP"`) if it
cached a failed start. Role and identity background:
[Azure OpenAI RBAC](https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/role-based-access-control),
[managed identities for App Service and Azure Functions](https://learn.microsoft.com/en-us/azure/app-service/overview-managed-identity).

4. **Invoke with the function key** exactly as in the tutorial, against
   `/api/graphs/azure_openai_agent/invoke` (the route name is this example's
   registered graph name). `LangGraphApp` defaults to `AuthLevel.FUNCTION`; the
   `/api/health` probe stays anonymous.

> **Cost:** each invoke consumes tokens on a **billable** Azure OpenAI deployment.

### Which "storage" and "credential" is which

| Name | What it is | Needed here? |
| --- | --- | --- |
| `AzureWebJobsStorage` | Azure Functions **host** storage (keys, deployment package) | Yes, created with the Function App (tutorial) |
| Checkpoint / thread storage (Blob, Table) | Optional **application** persistence; this graph is stateless | No — see [deployment](../../docs/deployment.md#optional-application-storage) |
| Azure OpenAI resource + deployment | The model endpoint and deployment named by `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` | Yes (prerequisite) |
| `AZURE_OPENAI_API_KEY` or Managed Identity | Credential **to the model** | One of the two |
| Function key | Credential **to your HTTP endpoint** | Yes, for invoke |

Premium, persistence and hardening are optional follow-ups: see the
[deployment guide](../../docs/deployment.md) and
[production guide](../../docs/production-guide.md).

### Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `401` from the invoke URL itself | Missing or wrong **function key** | Retrieve the key as in the tutorial |
| No functions listed / indexing error; log shows `Missing Azure OpenAI settings: ...` | Cold-start import failed; the names listed are the missing settings | Set the listed app settings, restart the app |
| No functions listed / indexing error; log shows `AGENT_MODEL_MODE must be set to fake or azure_openai` | `AGENT_MODEL_MODE` not set in the cloud (local file is not published) | Set it as above |
| Functions not listed / indexing fails after publish with an import error | Dependencies not installed or wrong directory published | Check logs; confirm you published `examples/azure_openai_agent`, not another directory |
| Invoke fails; log shows model `401` (key path) | Wrong or rotated `AZURE_OPENAI_API_KEY` | Re-read the key and reset the setting |
| Invoke fails; log shows model `401`/`403` (Entra path) | No identity, role not yet effective, or role on the wrong resource | Verify the identity, the `Cognitive Services OpenAI User` assignment scope, then wait and restart |
| Invoke fails; log shows `404 DeploymentNotFound` | `AZURE_OPENAI_DEPLOYMENT` is not a deployment *name* on that resource | Use the deployment name, not the model name |
| Invoke fails; log shows `429` | Quota / rate limit on the deployment | Retry with backoff or raise quota |

Errors from the SDK, authentication and the service propagate; the example never
downgrades to the fake model.

### Verification status

| Check | Status |
| --- | --- |
| Fake mode, local `func start` + invoke (Python 3.11.15, Core Tools 4.8.0, `azure-functions-langgraph` 0.12.2, `langgraph` 1.2.20, `langchain-openai` 1.7.4, `azure-identity` 1.27.0) | **Verified** — `/api/health` returned `{"status": "ok"}` and invoke returned the `[fake model]` marker |
| `azure_openai` mode with incomplete settings fails at import with `Missing Azure OpenAI settings: ...` | **Verified** locally |
| Fake-mode routing and packaging in CI (`tests/test_examples_smoke.py`, `tests/test_example_model_modes.py`) | **Verified** (no network; not live-model proof) |
| Real Azure OpenAI call, local or deployed (API key or Managed Identity) | **UNVERIFIED** — not executed |
| Azure deployment of this example, role assignment, deployed authenticated invoke, cleanup | **UNVERIFIED** — not executed; commands above were checked only against `az ... --help` |

A fake-model test is not evidence of a real-model cloud deployment. If you run the
live path, label the result with model deployment, plan/runtime, package version,
the (secret-free) successful response, and that cleanup ran.

## CI and credential-free runs

Set `AGENT_MODEL_MODE=fake` deliberately to run offline. Startup emits a warning,
responses retain the `[fake model]` marker, no Azure credentials are required,
and `langchain_openai` is not imported. Set `AGENT_MODEL_MODE=azure_openai` for
live operation; missing endpoint, deployment, or authentication settings fail at
cold start and SDK/authentication/service errors propagate without downgrade.

**Migration:** configurations that previously omitted `AGENT_MODEL_MODE` must
now choose `fake` or `azure_openai`. Existing partial `AZURE_OPENAI_*` settings
no longer select fake mode; choose `fake` explicitly or complete the live setup.
