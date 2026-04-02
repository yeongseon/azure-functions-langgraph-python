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
4. **Health Endpoint** — `GET /api/health` listing registered graphs
5. **Thread Support** — Pass `thread_id` via config for checkpointer-backed conversation state
6. **Error Handling** — Consistent JSON error responses (400, 422, 500)

### Should Have (v0.2)

1. **State Endpoint** — `GET /api/graphs/{name}/threads/{thread_id}/state` for thread state inspection
2. **Azure Table Storage Checkpointer** — Azure-native checkpointer for conversation persistence
3. **Auth Level Configuration** — Per-graph auth level overrides

### Could Have (v0.3+)

1. **LangGraph Platform API Compatibility** — Mirror the LangGraph Platform REST API so `langgraph_sdk` can connect
2. **Durable Functions Integration** — Use Durable Functions for long-running agent executions (>10 min timeout)
3. **OpenAPI Generation** — Integration with `azure-functions-openapi` for auto-generated API docs

## Non-Goals

- Replacing LangGraph — we are a deployment adapter, not a framework
- Building our own graph runtime — we delegate entirely to LangGraph's `invoke()`/`stream()`
- Supporting LangGraph.js — Python only

## Success Metrics

- Zero-boilerplate deployment: user registers a graph and gets HTTP endpoints
- Works with any LangGraph-compatible graph (ReAct, multi-agent, custom)
- Compatible with LangGraph checkpointers for stateful conversations
