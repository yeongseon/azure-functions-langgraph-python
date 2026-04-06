# Architecture

## Overview

`azure-functions-langgraph` is a thin deployment adapter. It bridges LangGraph compiled graphs and Azure Functions HTTP endpoints without adding intermediate abstractions.

```mermaid
flowchart TB
    subgraph User ["User Code"]
        SG["StateGraph → compile()"] --> CSG["CompiledStateGraph"]
    end

    subgraph Core ["LangGraphApp"]
        CSG --> REG["register(graph)"]
        REG --> FA["Azure FunctionApp"]
    end

    subgraph Native ["Native Routes"]
        FA --> INV["POST /graphs/{name}/invoke"]
        FA --> STR["POST /graphs/{name}/stream"]
        FA --> GST["GET /graphs/{name}/threads/{id}/state"]
        FA --> HLT["GET /health"]
    end

    subgraph Platform ["Platform-Compatible Routes"]
        FA --> THR["Thread CRUD + search"]
        FA --> RUN["POST /threads/{id}/runs/wait|stream"]
        FA --> TLR["POST /runs/wait|stream (threadless)"]
        FA --> AST["POST /assistants/search"]
        FA --> PST["State: get / update / history"]
    end

    subgraph Storage ["Persistent Storage (optional)"]
        CKP["Checkpoint Store"]
        THS["Thread Store"]
    end
```

Platform-compatible routes are registered when `platform_compat=True`, enabling the official `langgraph-sdk` Python client to communicate with Azure Functions–hosted graphs.

## Design Objectives

- **Thin adapter, not a framework** — wrap LangGraph, don't replace it. All graph logic stays in LangGraph.
- **Zero boilerplate** — `register()` + `function_app` is the entire API surface.
- **LangGraph conventions first** — input/output contracts follow LangGraph's patterns (messages, config, stream_mode).
- **Azure Functions native** — use the v2 programming model directly, no intermediate web framework.
- **Checkpointer agnostic** — users bring their own checkpointer; config is passed through.

## High-Level Flow

### Invoke

```mermaid
sequenceDiagram
    participant Client
    participant AzureFunctions
    participant Handlers as _handlers.py
    participant Validation as _validation.py
    participant Graph

    Client->>AzureFunctions: POST /api/graphs/agent/invoke
    AzureFunctions->>Handlers: handle_invoke(req, reg)
    Handlers->>Validation: validate_body_size, validate_input_structure
    Validation-->>Handlers: ok
    Handlers->>Handlers: Parse JSON body → InvokeRequest
    Handlers->>Graph: graph.invoke(input, config=config)
    Graph-->>Handlers: result dict
    Handlers->>Handlers: Wrap in InvokeResponse
    Handlers-->>AzureFunctions: HttpResponse (200, JSON)
    AzureFunctions-->>Client: JSON response
```

### Stream (buffered)

```mermaid
sequenceDiagram
    participant Client
    participant AzureFunctions
    participant Handlers as _handlers.py
    participant Graph

    Client->>AzureFunctions: POST /api/graphs/agent/stream
    AzureFunctions->>Handlers: handle_stream(req, reg)
    Handlers->>Handlers: validate_body_size, validate_input_structure
    Handlers->>Handlers: Parse JSON body → StreamRequest
    loop Collect chunks
        Graph-->>Handlers: event dict
        Handlers->>Handlers: Serialize to SSE format
    end
    Handlers->>Handlers: Append "event: end"
    Handlers-->>AzureFunctions: HttpResponse (200, text/event-stream)
    AzureFunctions-->>Client: Buffered SSE response
```

### Platform: Thread Run (SDK-compatible)

```mermaid
sequenceDiagram
    participant SDK as langgraph-sdk Client
    participant AzureFunctions
    participant Routes as platform/routes.py
    participant Store as ThreadStore
    participant Graph

    SDK->>AzureFunctions: POST /api/threads/{id}/runs/wait
    AzureFunctions->>Routes: runs_wait(req)
    Routes->>Routes: Validate thread_id, body, assistant_id
    Routes->>Store: get thread by ID
    Store-->>Routes: Thread record
    Routes->>Routes: Optional assistant bind (first run on thread)
    Routes->>Store: update thread status → busy
    Routes->>Graph: graph.invoke(input, config)
    Graph-->>Routes: result dict
    Routes->>Store: update thread status → idle (or error on failure)
    Routes-->>AzureFunctions: HttpResponse (200, final values JSON + Content-Location header)
    AzureFunctions-->>SDK: JSON response
```

Successful run responses include `Content-Location: /api/threads/{thread_id}/runs/{run_id}`. Threadless runs use `/api/runs/{run_id}`.

## Module Boundaries (key import edges)

