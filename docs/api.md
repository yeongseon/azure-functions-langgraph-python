# API Reference

## LangGraphApp

::: azure_functions_langgraph.LangGraphApp
    options:
      show_root_heading: true
      members:
        - register
        - function_app

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
