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
> example uses a real Azure OpenAI deployment so you can evaluate the package
> against an actual agent. When Azure OpenAI is **not** configured, this graph
> falls back to a deterministic fake model (see [CI note](#ci--credential-free-runs)).

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4+
- Python 3.10+
- An **Azure OpenAI** resource with a **chat model deployment** (e.g. `gpt-4o-mini`)

## Configuration

Copy the settings template and fill in your Azure OpenAI values:

```bash
cd examples/azure_openai_agent
cp local.settings.json.example local.settings.json
```

| Environment variable | Required | Description |
| --- | --- | --- |
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
pip install -r requirements.txt
func start
```

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

## Deploy to Azure

Application code does not change between local and Azure. Follow the canonical
[deployment guide](../../docs/deployment.md), then set the same
`AZURE_OPENAI_*` values as **Function App application settings** (use Managed
Identity in production — do not store the key in App Settings).

> **Production auth:** `LangGraphApp` defaults to `AuthLevel.FUNCTION`, so the
> deployed invoke endpoint requires a function key
> (`?code=<FUNCTION_KEY>` or the `x-functions-key` header).

> **Cost:** this example invokes a **billable** Azure OpenAI deployment. Each
> request to the invoke endpoint consumes tokens against your Azure OpenAI
> quota.

## CI / credential-free runs

When `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` are unset, `graph.py`
builds a deterministic `GenericFakeChatModel` instead of calling Azure OpenAI.
This keeps the example importable and smoke-testable in CI without any cloud
credentials, and the fake path never imports `langchain_openai`. Configure the
`AZURE_OPENAI_*` variables to use a real deployment.