```mermaid
flowchart TD
    INIT["__init__.py\nLazy re-exports + __version__"]
    APP["app.py\nLangGraphApp + route wiring"]
    OBR["openapi.py\nBridge to azure-functions-openapi"]
    HDL["_handlers.py\nNative route handlers"]
    VAL["_validation.py\nTransport-agnostic validators"]
    CON["contracts.py\nPydantic + metadata dataclasses"]
    PRO["protocols.py\nProtocol interfaces"]

    subgraph platform ["platform/"]
        PRTE["routes.py\nSDK-compatible handlers"]
        PCON["contracts.py\nPlatform API models"]
        PSTR["stores.py\nThreadStore protocol + InMemoryThreadStore"]
        PSSE["_sse.py\nSSE event formatting"]
    end

    subgraph checkpointers ["checkpointers/"]
        ABLOB["azure_blob.py\nAzureBlobCheckpointSaver"]
    end

    subgraph stores ["stores/"]
        ATABLE["azure_table.py\nAzureTableThreadStore"]
    end

    INIT -.-> APP
    INIT -.-> CON
    INIT -.-> PRO
    APP --> HDL
    APP --> VAL
    APP --> CON
    APP --> PRO
    APP --> PRTE
    APP --> PSTR
    OBR --> APP
    OBR --> CON
    HDL --> VAL
    HDL --> CON
    HDL --> PRO
    PRTE --> PCON
    PRTE --> PSTR
    PRTE --> PSSE
    PRTE --> VAL
    PRTE --> PRO
    ATABLE --> PCON
    ATABLE --> PSTR
    PSTR --> PCON
```

### `app.py`

The core module. Contains:

- `LangGraphApp` — main class (dataclass) that holds graph registrations and builds an `azure.functions.FunctionApp`.
- `_GraphRegistration` — internal record for a registered graph (includes `request_model`/`response_model` since v0.5).
- Route wiring — delegates to `_handlers.py` for native routes and `platform/routes.py` for SDK-compatible routes.
- `get_app_metadata()` — returns an immutable `AppMetadata` snapshot with per-graph route metadata (v0.5+).
- `health()` — health check handler returning registered graph list.


### `openapi.py` *(v0.5+)*

Bridge module between `azure-functions-langgraph` and `azure-functions-openapi`:

- `register_with_openapi(app)` — reads `app.get_app_metadata()` and calls `register_openapi_metadata()` for each route.
- `_validate_model()` — ensures model arguments are Pydantic `BaseModel` subclasses.
- `_build_request_body()` — converts a Pydantic model to an OpenAPI request body dict via `model_json_schema()`.

This module lazily imports `azure-functions-openapi` and raises `ImportError` if the package is not installed.
### `_handlers.py`

Standalone request handlers extracted from `LangGraphApp`:

- `handle_invoke()` — parses request, validates, calls `graph.invoke()`, returns JSON.
- `handle_stream()` — parses request, validates, calls `graph.stream()`, collects chunks into buffered SSE.
- `handle_state()` — retrieves thread state via `graph.get_state()` for `StatefulGraph` instances.
- `_error_response()` — consistent error response builder for native routes.

### `_validation.py`

Transport-agnostic input validators (pure functions returning error message or `None`):

- `validate_graph_name()`, `validate_body_size()`, `validate_input_structure()`, `validate_thread_id()`.
- Shared by both native handlers and platform routes.

### `contracts.py`

Pydantic v2 models for request/response validation:

- `InvokeRequest`, `InvokeResponse`, `StreamRequest` — graph operation models.
- `HealthResponse`, `GraphInfo` — health endpoint models.
- `ErrorResponse` — consistent error format.
- `StateResponse` — thread state values, next steps, metadata, config, timestamps.

Stdlib metadata dataclasses (v0.5+):

- `RouteMetadata` — frozen dataclass describing a single HTTP route (path, method, parameters, models).
- `RegisteredGraphMetadata` — frozen dataclass grouping routes for a registered graph.
- `AppMetadata` — top-level snapshot; `graphs` is `MappingProxyType`, nested parameter dicts are also immutable.

### `protocols.py`

`typing.Protocol` interfaces with `@runtime_checkable`:

- `InvocableGraph` — has `invoke(input, config)`.
- `StreamableGraph` — has `stream(input, config, stream_mode)`.
- `LangGraphLike` — combines both (matches `CompiledStateGraph`).
- `StatefulGraph` — has `get_state(config)`.
- `UpdatableStateGraph` — has `update_state(config, values)`.
- `StateHistoryGraph` — has `get_state_history(config)`.

Using protocols avoids a hard import dependency on `langgraph` at the library level. Any object with the right methods works.

### `platform/`

LangGraph Platform API compatibility layer (v0.3+):

