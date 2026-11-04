# API Reference

## LangGraphApp

::: azure_functions_langgraph.LangGraphApp
    options:
      show_root_heading: true
      members:
        - register
        - function_app

## Streaming authorization (experimental)

`StreamingLangGraphApp` accepts the keyword-only constructor option
`authorize: StreamingThreadAuthorizer | None = None`. No hook preserves permissive
resource access; Function keys alone do not establish user/thread ownership.
The hook applies to native invoke and true-stream routes, for sync and async
graphs. It receives `RUN` after request/config validation and before graph work,
checkpoint access, lock acquisition or deferred response construction.

Import the transport-specific types from
`azure_functions_langgraph.streaming_authorization`:

| Type / field | Contract |
| --- | --- |
| `StreamingAuthorizationContext` | Frozen context; its repr excludes request/ownership data |
| `action: AuthorizationAction` | `AuthorizationAction.RUN` for both routes |
| `graph_name: str` | Registered graph name |
| `thread_id: str \| None` | Validated client-selected ID, or `None` for threadless execution; not proof of ownership |
| `request: starlette.requests.Request` | Original FastAPI/ASGI request, not a verified principal |
| `StreamingThreadAuthorizer.__call__(context)` | Returns `bool \| Awaitable[bool]`; only `True` admits |

Use the shared `UnauthenticatedError` from `azure_functions_langgraph.authorization`
for 401; `False` produces 403 and other callback exceptions produce sanitized 500.
Sync callbacks must be nonblocking; returned awaitables run on the request loop.
There is one call per admission, not another call during response-body execution.
Revocation affects subsequent requests, not already-admitted streams. Existing
positional constructor arguments and the classic `ThreadAuthorizer` contract are
unchanged; adopting the hook requires an ASGI-aware identity verifier. See
[security and the composition example](security.md#true-stream-authorization).

## Request models

### InvokeRequest

::: azure_functions_langgraph.contracts.InvokeRequest
    options:
      show_root_heading: true

### StreamRequest

::: azure_functions_langgraph.contracts.StreamRequest
    options:
      show_root_heading: true

## Response models

### InvokeResponse

::: azure_functions_langgraph.contracts.InvokeResponse
    options:
      show_root_heading: true

### HealthResponse

::: azure_functions_langgraph.contracts.HealthResponse
    options:
      show_root_heading: true

### ErrorResponse

::: azure_functions_langgraph.contracts.ErrorResponse
    options:
      show_root_heading: true

### GraphInfo

::: azure_functions_langgraph.contracts.GraphInfo
    options:
      show_root_heading: true

## Protocol interfaces

### InvocableGraph

::: azure_functions_langgraph.protocols.InvocableGraph
    options:
      show_root_heading: true

### StreamableGraph

::: azure_functions_langgraph.protocols.StreamableGraph
    options:
      show_root_heading: true

### LangGraphLike

::: azure_functions_langgraph.protocols.LangGraphLike
    options:
      show_root_heading: true

## Thread locks

Pluggable per-thread lock backends for the native `invoke` / `stream`
endpoints. See [Operations & API Surface](ops-and-apis.md#distributed-thread-locks)
for the operational guide (RBAC, lease renewal, production checklist).

### ThreadLock

::: azure_functions_langgraph.locks.ThreadLock
    options:
      show_root_heading: true

### InProcessThreadLock

::: azure_functions_langgraph.locks.InProcessThreadLock
    options:
      show_root_heading: true

### AzureBlobLeaseThreadLock

::: azure_functions_langgraph.locks.AzureBlobLeaseThreadLock
    options:
      show_root_heading: true

## OpenAPI integration

Bridges a `LangGraphApp` into
[azure-functions-openapi-python](https://pypi.org/project/azure-functions-openapi-python/).

### register_with_openapi

Registers all graph and app-level routes with the OpenAPI package and returns the
number of routes registered. Requires the optional dependency
`azure-functions-openapi-python`.

```python
from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.openapi import register_with_openapi

app = LangGraphApp()
# ... app.register(...) your graphs ...
count = register_with_openapi(app)  # -> int (routes registered)
```

::: azure_functions_langgraph.openapi.register_with_openapi
    options:
      show_root_heading: true
