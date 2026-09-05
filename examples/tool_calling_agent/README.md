# Tool-Calling Agent Example

**Continue here after the [Azure OpenAI agent](../azure_openai_agent/) and
[conversation memory](../conversation_memory/) examples.** Those examples show a
real Azure OpenAI agent and how to give it memory. This example adds the next
capability real agents need: **calling tools**.

The agent decides — per request — whether a question needs a tool. If it does,
LangGraph routes to a `ToolNode` that executes the chosen tool from
[`tools.py`](tools.py), feeds the result back to the agent, and the agent phrases
the final answer. If it doesn't, the agent just answers directly.

```text
HTTP request
  -> agent node        (Azure OpenAI decides: tool or direct answer?)
  -> tools node        (ToolNode runs the chosen tool from tools.py)
  -> agent node        (reads the tool result, writes the final answer)
  -> HTTP response
```

## The tools are YOUR code, not the adapter's

This is the point of the example. `azure-functions-langgraph` **does not own
tools, models, or the agent loop** — it only exposes your compiled graph over
HTTP. The tools live in [`tools.py`](tools.py) because that is where **your**
business logic goes:

```python
# tools.py — demo tools; replace the bodies with real work
@tool
def lookup_order(order_id: str) -> str:
    """Look up the delivery status of a customer order by its ID."""
    ...

TOOLS = [lookup_order, get_weather]
```

The demo tools are deterministic, local, and dependency-free (an in-memory dict)
so the example — and CI — runs with **zero external SaaS APIs**. Azure OpenAI is
the only live service dependency, and only in the real-model path.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4+
- Python 3.10+
- *(Optional)* an **Azure OpenAI** resource with a chat deployment. Without one,
  the example runs against a deterministic fake model that emits one known tool
  call followed by a final answer — perfect for trying the tool loop locally
  with zero credentials.

## Run locally

```bash
cd examples/tool_calling_agent
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

To use a real Azure OpenAI deployment, fill in the `AZURE_OPENAI_*` values in
`local.settings.json` (same variables and auth paths as the
[`azure_openai_agent`](../azure_openai_agent/#configuration) example).

## Two requests: one needs a tool, one doesn't

**Request A — no tool needed** (the agent just answers):

```bash
curl -s -X POST http://localhost:7071/api/graphs/tool_calling_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Say hello in one sentence."}]}}'
```

With a real deployment the agent answers directly — no tool is invoked, so no
`ToolMessage` appears in the returned `messages`.

**Request B — needs a tool** (the agent calls `lookup_order`):

```bash
curl -s -X POST http://localhost:7071/api/graphs/tool_calling_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "What is the status of order A1001?"}]}}'
```

Now the returned `messages` include the full loop: an `AIMessage` with
`tool_calls`, a `ToolMessage` carrying the tool's output
(`"Order A1001: shipped via Contoso Express, ETA 2 days."`), and a final
`AIMessage` phrased from that result.

> With the **fake model** (no credentials), every request drives the scripted
> loop — one `lookup_order("A1001")` call, then a canned final answer — so you
> can observe the tool actually firing without Azure OpenAI.

## Observing the tool invocation

The tool invocation is visible two ways:

1. **In the response** — Request B's `messages` array contains a `ToolMessage`
   whose `content` is exactly what `lookup_order` returned. That is proof the
   tool ran inside the graph, not the model guessing.
2. **In the host logs** — `func start` prints each node as it executes; add a
   `print(...)`/`logging` call inside a tool body to log every invocation with
   its arguments.

## Replace the demo tools

The demo tools use an in-memory dict. **This is exactly where your own
integration goes.** Swap the body of a tool for real work:

- a **REST API** call (`httpx`/`requests`),
- a cloud/database **SDK** (Azure SDK, `azure-cosmos`, `psycopg`, ...),
- a **database** query,
- or even **another Azure Function** (see below).

Keep the `@tool` decorator and a clear docstring — the model reads the docstring
and type hints to decide when and how to call the tool.

### Production notes

Real tools reach external systems, so treat them like any outbound dependency:

- **Timeouts** — set explicit client timeouts; a hung tool hangs the whole
  request (and burns Azure Functions execution time).
- **Retries** — retry transient failures with backoff, but bound the attempts so
  a slow dependency doesn't exceed the Functions timeout.
- **Idempotency** — the agent may call a tool more than once in a loop; make
  writes idempotent (or guard them) so repeats are safe.

## Optional: a tool that calls another Azure Function

You do **not** need this for the primary example — it is deliberately kept as a
single-app demo to minimize setup friction. But a tool is just Python, so it can
call another Azure Functions HTTP endpoint:

```python
import os
import httpx
from langchain_core.tools import tool

@tool
def lookup_order(order_id: str) -> str:
    """Look up an order via a separate 'orders' Azure Function."""
    resp = httpx.get(
        f"{os.environ['ORDERS_FUNCTION_URL']}/api/orders/{order_id}",
        headers={"x-functions-key": os.environ["ORDERS_FUNCTION_KEY"]},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.text
```

Authenticate the cross-Function call with a **function key** (shown above) or,
preferably in production, the calling app's **Managed Identity** against an
Entra-protected target. This is a recipe, not a second implementation — the
graph wiring is unchanged.

## OpenAPI / Swagger UI

`function_app.py` wires the OpenAPI bridge (issue #411):

```python
from azure_functions_langgraph.openapi import register_with_openapi

count = register_with_openapi(langgraph_app)  # forwards route metadata
```

`register_with_openapi` reads `LangGraphApp.get_app_metadata()` and forwards each
route (request/response models, summary, status codes) to
`azure-functions-openapi-python`, so `tool_calling_agent`'s invoke/stream/state
endpoints appear in the generated OpenAPI spec and are Swagger-testable. Exposing
`/openapi.json` or Swagger UI is that package's responsibility — follow its setup
and point it at the same `langgraph_app.function_app`. See the
[`openapi_bridge`](../openapi_bridge/) example for the bridge in isolation.

> **`azure-functions-openapi-python` is an example-only dependency.** It lives in
> this example's [`requirements.txt`](requirements.txt), never in the base
> `azure-functions-langgraph` install, and the import stays behind
> `register_with_openapi`'s `ImportError` guard. The bridge requires
> `azure-functions-openapi-python >= 0.16.0`.

## Deploy to Azure

Application code does not change between local and Azure. Follow the canonical
[deployment guide](../../docs/deployment.md).

> **Production auth:** `LangGraphApp` defaults to `AuthLevel.FUNCTION`, so the
> deployed endpoints require a function key (`?code=<FUNCTION_KEY>` or the
> `x-functions-key` header).

## CI / credential-free runs

Unless Azure OpenAI is **fully** configured — endpoint, deployment, **and** an
auth method (`AZURE_OPENAI_API_KEY`, or `AZURE_OPENAI_USE_ENTRA_ID=true`) —
`graph.py` builds a deterministic scripted `GenericFakeChatModel` that emits one
known `lookup_order` tool call followed by a final answer. This keeps the full
agent→tool→agent loop importable and smoke-testable in CI without any cloud
credentials, and the fake path never imports `langchain_openai`.