- `routes.py` — SDK-compatible HTTP route handlers for threads, runs, assistants, state operations.
- `contracts.py` — Platform API Pydantic models (Thread, Run, Assistant, Interrupt, etc.).
- `stores.py` — `ThreadStore` protocol + `InMemoryThreadStore` default implementation.
- `_sse.py` — SSE event formatting for platform streaming endpoints.

### `checkpointers/`

Persistent checkpoint storage (v0.4+):

- `azure_blob.py` — `AzureBlobCheckpointSaver` (optional extra: `azure-functions-langgraph[azure-blob]`). Stores checkpoints as blob hierarchies: `{thread_id}/{checkpoint_ns}/{checkpoint_id}/checkpoint.bin`.

### `stores/`

Persistent thread storage (v0.4+):

- `azure_table.py` — `AzureTableThreadStore` (optional extra: `azure-functions-langgraph[azure-table]`). Single-partition design with client-side filtering.

## Public API Boundary

Exported symbols (via `__all__`, all lazy-loaded via `__getattr__`):

- `LangGraphApp` — main class for graph registration and route creation
- `__version__` — package version string
- `InvokeRequest`, `InvokeResponse`, `StreamRequest` — request/response contracts
- `HealthResponse`, `GraphInfo`, `ErrorResponse`, `StateResponse` — endpoint models
- `RouteMetadata`, `RegisteredGraphMetadata`, `AppMetadata` — metadata dataclasses (v0.5+)
- `InvocableGraph`, `StreamableGraph`, `LangGraphLike`, `StatefulGraph` — protocol interfaces

Lazy imports via `__getattr__` are a deliberate design choice: importing the package does not require `azure-functions` or `langgraph` to be installed, enabling use in environments where only contracts or protocols are needed.

Everything else (handlers, validators, platform internals, storage implementations) is implementation detail.

## Key Design Decisions

### Compiled graphs as intended input

Users typically call `.compile()` before registering. Registration enforces only the `InvocableGraph` protocol (requiring `invoke()`), so any object satisfying the protocol works — but compiled graphs are the primary expected input because they carry configured checkpointers and validated graph structure.

### Protocol-based graph acceptance

Rather than importing `CompiledStateGraph` from `langgraph`, the library uses `typing.Protocol`. Registration requires only the `InvocableGraph` protocol (`invoke()`); objects that fail this check are rejected at registration time with `TypeError`. Native stream endpoints additionally require `stream_enabled=True` on the registration and the graph to satisfy `StreamableGraph` (`stream()`). Platform stream routes check only `StreamableGraph` via `isinstance()`. Stream routes return **501 Not Implemented** when the graph does not support the required streaming protocol. This approach means no hard dependency on `langgraph` at import time and testing with mock graphs is straightforward.

### Protocol-based capability detection (v0.4)

`UpdatableStateGraph` and `StateHistoryGraph` protocols enable graceful degradation — graphs without these capabilities return 409 instead of failing. Route handlers use `isinstance()` checks with `@runtime_checkable`.

### Buffered SSE (v0.1)

Azure Functions Python worker does not support true chunked HTTP streaming. All stream events are collected into memory and returned as a single SSE-formatted response. Functional for development but not suitable for long-running streams in production.

**⚠️ User expectation**: The SSE endpoints use "stream" in their path names and return `text/event-stream` content type, but responses are **buffered end-to-end** by the Azure Functions Python worker. Users should expect complete SSE-formatted responses delivered at once, not incremental token-by-token delivery. This is a platform limitation, not a design choice. When Azure Functions Python HTTP streaming stabilises, true incremental delivery will be implemented.

### Platform compatibility layer (v0.3)

When `platform_compat=True`, the library registers routes that mirror the LangGraph Platform REST API, enabling the official `langgraph-sdk` Python client to work with Azure Functions–hosted graphs. This adds thread lifecycle management, SDK-compatible run endpoints, assistant listing, and state operations.

### Azure Blob Storage checkpointer (v0.4)

`AzureBlobCheckpointSaver` provides durable checkpoint persistence across Azure Functions instances. Installed as optional extra (`[azure-blob]`). Synchronous I/O, single-writer assumed.

**⚠️ Concurrency constraint**: The checkpointer assumes a single writer per thread. Concurrent writes to the same thread from multiple Azure Functions instances may corrupt checkpoint data. The write order (values -> metadata -> checkpoint commit marker -> latest hint) is designed for recoverability under single-writer semantics only. If multi-writer support is needed, add blob lease or ETag coordination. For production deployments with multiple instances, ensure serialized access to each thread (e.g., via queue-triggered processing or external locking).

### Azure Table Storage thread store (v0.4)

`AzureTableThreadStore` provides persistent thread metadata across restarts. Installed as optional extra (`[azure-table]`). Single-partition design works well for <100K threads.

