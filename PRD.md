# PRD — Azure Functions LangGraph

## Problem

LangGraph is the most popular framework for building stateful AI agents with LLMs. Developers using Azure Functions have no straightforward way to deploy LangGraph agents as serverless HTTP endpoints. The options are:

1. **LangGraph Platform** — LangChain's hosted solution, but it's paid and not Azure-native
2. **Manual wiring** — Write boilerplate to bridge LangGraph's `invoke()`/`stream()` to Azure Functions HTTP handlers
3. **Container deployment** — Deploy LangGraph Server in a container, losing serverless benefits

## Solution

`azure-functions-langgraph` provides a thin integration layer that wraps any compiled LangGraph graph into Azure Functions HTTP endpoints with zero boilerplate.

## Target Users

- Developers already using LangGraph who want to deploy on Azure Functions
- Teams building AI agents on Azure who prefer LangGraph's graph abstraction
- Organizations that need serverless deployment without vendor lock-in to LangGraph Platform

## Core Requirements

### Must Have (v0.1)

1. **Graph Registration** — Accept `CompiledStateGraph` from LangGraph and auto-register HTTP endpoints
2. **Invoke Endpoint** — `POST /api/graphs/{name}/invoke` for synchronous graph execution
3. **Stream Endpoint** — `POST /api/graphs/{name}/stream` for SSE streaming responses
4. **Health Endpoint** — anonymous `GET /api/health` liveness probe (`{"status": "ok"}`) plus protected `GET /api/health/details` for the registered-graph inventory
5. **Thread Support** — Pass `thread_id` via config for checkpointer-backed conversation state
6. **Error Handling** — Consistent JSON error responses (400, 422, 500)

### Should Have (v0.2)

1. **State Endpoint** — `GET /api/graphs/{name}/threads/{thread_id}/state` for thread state inspection
2. **Azure-native persistent storage** — Azure Blob Storage checkpointer and Azure Table Storage thread store for conversation persistence
3. **Auth Level Configuration** — Per-graph auth level overrides

### Could Have (v0.3+) — delivered as experimental, optional surfaces

1. **LangGraph Platform API Compatibility** *(experimental, opt-in)* — Mirror the LangGraph Platform REST API so `langgraph_sdk` can connect. Delivered (v0.3+) but framed as an **optional** surface layered on the Core adapter; see "Product boundary" below.
2. **Durable Functions Integration** — Use Durable Functions for long-running agent executions (>10 min timeout)
3. **OpenAPI Generation** — Integration with `azure-functions-openapi-python` for auto-generated API docs

## Product boundary: Core adapter vs Experimental Platform Compatibility

This package is, first and foremost, **the most natural way to deploy an
already-built LangGraph graph on Azure Functions** — a *deployment adapter*. It
is deliberately **not** a reimplementation of LangGraph Platform on Azure
Functions. To keep that identity crisp, the surface is split into two layers:

- **Core adapter (stable).** Graph registration, `invoke` / `stream` / `state`
  endpoints, health probes, auth, route + endpoint metadata, and checkpointer
  pass-through. This is the supported product and the reason the package exists.
- **Experimental Platform Compatibility (optional, opt-in).** The
  `langgraph_sdk`-compatible thread / run / assistant / state surface under
  `src/azure_functions_langgraph/platform/`. It is a genuinely useful
  convenience for teams that already speak the Platform API, but it is layered
  *on top of* the Core adapter and is not the package's core promise.

**`platform_compat=False` is the default, and that default is the intentional
expression of this boundary** — the Core adapter is always on; the Platform
surface is something you explicitly opt into with `platform_compat=True`.

### Store terminology (three distinct concepts)

These are frequently conflated; they are **not** the same thing:

- **Checkpointer** (e.g. `AzureBlobCheckpointSaver`) — persists *graph execution
  state* (LangGraph checkpoints) so a thread can resume. This is LangGraph's
  `BaseCheckpointSaver` contract.
- **`AzureTableThreadStore`** — a *Platform thread-metadata registry* (thread
  records, status, run locks) backing the experimental Platform Compatibility
  routes. It exists to serve the Platform surface, not graph execution.
- **LangGraph `BaseStore`** — LangGraph's *long-term, cross-thread memory*
  abstraction. **This package does not implement `BaseStore`**; do not confuse
  `AzureTableThreadStore` (Platform thread registry) with it.

## Non-Goals

- Replacing LangGraph — we are a deployment adapter, not a framework
- Building our own graph runtime — we delegate entirely to LangGraph's `invoke()`/`stream()`
- Supporting LangGraph.js — Python only

## Success Metrics

- Zero-boilerplate deployment: user registers a graph and gets HTTP endpoints
- Works with any LangGraph-compatible graph (ReAct, multi-agent, custom)
- Compatible with LangGraph checkpointers for stateful conversations
