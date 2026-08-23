# Security

## Authentication

By default, `LangGraphApp` creates endpoints with `AuthLevel.FUNCTION`, requiring an Azure Functions key on every request. This is the recommended baseline for production deployments:

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

app = LangGraphApp()  # default: AuthLevel.FUNCTION
```

For local development or explicitly public surfaces, opt in to `ANONYMOUS`. Doing so emits an unconditional `UserWarning` on app construction:

```python
app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)  # emits UserWarning
```

> The health surfaces are controlled separately. The liveness probe
> `GET /api/health` uses `health_auth_level` (defaults to `ANONYMOUS`, the
> conventional choice for liveness/readiness probes) and exposes only
> `{"status": "ok"}`. The registered-graph inventory lives on
> `GET /api/health/details`, gated by `health_details_auth_level`, which
> defaults to the app-level `auth_level` (`FUNCTION`) so the inventory is
> protected by default.

See [Azure Functions authentication](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-http-webhook-trigger#authorization-keys) for details on function keys and admin keys.

## Input validation

All request bodies are validated using Pydantic v2 models before being passed to the graph. Invalid requests receive a 422 response with validation error details.

## Secret management

This library does not handle secrets (API keys, connection strings, etc.). Use Azure Functions application settings or Azure Key Vault for secret management:

```python
import os

api_key = os.environ["OPENAI_API_KEY"]
```

See [Azure Functions app settings](https://learn.microsoft.com/en-us/azure/azure-functions/functions-how-to-use-azure-function-app-settings) for configuration guidance.

## Checkpoint store security

The checkpointer backends (`AzureBlobCheckpointSaver`, the DB helpers, and the
Cosmos DB helper) persist graph state using LangGraph's default serializer,
which can restore **arbitrary Python types** from a checkpoint payload.
Treat the checkpoint store as part of your threat model: a payload written by a
compromised or untrusted party can execute code paths during deserialization.
Checkpoint blobs are **not** trusted-free data.

Hardening guidance for production:

- **Restrict storage access.** Use RBAC / Managed Identity (see the persistent
  storage examples in the README) and private endpoints so only the Function
  App identity can read or write checkpoints. Never expose the checkpoint
  container/table/database publicly.
- **Enable strict deserialization.** LangGraph exposes
  `LANGGRAPH_STRICT_MSGPACK` (and an allowed-modules list) to restrict which
  types may be reconstructed from checkpoint payloads. Set
  `LANGGRAPH_STRICT_MSGPACK=true` (plus any allowed modules your graphs
  genuinely need) where your workload permits it. The upstream default is
  permissive (`false`).
- **Set it before import.** The env var is read at **import time**, so it must
  be present in the process environment *before* LangGraph is imported — set it
  in Azure Functions **application settings** (not at runtime inside a handler)
  so it takes effect on cold start. See the import-order caveat in
  [langchain-ai/langgraph#7847](https://github.com/langchain-ai/langgraph/issues/7847).

See the upstream security advisory
[GHSA-g48c-2wqr-h844](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-g48c-2wqr-h844)
for the full rationale and recommendations.

## Dependency security

The project uses:

- **Bandit** for static security analysis of Python code
- **Dependabot** for automated dependency updates
- **CodeQL** for code scanning via GitHub Actions

Run the security scan locally:

```bash
make security
```

## Reporting vulnerabilities

If you discover a security vulnerability, please report it responsibly. See [SECURITY.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/SECURITY.md) for reporting instructions.

Do not open a public GitHub issue for security vulnerabilities.
