# Security

## Authentication and thread ownership

Azure Functions `FUNCTION` keys grant application-level access, not end-user or
tenant ownership of a LangGraph `thread_id`. Keep the default `FUNCTION` level,
then independently authenticate the principal and authorize each resource/action.
Random, unguessable thread IDs reduce enumeration; they are **not authorization**.
Neither checkpoint separation nor distributed locks establish ownership.

The classic hook receives an immutable `AuthorizationContext` containing the action,
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
| `StreamingLangGraphApp` native invoke and true stream | Opt-in `StreamingThreadAuthorizer`, `RUN`, registered graph and validated thread ID (or `None`) | Verify the ASGI principal, graph entitlement and thread ownership; see [true-stream authorization](#true-stream-authorization) |
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
The separate experimental `StreamingLangGraphApp` uses the transport-specific
context described below, not the classic `ThreadAuthorizer` request type.

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

### True-stream authorization

Opt in with `StreamingLangGraphApp(authorize=policy)`. The keyword-only argument
defaults to `None`: existing deployments remain permissive at the resource
boundary, without an isolation warning. Function-key access still does **not**
establish ownership. Health and graph-inventory routes are outside this RUN hook.

`StreamingAuthorizationContext` and `StreamingThreadAuthorizer` are imported from
`azure_functions_langgraph.streaming_authorization`. The frozen context carries
`action=AuthorizationAction.RUN`, the registered `graph_name`, validated
`thread_id` (or `None`), and the original `starlette.requests.Request` used by the
FastAPI extension. It is not a verified principal. A forged identity header,
body owner/tenant field or client-selected thread ID must never authenticate a
caller. Apply the [principal verification requirements](#principal-verification-and-policy-composition)
before consulting trusted graph-entitlement and thread-ownership storage.

```python
from azure_functions_langgraph.authorization import UnauthenticatedError
from azure_functions_langgraph.streaming import StreamingLangGraphApp
from azure_functions_langgraph.streaming_authorization import StreamingAuthorizationContext
from my_application.security import verified_asgi_principal, can_run


async def authorize_streaming(context: StreamingAuthorizationContext) -> bool:
    principal = await verified_asgi_principal(context.request)
    if principal is None:
        raise UnauthenticatedError
    return await can_run(principal, context.graph_name, context.thread_id)


app = StreamingLangGraphApp(authorize=authorize_streaming)
```

The application-owned functions above are a composition sketch, not a bundled
identity provider. Explicitly decide whether to permit threadless execution.
Return `True` to admit, `False` for JSON 403, or raise `UnauthenticatedError` for
JSON 401. Unexpected callback exceptions produce sanitized JSON 500 and a generic
error log without exception text or traceback. All these denials use
`detail="Access denied"` and emit one `authorization_denied` observer event with
no thread ID, credentials or ownership token; no graph-start event occurs.
Callbacks must not log credentials themselves. Sync callbacks execute inline and
must be fast/nonblocking. Async callbacks (including returned awaitables) are
awaited on the request loop; use them for bounded identity/policy storage I/O.

Request/config validation (including the client configuration allow-list) runs
first. Policy then runs **once per admission**, before graph/checkpoint I/O, lock
acquisition and construction of the deferred SSE response. Denial is an HTTP
error, never an SSE error inside HTTP 200. An allowed stream still acquires its
lock only when ASGI executes the response or its body is consumed. Abandoning an
unexecuted response acquires nothing. Policy is not replayed during body execution:
revocation applies to subsequent requests/reconnects, not to an already-admitted
response, even if execution has not started. This is admission-only authorization,
not continuous revocation or an atomic ownership transaction with checkpoints.

**Compatibility:** no migration is required when omitting the new keyword.
To adopt it, adapt the identity-verification integration to an ASGI `Request`;
do not pass a callback assuming `func.HttpRequest.get_json()` or `.get_body()`.
ASGI body methods are awaitable. Classic `LangGraphApp` authorization is unchanged.
Durable management and Platform assistant entitlement remain the separate gaps
in the route table. The tests drive real compiled graphs, in-memory checkpoints
and ASGI responses; they are not Functions-host or Azure identity certification.

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
Per-graph overrides affect native routes only; Platform and Durable routes are
shared by every graph and are protected by the app-level `auth_level`.

### Mixed per-graph auth with Platform or Durable routes

A graph registered with an `auth_level` **stricter** than the app-level
`auth_level` (order: `ANONYMOUS` < `FUNCTION` < `ADMIN`) would be reachable
through the Platform (`platform_compat=True`) or Durable (`async_runs="durable"`)
routes without its intended host key. Building the app now fails closed with a
`ValueError` naming the graph and the unsafe configuration:

```python
app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS, platform_compat=True)
app.register(graph, name="agent", auth_level=func.AuthLevel.FUNCTION)
app.function_app  # ValueError: Graph 'agent' requires auth_level=FUNCTION ...
```

Safe options: raise the app-level `auth_level` to at least the strictest graph
level, relax the graph override, or disable the optional routes. Graphs that are
*looser* than the app level, and native-only apps with mixed levels, keep working.

Registration after export is guarded the same way. Once `app.function_app` has
exposed Platform or Durable routes, those handlers keep serving the app's live
graph registry, so `register()` raises `ValueError` **before** mutating the
registry when the new graph's `auth_level` is stricter than the app-level
`auth_level`. Register such graphs before first accessing `app.function_app`, or
raise the app-level `auth_level`. Looser or equal graphs, and native-only apps,
can still register late.

Platform run routes (`/runs/wait`, `/runs/stream`, `/threads/{id}/runs/*`) also
call `authorize` a second time after `assistant_id` is resolved, with
`AuthorizationContext.graph_name` set to the registered graph name, so a callback
can deny per graph before the graph runs. Thread-scoped routes (`/threads/{id}/runs/wait|stream`) perform this graph-aware check before the thread lookup, so a denial is uniform for existing and missing threads and never reveals thread existence. Durable lifecycle routes rely on the host
key only; they do not invoke `authorize` callbacks.

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

Classic native and true-stream invoke/stream share a default-deny configuration
policy: only `config.configurable.thread_id` is client-selectable by default.
Registration may explicitly allow safe configurable names; internal `__*` names
and top-level execution controls remain forbidden. Rejection returns sanitized
HTTP 400 before lock acquisition or graph/observer start, without logging input
values. This does not redact arbitrary graph exception diagnostics or grant
thread ownership. Configure a resource authorizer on either HTTP transport;
identity verification and the ownership policy remain application responsibilities.
See [configuration and the breaking true-stream migration](configuration.md#native-client-configuration-admission).

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
