# DESIGN — Azure Functions LangGraph

## Design Principles

1. **Thin Adapter, Not a Framework** — We wrap LangGraph, not replace it. All graph logic stays in LangGraph.
2. **Zero Boilerplate** — `register()` + `function_app` is the entire API surface.
3. **LangGraph Conventions First** — Input/output contracts follow LangGraph's patterns (messages, config, stream_mode).
4. **Azure Functions Native** — Use the v2 programming model directly, no intermediate web framework.
5. **Checkpointer Agnostic** — Users bring their own checkpointer; we pass config through.

## Architecture

```
User Code                     azure-functions-langgraph           Azure Functions
─────────────────────────────────────────────────────────────────────────────────
StateGraph → compile()  →  LangGraphApp.register(graph)  →  FunctionApp with routes
                            ├─ POST /graphs/{name}/invoke  →  graph.invoke(input, config)
                            ├─ POST /graphs/{name}/stream  →  graph.stream(input, config)
                            ├─ GET /graphs/{name}/threads/{id}/state → graph.get_state(config)
                            ├─ GET /health                 →  list registered graphs
                            └─ GET /openapi.json           →  auto-generated spec
```

## Key Decisions

### 1. Accept `CompiledStateGraph` only (not `StateGraph`)
Users must call `.compile()` themselves. This ensures:
- Checkpointer is configured before registration
- Graph validation happens at user code level
- We don't need to know about graph compilation options

### 2. SSE streaming as buffered response (v0.1)
Azure Functions doesn't natively support true SSE streaming (no chunked transfer encoding in the Python worker). In v0.1, we buffer all stream events and return them as a single SSE-formatted response. This is functional but not truly streaming.

Future versions may use Durable Functions fan-out or WebSocket support to enable true streaming.

### 3. Thread ID in request body config (not URL path)
Following LangGraph conventions, `thread_id` is passed in `config.configurable.thread_id`, not as a URL path parameter. This keeps the API surface minimal and compatible with LangGraph's native client patterns.

### 4. No Durable Functions dependency (v0.1)
The v0.1 release is HTTP-only. This keeps the dependency footprint small and the mental model simple. Durable Functions can be added later for timeout extension and fan-out patterns.

### 5. Per-graph auth override (v0.2)
Each graph registration can override the app-level `auth_level`. This enables mixed-auth deployments where public-facing graphs use `ANONYMOUS` while admin graphs require `FUNCTION` keys. The override is stored per-registration and applied at route creation time.

### 6. State endpoint via StatefulGraph protocol (v0.2)
Graphs that implement `get_state(config)` (i.e., graphs compiled with a checkpointer) expose a `GET /graphs/{name}/threads/{thread_id}/state` endpoint. This uses a new `StatefulGraph` protocol added to `protocols.py`, keeping the protocol-based design consistent.

## Module Structure

```
src/azure_functions_langgraph/
├── __init__.py       # Package init, lazy imports, __version__
├── app.py            # LangGraphApp class, route registration, request handlers
├── contracts.py      # Pydantic request/response models (InvokeRequest, StreamRequest, InvokeResponse, StateResponse, etc.)
├── protocols.py      # Protocol interfaces (InvocableGraph, StreamableGraph, StatefulGraph, LangGraphLike)
└── py.typed          # PEP 561 marker
```

## Testing Strategy

- Unit tests use `FakeCompiledGraph` and `FakeStatefulGraph` mock objects
- No real LLM calls in unit tests
- 105 tests, 98%+ coverage as of v0.2.0
- Integration tests (future) would use LangGraph with mock tools
- E2E tests (future) deploy to Azure Functions and hit real endpoints