**Scale envelope**: The single-partition design works well for up to ~100K threads. At that scale:
- Azure Table Storage throughput limit of ~2,000 entities/sec per partition applies.
- Client-side metadata filtering for `search()` and `count()` becomes progressively expensive as all entities must be scanned.
- Consider migrating to a multi-partition design, Azure Cosmos DB, or a dedicated database when approaching these limits.

### Threadless runs (v0.4)

`POST /runs/wait` and `POST /runs/stream` clone the graph with `checkpointer=None` for truly ephemeral executions. Client-supplied `thread_id` in config is rejected with 422 to prevent semantic confusion.

### Single-writer constraint for thread operations

Thread-assistant binding uses a read-then-write pattern: first run on a thread binds `assistant_id`, and the binding is immutable. This is not atomic; there is an inherent TOCTOU (time-of-check-time-of-use) race between reading and updating.

`InMemoryThreadStore` (single-process) uses an `RLock`, which is sufficient in-process. Durable backends such as `AzureTableThreadStore` can see conflicting first-run bindings under concurrent writes across instances.

Deployments on multi-instance Azure Functions should enforce single-writer-per-thread semantics (for example, queue-based serialization), or explicitly accept last-writer-wins behavior until atomic compare-and-set is added to `ThreadStore`.

### Metadata API and OpenAPI bridge (v0.5)

`LangGraphApp.get_app_metadata()` returns a frozen `AppMetadata` snapshot describing all registered routes. External consumers (e.g. `azure-functions-openapi`) read this instead of reaching into internal state. The bridge module `openapi.py` forwards metadata to `azure-functions-openapi`'s `register_openapi_metadata()` for spec generation. All metadata objects are deeply immutable: `AppMetadata.graphs` is `MappingProxyType`, route parameter dicts are also `MappingProxyType`, and all dataclasses are frozen.

### Per-graph auth override (v0.2)

Each graph registration can override the app-level `auth_level`, enabling mixed-auth deployments.

### Thread ID in request body (native routes)

For native routes, `thread_id` is passed in `config.configurable.thread_id`, not as a URL path parameter. This keeps the native API surface minimal and matches LangGraph's client expectations. Platform-compatible routes (`/threads/{thread_id}/...`) do use path parameters to match the LangGraph Platform REST API.

## Non-Goals

1. **No runtime orchestration** — This package exposes LangGraph graphs as Azure Functions HTTP endpoints. It does not orchestrate graph composition, tool management, or agent logic. Those concerns belong in LangGraph itself.

2. **No validation framework** — Request/response validation beyond transport-level safety checks (body size, input depth/node count, graph name format) belongs in `azure-functions-validation`. This package validates only what is needed for safe HTTP handling.

3. **No OpenAPI ownership** — API documentation and spec generation belong in `azure-functions-openapi`. This package provides metadata for the bridge module but never generates OpenAPI specs itself.

4. **No LangGraph Platform replacement** — This is a deployment adapter, not a competing platform. It mirrors SDK shapes for client compatibility, not to replicate LangGraph Platform functionality.

5. **No custom storage engines** — Storage implementations (`AzureBlobCheckpointSaver`, `AzureTableThreadStore`) are adapters for Azure services. They are not a generic storage framework.

## Related Documents

- [Usage Guide](usage.md)
- [Configuration](configuration.md)
- [API Reference](api.md)
- [Getting Started](getting-started.md)
- [Testing](testing.md)
- [Troubleshooting](troubleshooting.md)

## Sources

- [Azure Functions Python developer reference](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)
- [Azure Functions HTTP trigger](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-http-webhook-trigger)
- [Supported languages in Azure Functions](https://learn.microsoft.com/en-us/azure/azure-functions/supported-languages)
- [Azure Blob Storage documentation](https://learn.microsoft.com/en-us/azure/storage/blobs/)
- [Azure Table Storage documentation](https://learn.microsoft.com/en-us/azure/storage/tables/)
- [LangGraph documentation](https://langchain-ai.github.io/langgraph/)

## See Also

- [azure-functions-validation — Architecture](https://github.com/yeongseon/azure-functions-validation) — Request/response validation pipeline
- [azure-functions-openapi — Architecture](https://github.com/yeongseon/azure-functions-openapi) — OpenAPI spec generation
- [azure-functions-logging — Architecture](https://github.com/yeongseon/azure-functions-logging) — Structured logging with contextvars
- [azure-functions-doctor — Architecture](https://github.com/yeongseon/azure-functions-doctor) — Pre-deploy diagnostic CLI
- [azure-functions-scaffold — Architecture](https://github.com/yeongseon/azure-functions-scaffold) — Project scaffolding CLI
