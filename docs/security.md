# Security

## Authentication and thread ownership

Azure Functions `FUNCTION` keys grant application-level access, not end-user or
tenant ownership of a LangGraph `thread_id`. Keep the default `FUNCTION` level,
then independently authenticate the principal and authorize each resource/action.
Random, unguessable thread IDs reduce enumeration; they are **not authorization**.
Neither checkpoint separation nor distributed locks establish ownership.

The hook receives an immutable `AuthorizationContext` containing the action,
graph name, thread ID (when applicable), and raw `HttpRequest`. Return `True` to
allow, `False` for a uniform 403, or raise `UnauthenticatedError` for 401. Denial
happens before thread lookup on the configured surfaces below, so denied missing
and existing threads are not distinguishable. With **no callback**, those handlers
allow access without an isolation warning. Installing a callback is the
application's responsibility; there is no automatic tenant policy.

### Current route boundaries

| Surface | Resource callback today | Production responsibility |
| --- | --- | --- |
| Native `GET /graphs/{name}/threads/{id}/state` | `STATE_READ`, graph name and thread ID | Verify both graph entitlement and thread ownership |
| Platform thread create/get/update/delete/search/count | Corresponding `THREAD_*` action (`count` uses `THREAD_SEARCH`) | Restrict collection queries as well as individual threads |
| Platform state read/update/history | `STATE_READ`, `STATE_UPDATE`, `HISTORY_READ` | Resolve ownership from trusted storage; updates/history are sensitive too |
| Platform threaded/threadless `runs/wait`, `runs/stream` | `RUN`; threadless ID is `None` | Authorize execution separately from reading; authorize requested assistant |
| Native sync/async invoke and buffered stream | `RUN`, registered graph name and validated thread ID (or `None`); enforced since [#722](https://github.com/yeongseon/azure-functions-langgraph-python/pull/722) closed #687 | Authorize execution independently of state reads, including threadless execution |
| `StreamingLangGraphApp` true streaming | No `ThreadAuthorizer` API | Separate app/transport authorization required; not covered by #687 |
| Durable create/poll/cancel | Host auth only, no resource callback | Protect run ownership and management operations in application/ingress policy |
| Platform assistants search/get/count | Host auth only, no resource callback | Restrict graph inventory and entitlement externally |

Platform callback contexts currently have `graph_name=None`, including RUN.
For runs, the application must safely parse and authorize `assistant_id` from the
request as well as the thread; for state/history, resolve its bound assistant
through trusted policy storage. Collection permission is **not row filtering**:
allowing search/count exposes the store's matching records, not only records
owned by the principal. Deny collection access or supply an appropriately scoped
store/application surface. Thread creation does not automatically record an owner.

### Principal verification and policy composition

App Service Authentication (Easy Auth) can establish identity at a trusted
ingress. Trust its `x-ms-client-principal` headers **only** when direct/untrusted
ingress is blocked and the platform strips/replaces caller-supplied identity
headers. Base64 decoding a header is not authentication. Alternatively, validate
JWT signatures, issuer, audience, expiry and revocation in application middleware.
The library does not validate tokens, establish a principal, or maintain a
revocation cache. The callback receives the original request, not a verified user.

Compose a verified principal with a server-side ownership/entitlement policy:

Native invoke and buffered stream routes evaluate `AuthorizationAction.RUN`
after request/config validation and before acquiring a thread lock or executing
the graph (including checkpoint reads/writes). Both synchronous and asynchronous
graph handlers accept synchronous or asynchronous callbacks. A policy failure
returns a sanitized JSON 401/403; an unexpected callback exception fails closed
with a sanitized 500. Each rejection emits exactly one `on_run_rejected` event
with reason `authorization_denied`, never a run-started event.

`RUN` is independent of `STATE_READ`: a policy that denies state inspection
must also deny `RUN` if the caller must not execute or resume that thread.
The context carries the registered graph name, validated
`config.configurable.thread_id` (or `None` for threadless execution), and the
original request. Policies must explicitly decide whether threadless runs are
permitted; a missing thread ID is not proof of identity. Omitting `authorize`
preserves permissive resource access and provides no per-user isolation.
The separate experimental `StreamingLangGraphApp` does not expose this hook.

```python
from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.authorization import AuthorizationContext, UnauthenticatedError

# Application-owned integrations: validate identity and check trusted policy storage.
from my_application.security import verified_principal, can_access


def authorize(context: AuthorizationContext) -> bool:
    principal = verified_principal(context.request)
    if principal is None:
        raise UnauthenticatedError
    return can_access(principal, context)


app = LangGraphApp(authorize=authorize)
```

This is a composition sketch, not a bundled identity implementation. Evaluate
current credentials on every operation, including after revocation. A state-read
allow does not imply RUN or state-update permission. Validate graph/tool access
and any long-term memory namespaces inside the application as well. Reauthorize
reconnects; admission authorization does not continuously revoke an already
running stream or Durable activity. Never put secrets in policy exceptions.

The negative handler tests use real compiled graphs/checkpoints, fake credentials
and DEBUG log capture. They prove sanitized 401/403 admission denials on configured
routes, including all four native RUN handlers fixed by #687. This is not
evidence that all graph exceptions or operator logs are redacted. Existing
exception diagnostics are intentionally retained: restrict log access, retention
and export, and ensure application nodes/tools never log credentials or state.

### Shared persistence is a trust boundary

Two apps sharing a Blob/Table/Cosmos/Postgres/checkpoint store can access the same
thread state. Enforce a consistent ownership policy in every app and every read,
write, history, run, stream and management entry point. Backend credentials grant
storage access, not per-end-user isolation. Use least-privilege Managed Identity,
private networking, server-owned namespaces and separate stores/accounts where
tenant isolation requires them. Do not accept tenant IDs or owner metadata from
the request as proof of ownership. Backups, Durable history and logs need the same
classification and access controls as live state.

Setting app-level or per-graph auth to `ANONYMOUS` does not authenticate a
principal; explicit warnings remain enabled for both configurations.

## Authentication

By default, `LangGraphApp` creates endpoints with `AuthLevel.FUNCTION`, requiring an Azure Functions key on every request. This is the recommended baseline for production deployments:

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

app = LangGraphApp()  # default: AuthLevel.FUNCTION
```

For local development only, opt in to `ANONYMOUS`. Doing so emits an unconditional
`UserWarning` on app construction; it is not a production isolation mechanism:

```python
app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)  # local development only
```

> The health surfaces are controlled separately. The liveness probe
> `GET /api/health` uses `health_auth_level` (defaults to `ANONYMOUS`, the
> conventional choice for liveness/readiness probes) and exposes only
> `{"status": "ok"}`. The registered-graph inventory lives on
> `GET /api/health/details`, gated by `health_details_auth_level`, which
> defaults to the app-level `auth_level` (`FUNCTION`) so the inventory is
> protected by default.

See [Azure Functions authentication](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-http-webhook-trigger#authorization-keys) for details on function keys and admin keys.

Core Tools does not enforce function keys locally. A local real-host callback
test proves only library admission behavior, never Azure host key enforcement.
Per-graph overrides affect native routes only; Platform and Durable use app auth.

## Local emulators and resource ownership

The Compose files bind published emulator ports to loopback. Run-owned cleanup
and fail-closed host lanes landed in
[#706](https://github.com/yeongseon/azure-functions-langgraph-python/pull/706).
Do not expose development emulators to a LAN or the internet.

That change binds Azurite Blob/Queue/Table to loopback with
`AZURITE_BLOB_PORT`, `AZURITE_QUEUE_PORT`, `AZURITE_TABLE_PORT` overrides
(defaults 10000/10001/10002). Cosmos uses `COSMOS_PORT` (8081) and
`COSMOS_PORT_10250` through `COSMOS_PORT_10255`. Overrides change host port
numbers, not the loopback host binding. Container-side `0.0.0.0` listeners do not
mean host-side public exposure when published ports are loopback-bound.

The Makefile emulator lanes create a unique Compose project and clean up only
that project's resources through exit/signal traps, including startup failures.
Occupied ports fail startup rather than adopting a shared project. Local and CI runs follow the same
ownership model; they must not run global prune or destroy a developer's existing
Compose environment. Hard termination can still leave run-owned resources for
explicit cleanup by project name.

Cosmos emulator TLS uses a **local emulator certificate**. Trust that certificate
through a run-scoped CA bundle for subsequent SDK requests; fetching the known
loopback emulator certificate with a local-only insecure bootstrap is not
production TLS guidance. Never set `verify=False`, `connection_verify=False`, or
disable certificate validation against production Cosmos/Storage endpoints.
Do not reuse emulator keys, certificates, connection strings or CA overrides in
Azure app settings.

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
