# Adapter reference and migration guide

> Rolling 0.x: stable core HTTP contracts, Beta storage/locking, and explicitly experimental optional transports. See the canonical [README](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/README.md).

> Part of the **Azure Functions Python DX Toolkit** — dogfood-tested by [azure-functions-cookbook-python](https://github.com/yeongseon/azure-functions-cookbook-python).


[![PyPI](https://img.shields.io/pypi/v/azure-functions-langgraph.svg)](https://pypi.org/project/azure-functions-langgraph/)
[![Downloads](https://static.pepy.tech/badge/azure-functions-langgraph/month)](https://pepy.tech/project/azure-functions-langgraph)
[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/azure-functions-langgraph/)
[![CI](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml)
[![Release](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/publish-pypi.yml)
[![Security Scans](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml)
[![codecov](https://codecov.io/gh/yeongseon/azure-functions-langgraph-python/branch/main/graph/badge.svg)](https://codecov.io/gh/yeongseon/azure-functions-langgraph-python)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://pre-commit.com/)
[![Docs](https://img.shields.io/badge/docs-yeongseon.dev-blue)](https://yeongseon.dev/azure-functions-python/langgraph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)

Read this in: [한국어](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/README.ko.md) | [日本語](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/README.ja.md) | [简体中文](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/README.zh-CN.md)

> **Stability — rolling 0.x, per surface.** The package is classified Beta; optional transports remain Experimental. The canonical [README stability table](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/README.md#stability--rolling-0x-per-surface) defines each surface's contract.
>
> | Surface | Tier | What it means |
> | --- | --- | --- |
> | Core — `LangGraphApp` construction, native invoke / stream / state HTTP endpoints, auth levels | **Stable** | API frozen; a breaking change warrants a major bump |
> | Checkpoint backends, thread locking, native async runtime (`ainvoke`/`astream`), `version="v2"` pass-through, `RunObserver` contract | **Beta** | Shape settled; may be refined before v1.0 |
> | LangGraph Platform compatibility, true HTTP streaming, Azure Service Bus trigger, Durable async run lifecycle | **Experimental** | May change or be removed between minor releases |

Deploy [LangGraph](https://github.com/langchain-ai/langgraph) graphs as **Azure Functions** HTTP endpoints with minimal boilerplate.

---

Part of the **Azure Functions Python DX Toolkit**

## Why this exists

Deploying LangGraph on Azure Functions is harder than it should be.

- LangGraph does not provide an Azure Functions-native deployment adapter
- Exposing compiled graphs as HTTP endpoints requires repetitive wiring
- Teams often rebuild the same invoke/stream wrapper for every project

This package provides a focused adapter for serving LangGraph graphs on Azure Functions Python v2.

## What it does

- **Zero-boilerplate deployment** — register a compiled graph, get HTTP endpoints automatically
- **Invoke endpoint** — `POST /api/graphs/{name}/invoke` for synchronous execution
- **Stream endpoint** — `POST /api/graphs/{name}/stream` for buffered SSE responses (opt into true incremental streaming with `StreamingLangGraphApp`)
- **Health endpoints** — anonymous `GET /api/health` liveness probe (`{"status": "ok"}`, no graph inventory) plus protected `GET /api/health/details` listing registered graphs with checkpointer status
- **Checkpointer pass-through** — thread-based conversation state works via LangGraph's native config
- **State endpoint** — `GET /api/graphs/{name}/threads/{thread_id}/state` for thread state inspection (when supported)
- **Per-graph auth** — override app-level auth with `register(..., auth_level=...)`
- **LangGraph Platform API compatibility** — SDK-compatible endpoints for threads, runs, assistants, and state (v0.3+)
- **Persistent storage backends** — Azure Blob Storage checkpointer and Azure Table Storage thread store (v0.4+)

## LangGraph Platform comparison

| Feature | LangGraph Platform | azure-functions-langgraph |
|---------|-------------------|--------------------------|
| Hosting | LangChain Cloud (paid) | Your Azure subscription |
| Assistants | Built-in | SDK-compatible API (v0.3+) |
| Thread lifecycle | Built-in | Create, get, update, delete, search, count (v0.3+) |
| Runs | Built-in | Threaded + threadless runs (v0.4+) |
| State read/update | Built-in | get_state + update_state (v0.4+) |
| State history | Built-in | Checkpoint history with filtering (v0.4+) |
| Streaming | True SSE | Buffered SSE (opt-in true SSE via `StreamingLangGraphApp`) |
| Persistent storage | Built-in | Azure Blob + Table Storage (v0.4+) |
| Infrastructure | Managed | Azure Functions (serverless) |
| Cost model | Per-seat/usage | Azure Functions pricing |

> See [COMPATIBILITY.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/COMPATIBILITY.md) for the per-feature SDK support matrix, including which `RunCreate` fields, thread filters, and SDK calls return `501 Not Implemented`.

### SDK adoption checklist

Choose the [Platform SDK example](../examples/platform_compat_sdk/) only when you
need SDK request/response shapes. Native `LangGraphApp()` remains the default
for new clients. This optional surface is **Experimental**, not Platform parity.
Start with the exact [SDK feature support matrix](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/COMPATIBILITY.md#sdk-feature-support-matrix):
assistant discovery, thread metadata CRUD/search/count, blocking wait, buffered
stream and state read/update/history each have their own limits. The example's
client demonstrates discovery, thread creation, `runs.wait` and state retrieval;
it is not evidence that every SDK call is available.

#### Persistence and hosting topology

The SDK example uses an in-memory checkpointer; Platform thread records default
to `InMemoryThreadStore`, and `thread_lock` defaults to `InProcessThreadLock`.
These are process-local: checkpoints and thread records disappear on process
restart, and one Azure instance can run multiple Python workers. Choose three
things independently before deployment:

1. A persistent checkpointer compiled into the graph for conversation state.
2. A shared Platform `ThreadStore`, assigned to `app.thread_store` **before**
   exporting `app.function_app`, for thread metadata, status and Platform run locks.
   The [Managed Identity storage recipe](../examples/managed_identity_storage/)
   wires Blob checkpoints and Azure Table records explicitly.
3. A distributed `ThreadLock` for shared checkpoint writers, including mixed
   native/Platform writers. A Table run lock alone is not mixed-surface exclusion.
   Use the [distributed deployment decision table](production-guide.md#distributed-deployment)
   and [lock limitations](production-guide.md#distributed-thread-locking), rather
   than treating persistent storage as a concurrency guarantee.

No backend is selected automatically. Build-time
[topology validation](production-guide.md#topology-validation-880) rejects declared
distributed configurations that retain incompatible process-local defaults:
`AZFUNC_LANGGRAPH_LOCK_BACKEND=distributed` requires a supplied distributed lock;
either that declaration or `AZFUNC_LANGGRAPH_THREAD_STORE=distributed` rejects an
in-memory Platform store or detected `InMemorySaver` checkpointer. Without an
explicit distributed declaration, Azure-host detection can warn instead of
rejecting local defaults. These checks do not prove the actual instance/worker
count or certify a deployment's scale-out safety (the remaining verification
scope is tracked in [#880](https://github.com/yeongseon/azure-functions-langgraph-python/issues/880)).

#### Checkpoint retention and thread deletion

`threads.delete` deletes only the `ThreadStore` record (metadata, status and
Platform lock state); it does **not** delete graph checkpoints. Select an explicit,
application-owned retention policy for the checkpointer independently of thread
record retention. For Blob storage, `AzureBlobCheckpointSaver.delete_thread(thread_id)`
removes a whole thread's checkpoint data; `delete_old_checkpoints` and
`delete_checkpoints_before` support partial pruning. See the
[checkpoint retention recipe and orphan-value cleanup](production-guide.md#checkpoint-retention)
for the helpers' limits and scheduling.

Keep the IDs needed for cleanup before removing thread records, or schedule
checkpoint cleanup separately: a job that enumerates only surviving thread
records will miss deleted threads. Coordinate cleanup with writers; deleting a
metadata record is neither cancellation of graph execution nor a data-erasure
workflow for the checkpoint store.

#### Search and scale

Thread metadata filters run **in-process**, for both `InMemoryThreadStore` and
`AzureTableThreadStore`. Table scan candidates can be narrowed by status, but
metadata is matched after fetching candidates, with sorting and pagination
after filtering. A small `limit` is not a storage-read budget.

The Table status index is opt-in and off by default. Only an explicit `idle`,
`interrupted` or `error` search with **no metadata filter**, a `ready` marker and
an offset within `status_index_offset_cap` can use it. Unfiltered search,
metadata-filtered search, `status="busy"` and **all counts** still scan; deep
offsets and unavailable/not-ready index state also fall back to scanning.
Review [index rollout, mixed-writer hazards and costs](#table-status-index-opt-in-experimental)
and the remaining measurement/operational work in
[#801](https://github.com/yeongseon/azure-functions-langgraph-python/issues/801)
before relying on it for scale. This is not a general metadata or global search index.

#### SDK lifecycle and transport

`runs.wait` executes while the HTTP request is open; `runs.stream` returns
**buffered SSE** after graph execution, not incremental tokens. Threadless calls
require a graph that can disable checkpointing per call, and streaming requires
the graph's streaming capability; the matrix specifies their `501` boundaries.

The Platform routes do not expose `runs.create` (fire-and-forget), `runs.get`,
`runs.list`, `runs.cancel`, `runs.delete`, `runs.join` or `runs.join_stream`.
SDK requests to those unregistered paths receive HTTP **404 Not Found**, not a
run handle, persisted run status or the explicit `501` JSON feature errors
returned for unsupported fields on **registered** routes. SDK error/exception
wrapping depends on the client version and request method; do not interpret a
404 as successful cancellation or as proof that no graph work happened.

True incremental SSE uses the separate native
[`StreamingLangGraphApp` transport](#streaming-behavior), not the Platform SDK
stream routes. Enabling the separate native Durable lifecycle likewise does not
implement SDK lifecycle calls; see the
[non-SDK distinction in the compatibility matrix](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/COMPATIBILITY.md#durable-async-run-lifecycle-experimental-non-sdk).

## Scope

- Azure Functions Python **v2 programming model**
- LangGraph graph deployment and HTTP exposure
- LangGraph runtime concerns: invoke, stream, threads, runs, and state
- Optional integration points for validation and OpenAPI via companion packages

This package is a **deployment adapter** — it wraps LangGraph, it does not replace it.

> Internally, graph registration remains protocol-based (`LangGraphLike`), so any object satisfying the protocol works — but the package's documentation and examples focus on LangGraph use cases.

## What this package does not do

This package does not own:
- OpenAPI document generation or Swagger UI — use [`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python)
- Request/response validation beyond LangGraph contracts — use [`azure-functions-validation-python`](https://github.com/yeongseon/azure-functions-validation-python)
- Generic graph-serving abstractions beyond LangGraph

> **Note:** For OpenAPI spec generation, use the [`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python) package with the bridge module (`azure_functions_langgraph.openapi.register_with_openapi`).

### OpenAPI companion compatibility policy

The optional `openapi` extra permits `azure-functions-openapi>=0.30,<1`.
The distribution name is `azure-functions-openapi`; the import name is
`azure_functions_openapi`. The bridge remains deprecated and retained for
documentation metadata; this policy covers `register_with_openapi(app)`, not
every companion feature or automatic endpoint-metadata scanning.

#### Published-range evidence (2026-11-25)

The [PyPI JSON index](https://pypi.org/pypi/azure-functions-openapi/json) listed
exactly these non-yanked stable releases in the advertised range. `>=0.30`
means the actual floor is **0.30.0**, not the newest 0.30 patch. Each release
declares Python `>=3.11,<3.15`.

| Companion | First upload (UTC) | Python 3.11 focused checks |
| --- | --- | --- |
| 0.30.0 (floor) | 2026-10-11 08:49:06 | 57 passed, 0 failed, 0 skipped |
| 0.30.1 (intermediate patch) | 2026-10-14 00:21:03 | 57 passed, 0 failed, 0 skipped |
| 0.31.0 (latest eligible) | 2026-11-01 08:05:18 | 57 passed, 0 failed, 0 skipped |

Each throwaway environment installed the candidate package from the checkout
plus an exact companion pin from PyPI, without modifying shared Hatch
environments. Floor and latest used separate fresh virtualenvs; the intermediate
patch was also checked in the floor environment before restoring its floor pin.
All three used the same focused selection: `test_openapi_bridge.py`,
`test_openapi_real_companion_interop.py`, `test_endpoint_metadata.py`, and
`test_endpoint_conformance.py` under `tests/`. The eight real-companion cases
executed, rather than silently skipping. These are local in-process integration
checks, not Functions-host or Azure certification, and not a Python 3.12–3.14
or minimum-transitive-dependency matrix.

All three resolved the companion's runtime dependencies to `azure-functions
1.26.0`, `pydantic 2.14.4`, `pydantic-core 2.50.5`, and `PyYAML 6.0.4`;
the graph stack resolved to `langgraph 1.2.24`, `langchain-core 1.8.4`,
`langgraph-checkpoint 4.3.2`, and `langgraph-sdk 0.4.14`. The companion metadata
requires `pydantic>=2,<3`, unbounded `pyyaml`, and `azure-functions>=1.21,<2`
below Python 3.13 (`>=1.21,<3` on Python 3.13+).

For comparison, the shared Table test environment had **no companion**:
49 checks passed and the real-companion module skipped explicitly. Its versions
were `azure-functions 1.26.0`, `pydantic 2.14.3`, `pydantic-core 2.50.4`,
`PyYAML 6.0.4`, `langgraph 1.2.22`, `langchain-core 1.8.1`,
`langgraph-checkpoint 4.3.2`, and `langgraph-sdk 0.4.14`. That comparison proves
the independent absent-package lane remains usable; a skip is not companion
certification. `TestImportGuard` and the optional-package-absent example test
remain independent of real-companion installation.

#### Emitted native-route semantics and known limitations

After registering one default stateful graph named `agent`, calling
`register_with_openapi(app)` and then the companion's `generate_openapi_spec()`
emitted the following **same** OpenAPI 3.1 document semantics on all three
releases (bridge-only generation, without `app=` scanning):

| Emitted path | Method | Emitted successful response |
| --- | --- | --- |
| `/api/graphs/agent/invoke` | POST | `200 application/json`, component schema with required `output` object envelope |
| `/api/graphs/agent/stream` | POST | `200 application/json`, generic object schema |
| `/api/graphs/agent/threads/{thread_id}/state` | GET | `200 application/json`, generic object schema; required string `thread_id` path parameter |
| `/api/health` | GET | `200 application/json`, generic object schema |
| `/api/health/details` | GET | `200 application/json`, generic object schema |

The generated paths preserve a matching custom `v1` or empty route prefix
without doubling it. The tests assert exact paths/methods, the state parameter,
and the invoke response's component reference and `output` envelope.

**Findings, not wire-fidelity certification:** the bridge-only stream response
above does **not** describe the runtime's buffered `text/event-stream` SSE.
State and health bodies are generic, and error responses are absent. Both
invoke and stream request bodies also contain an extra Request Body Object
inside `requestBody.content.application/json.schema` (its own `required` and
`content.application/json.schema`), rather than the request schema directly.
Registration success therefore does not imply a fully accurate document or
safe client generation. These limitations occur at both ends of the range;
narrowing to another tested release would not fix them. They require separate
bridge/schema work, not a dependency downgrade or weaker tests.

#### Maintaining the range

Retain `>=0.30,<1`: all currently published eligible releases pass the bridge
checks and preserve the recorded semantics; there is no observed
version-specific regression justifying a higher floor or lower ceiling.
This is evidence for the versions above, **not** a guarantee that every future
pre-1.0 minor will be compatible.

The maintainer reviewing a companion update or a user compatibility report
owns re-running the same selection in fresh floor and latest-eligible
environments. Check the PyPI release list and Python requirements, record
resolved versions and generated paths/responses, and inspect `-rs`: any skipped
interop case leaves that candidate uncertified. Keep the primary environment
without the companion to exercise optional-dependency absence independently.
The existing optional OpenAPI test entry point alone is not a two-ended matrix.

If a new release breaks registration, parameter conversion, generation, paths
or response semantics, report the exact failing version, dependencies, command
and failure. Users should pin their application lock/constraints to the last
verified exact companion version while the maintainer reproduces it. The
maintainer must then either adapt the bridge with a regression test or publish
a dependency-bound correction excluding the incompatible release (`!=` for an
isolated bad release, or an upper bound at the first incompatible line). Do not
raise the floor merely to hide a latest-release failure. Re-test floor and
latest within the corrected range, document migration impact, and widen again
only after the formerly failing release or its replacement passes. Never
convert a failure to a skip/xfail to preserve a compatibility claim.

### Endpoint metadata v1 producer certification

Native handlers still emit endpoint metadata **v1**. A metadata v2 producer
would be needed to describe response media types or headers not representable
by v1 (for example buffered SSE), not merely because a consumer supports v2
or a graph uses LangGraph's separate `version="v2"` output option. The core
package remains installable and importable without OpenAPI; only optional
consumer tests and bridge calls need the companion.

With `generate_openapi_spec(app=app.function_app)`, the tested consumers log a
warning for an unsupported endpoint metadata version and fall back to a
binding-derived operation without its endpoint schema hints. Treat that warning
as a compatibility failure, not successful schema certification. This signal
belongs to the consumer; no producer runtime warning or v2 emission is required.
Bridge-only generation does not inspect endpoint versions. Combining the full
deprecated bridge with app scanning currently raises `OpenAPISpecConfigError`
for conflicting request metadata; use native scanning for shapes and separate
documentation-only registration for custom prose.

The recorded fixture in `tests/fixtures/openapi_native_producer.v1.json` is
checked by `tests/test_openapi_v1_producer_consumers.py`. To update it, build
the test's producer app and capture its handler endpoint namespaces, retaining
the route-to-payload mapping and custom documentation field. Review schema
changes rather than blindly refreshing expectations, then run that module and
the existing conformance/real-companion modules with `--no-cov -q -rs` in fresh
exact-pin 0.30.0 and 0.31.0 environments; every consumer case must execute.
The local vendored `endpoint_metadata_schema.v1.json` is not proof of an
immutable Validation-package schema: that upstream artifact/ref or digest must
be identified before claiming cross-producer schema certification. Fixture
maintenance adds no mandatory runtime dependency and does not require v2.

## Installation

```bash
pip install azure-functions-langgraph
```

For persistent storage with Azure services:

```bash
# Azure Blob Storage checkpointer
pip install azure-functions-langgraph[azure-blob]

# Azure Table Storage thread store
pip install azure-functions-langgraph[azure-table]

# Both
pip install azure-functions-langgraph[azure-blob,azure-table]
```

For database or Cosmos DB checkpointer backends:

```bash
# Postgres checkpointer
pip install azure-functions-langgraph[postgres]

# SQLite checkpointer (local dev)
pip install azure-functions-langgraph[sqlite]

# Cosmos DB checkpointer
pip install azure-functions-langgraph[cosmos]
```

Your Azure Functions app should also include:

```text
azure-functions
langgraph
azure-functions-langgraph
```

For local development:

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
cd azure-functions-langgraph
pip install -e .[dev]
```

## Quick Start

> **New to this package?** Follow the [5-minute tutorial](tutorial-5-min.md) for a smoke-tested walkthrough.

### See it working

The shipped `simple_agent` registers these routes:

```text
GET /api/graphs/simple_agent/threads/{thread_id}/state
GET /api/health
GET /api/health/details
POST /api/graphs/simple_agent/invoke
POST /api/graphs/simple_agent/stream
```

```console
$ curl -s http://localhost:7071/api/health
{"status":"ok"}
$ curl -s -X POST http://localhost:7071/api/graphs/simple_agent/invoke -H "Content-Type: application/json" -d '{"input":{"messages":[{"role":"human","content":"World"}],"greeting":""}}'
{"output": {"messages": [{"role": "human", "content": "World"}, {"role": "assistant", "content": "Hello, World! Goodbye!"}], "greeting": "Hello, World!"}}
```

`/api/health` is the minimal anonymous liveness probe; `/api/health/details` is
the protected graph inventory. Continue with the [full 5-minute tutorial](tutorial-5-min.md).

### Recommended learning path

Once the Quick Start echo agent runs, these four examples each demonstrate **one capability** on top of a real Azure OpenAI agent. They are focused, standalone samples — read them in this order, but they are **not** a strict cumulative build: each one is its own Azure Functions app and does not include the features of the others.

| Example | Real model | Per-thread memory | Tool loop | Persistent storage |
| --- | :---: | :---: | :---: | :---: |
| [**`azure_openai_agent`**](../examples/azure_openai_agent/) | ✅ | — | — | — |
| [**`conversation_memory`**](../examples/conversation_memory/) | ✅ | ✅ in-memory (`InMemorySaver`) | — | — |
| [**`tool_calling_agent`**](../examples/tool_calling_agent/) | ✅ | — (no checkpointer) | ✅ (tools in `tools.py`, OpenAPI/Swagger wired in) | — |
| [**`production_persistent_agent`**](../examples/production_persistent_agent/) | ✅ | ✅ | — (single chat node) | ✅ (Azure Blob checkpointer; Managed Identity in production, Azurite locally) |

Per-`thread_id` state separation is **not** authenticated user ownership: any caller who can reach the endpoint and knows a `thread_id` can continue that thread. Treat these samples as minimal starting points, not a ready-made multi-user service.

> **[Build your own agent](https://github.com/yeongseon/azure-functions-cookbook-python/blob/main/docs/patterns/ai-and-agents/personal-langgraph-agent.md):** the cookbook ships an integrated, runnable recipe ([`personal_langgraph_agent`](https://github.com/yeongseon/azure-functions-cookbook-python/tree/main/examples/ai-and-agents/personal_langgraph_agent)) that combines a model, your own read-only tool, conversation memory, and optional durable Blob storage, with deployment guidance. This repository intentionally keeps the examples above small; integrated recipes live in the cookbook (tracked in [cookbook#343](https://github.com/yeongseon/azure-functions-cookbook-python/issues/343)). Ownership: your LangGraph/application code owns prompts, models, tools and graph logic; this package owns the Azure Functions deployment adapter; the cookbook owns integrated usage recipes.

Every example is a standalone Azure Functions app with explicit `AGENT_MODEL_MODE=fake|azure_openai` selection; checked-in local templates choose deterministic fake mode so CI needs no cloud credentials, while Azure deployment instructions choose live mode and fail fast on incomplete settings.

```python
from langgraph.graph import END, START, StateGraph
from typing import TypedDict

import azure.functions as func

from azure_functions_langgraph import LangGraphApp


# 1. Define your state
class AgentState(TypedDict):
    messages: list[dict[str, str]]


# 2. Define your nodes
def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


# 3. Build graph
builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile()

# 4. Deploy (default is `AuthLevel.FUNCTION` — requires a function key when deployed).
app = LangGraphApp()
app.register(graph=graph, name="echo_agent")
func_app = app.function_app  # ← use this as your Azure Functions app
```

Start the Functions host locally:

```bash
func start
```

### Verify locally and on Azure

After deploying (see [deployment](deployment.md)), the same request produces the same response in both environments. Azure requires a function key (`?code=<FUNCTION_KEY>`) when `auth_level` is set to `FUNCTION`.

#### Local

```bash
curl -s http://localhost:7071/api/health
```

```json
{"status": "ok"}
```

The anonymous liveness probe returns only `{"status": "ok"}`. To see the
registered-graph inventory, call the protected details endpoint:

```bash
curl -s http://localhost:7071/api/health/details
```

```json
{"status": "ok", "graphs": [{"name": "echo_agent", "description": null, "has_checkpointer": false}]}
```

#### Azure

```bash
# Liveness probe (anonymous by default)
curl -s "https://<your-app>.azurewebsites.net/api/health"
```

```json
{"status": "ok"}
```

```bash
# Detailed inventory (protected — defaults to the app auth_level)
curl -s "https://<your-app>.azurewebsites.net/api/health/details?code=<FUNCTION_KEY>"
```

```json
{"status": "ok", "graphs": [{"name": "echo_agent", "description": null, "has_checkpointer": false}]}
```

> Response format verified against a temporary Azure Functions deployment of the `simple_agent` example in koreacentral (Python 3.12, Consumption plan). The Quick Start uses `echo_agent` for illustration; the health endpoint returns the same JSON structure regardless of graph name. URL anonymized.


### Core adapter: state and thread exclusion

Start with `LangGraphApp`, `register()` and `function_app`. For conversation
state, compile your graph with a LangGraph checkpointer and send
`config.configurable.thread_id`; a checkpointer persists graph state, not
Platform thread records. Native routes do not require `platform_compat=True`,
Azure Table Storage, Durable Functions or Service Bus.

`thread_lock` accepts the `ThreadLock` protocol from
`azure_functions_langgraph.locks`: `acquire(graph_name, thread_id, timeout=0.0)`
returns an owner token (or `None` on contention), and `release(...)` receives
that token. The default `InProcessThreadLock` guards checkpointed native
invoke/stream runs for the same `(lock scope, thread_id)` **inside one Python
process only**; the lock scope is the graph name unless the graph's checkpointer
reports a storage identity, in which case graphs on the same storage share a lock
(see [Lock scope and shared checkpointers](production-guide.md#lock-scope-and-shared-checkpointers)). Even one Function App instance can have multiple worker
processes; local locking is not distributed exclusion.

Before sharing checkpoint state across workers or apps, follow the
[production and distributed deployment guidance](production-guide.md#distributed-deployment).
Persistence and exclusion are separate choices. Thread locking and checkpoint
backends remain **Beta**, as listed in the stability table above.

### Production authentication

> **Important:** `LangGraphApp` defaults to `AuthLevel.FUNCTION`, so deployed
> endpoints require a function key (`?code=<FUNCTION_KEY>` or the
> `x-functions-key` header) out of the box. For an unauthenticated public
> surface — e.g. local development against `func start` when you also want to
> hit the endpoint without a key — pass `auth_level=func.AuthLevel.ANONYMOUS`
> **explicitly**. Doing so emits an unconditional `UserWarning` so an
> accidental anonymous deployment is loud in test and CI output:

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

# Production (default): require function key authentication
app = LangGraphApp()  # equivalent to LangGraphApp(auth_level=func.AuthLevel.FUNCTION)

# Local dev only: explicit opt-in to anonymous — emits a UserWarning
app_local = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
```

> **Note:** There are two health surfaces. The liveness probe
> `GET /api/health` uses `health_auth_level`, which defaults to `ANONYMOUS`
> independently of `auth_level`, and returns only `{"status": "ok"}` — it never
> enumerates registered graphs. Set `health_auth_level=func.AuthLevel.FUNCTION`
> to require a key on the probe as well.
>
> The detailed inventory `GET /api/health/details` (graph names, descriptions,
> and checkpointer status) uses `health_details_auth_level`, which defaults to
> the app-level `auth_level` (`FUNCTION`) — so the inventory is **protected by
> default**. Pass `health_details_auth_level=func.AuthLevel.ANONYMOUS`
> explicitly to expose it publicly (e.g. local development).

### Streaming behavior

Native buffered `POST /api/graphs/{name}/stream` responses encode each graph
event directly as JSON in an `event: data` SSE frame, on both sync and async
paths. Dictionaries remain JSON objects, tuples/lists become arrays, and scalars
retain their JSON types (including `null`). In `stream_mode="messages"`, the
message/metadata tuple becomes a two-element array; LangChain messages, including
nested messages, retain structured `message_to_dict` data. Pydantic models use
JSON-mode dumps; unsupported custom objects use the shared codec's `str` fallback.
Non-dictionary events no longer use the lossy `{"data": "<Python repr>"}` envelope.
Clients should JSON-decode each frame's `data:` value rather than assume an object.
Non-finite numbers and other encoding failures produce the existing sanitized
`event: error` frame, followed by `event: end`; normal completion also ends with
`event: end`. Platform-compatible and true-streaming transports are unchanged.

> **Default (`LangGraphApp`): buffered SSE.** All `/stream` endpoints on the classic
> `LangGraphApp` (the native `POST /api/graphs/{name}/stream` and the
> Platform-compatible `POST /threads/{id}/runs/stream` and `POST /runs/stream`)
> return **buffered SSE**. Chunks emitted by the graph are collected during execution
> and flushed as SSE events **after the run completes** — this is **not** true
> token-level streaming, and clients will not receive partial tokens incrementally.
> Buffered SSE is the classic adapter's deliberate implementation choice, because the
> classic `HttpRequest`/`HttpResponse` model it is built on cannot flush a response
> incrementally.
>
> **Opt-in true streaming: `StreamingLangGraphApp` (experimental).** For genuine
> incremental delivery — each `event: data` frame flushed **as the graph produces
> it** — use the separate `StreamingLangGraphApp`. It serves the graph over the
> FastAPI/ASGI transport provided by the `azurefunctions-extensions-http-fastapi`
> extension (Azure Functions runtime **4.34.1+**), gated behind the `streaming` extra:
>
> ```bash
> pip install "azure-functions-langgraph[streaming]"
> ```
>
> ```python
> from azure_functions_langgraph import StreamingLangGraphApp
>
> app = StreamingLangGraphApp()
> app.register(graph=graph, name="my_agent")
> func_app = app.function_app
> ```
>
> Enabling true streaming converts the **entire** function app to the ASGI model, so
> it cannot be mixed with classic `LangGraphApp` routes in the same app — deploy a
> separate app if you also need the classic buffered surface, and note the classic
> `max_stream_response_bytes` byte guard is replaced by `max_stream_events` (a frame
> count cap) because a true stream is never buffered. `LangGraphApp` is unchanged and
> remains the default. See [`examples/true_streaming_agent/`](../examples/true_streaming_agent/)
> for a runnable `curl -N` walkthrough that proves incremental arrival.
>
> On client cancellation, lock ownership follows graph execution rather than the
> socket lifetime. A synchronous stream waits for its one in-flight iterator
> pull to finish, closes the iterator, releases the thread token exactly once,
> and only then propagates cancellation. Python cannot terminate a hung worker
> thread, so synchronous nodes and tools must enforce their own timeouts. Async
> graphs use native async-generator cancellation without the thread bridge. See
> the [production guide](production-guide.md#true-stream-cancellation-and-lock-ownership).

### Run observability

Wire a [`RunObserver`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/src/azure_functions_langgraph/observability.py) to receive
run-lifecycle signals (`started` / `completed` / `failed` / `rejected`) for every
native invoke/stream run and every Platform `runs/wait` / `runs/stream` run. Each
callback receives an immutable `RunContext` of **correlation identifiers and
timing only** — never input, output, config, headers, or secrets.

A thread lock backend that raises while acquiring a native invoke or stream run is
reported as `started` followed by `failed`, and the response is the standard JSON 500;
a lock held elsewhere stays a `rejected` run with a 409.

A backend that raises while *releasing* a native invoke or buffered stream run turns an
otherwise successful run into `started` followed by `failed`, and the response is the
standard JSON 500 (`Thread lock could not be released`), not the result. If the graph itself
failed, its error is the one reported and the release failure is only logged. The run may
already have changed state, so reconcile it before retrying (see
[Troubleshooting](troubleshooting.md#native-invoke-or-stream-returns-500-thread-lock-could-not-be-released)).
The bundled locks never raise from `release`.

On the true-streaming surface, each started run receives exactly one terminal
callback, including invoke cancellation, stream disconnection and iterator
cleanup failures. Stream callbacks finish after iterator cleanup and lock
release, before the final `event: end` frame is yielded, so closing at that
frame cannot leave a span open. Cancellation and generator close still
propagate; they are not reported as successful graph results.

The package ships a built-in, dependency-free `LoggingRunObserver` for
zero-boilerplate telemetry:

```python
import logging
from azure_functions_langgraph import LangGraphApp, LoggingRunObserver

logging.getLogger("azure_functions_langgraph.observability.run").setLevel(logging.INFO)

app = LangGraphApp(observer=LoggingRunObserver())
app.register(graph=graph, name="my_agent")
```

It emits one structured log record per lifecycle event under a single `extra`
key (`langgraph_run`) — `graph_name`, `endpoint`, `run_id`, `thread_id`,
`assistant_id`, `invocation_id`, `stream_mode`, `transport`, `has_checkpointer`, `lock_backend`,
`trigger_type`,
`duration_ms`, a derived `status`, and (on failure) the exception `error_type`
(class name only). Azure Functions' Application Insights integration surfaces
these under `customDimensions`.

For native HTTP invoke and stream endpoints, `run_id` is the adapter's
request/run correlation ID. A valid caller `X-Request-ID` (1–64 ASCII letters,
digits, `.`, `_`, or `-`) is preserved; a missing, invalid, or oversized value
is replaced with an opaque generated ID. The same safe value appears in the
response `X-Request-ID`, observer context, and sanitized JSON or post-start SSE
errors. Request bodies never set this ID.

Keep that ID distinct from both `invocation_id` and distributed trace IDs:

- `invocation_id` identifies an Azure Functions host invocation and is not
  populated from caller-controlled headers or request bodies.
- W3C `traceparent` / Application Insights operation IDs identify distributed
  traces and follow the host or tracing SDK's propagation rules.
- `run_id` correlates this adapter's graph run across its HTTP response and
  observer lifecycle events; it is not authentication data and does not claim
  to be either identifier above.

Capturing Functions invocation context and wiring parent spans is tracked
separately in #598; this package does not infer either value from
`X-Request-ID`.

> The package owns the **domain signal** only — not log formatting, App Insights
> ingestion, OpenTelemetry exporters, sampling, or PII redaction. Observer
> failures are isolated and never fail a graph run. To write your own observer,
> see [`examples/run_observer/`](../examples/run_observer/); for an App Insights +
> KQL walkthrough, see
> [`examples/observability_app_insights/`](../examples/observability_app_insights/).

#### OpenTelemetry observer (optional `otel` extra)

For distributed-tracing backends, the package ships an optional
`OTelRunObserver` that maps each run's lifecycle to a single OpenTelemetry
span. It is gated behind the `otel` extra:

```bash
pip install azure-functions-langgraph[otel]
```

```python
from azure_functions_langgraph import LangGraphApp, OTelRunObserver

# Uses whatever TracerProvider the operator has already installed.
app = LangGraphApp(observer=OTelRunObserver())
app.register(graph=graph, name="my_agent")
```

`on_run_started` opens a span (keyed by `run_id`); the terminal event
(`completed` / `failed` / `rejected`) closes it with the matching
`StatusCode`, a `langgraph.status`, and a `langgraph.duration_ms`. Span
attributes come **only** from the safe `RunContext` correlation fields — never
input, output, config, headers, or secrets — and failures record the exception
*type* only. They do not emit an exception event, message, or stack trace.

> Consistent with the observability boundary above, `OTelRunObserver` **does
> not** configure a `TracerProvider`, exporters, sampling, or resource
> attributes — the operator owns all SDK/exporter wiring. The observer only
> acquires a tracer via `opentelemetry.trace.get_tracer(...)` and emits spans
> onto whatever provider is installed. For a runnable local demo with a console
> exporter, see [`examples/run_observer_otel/`](../examples/run_observer_otel/).

## Optional execution surfaces

These opt-ins solve delivery or execution-lifetime needs, not universal
concurrency problems. They are not prerequisites for the core HTTP adapter.

### Service Bus trigger (experimental)

Beyond the HTTP surface, a compiled graph can be driven directly from an
**Azure Service Bus** message via `register_service_bus`. Each delivered
message is mapped to a single `invoke` (or `ainvoke`) call — useful for
background jobs and decoupled pipelines where work arrives as messages rather
than synchronous HTTP requests. A graph may be registered on **both** surfaces.

```python
from azure_functions_langgraph import LangGraphApp

app = LangGraphApp()

# Queue trigger
app.register_service_bus(
    graph=graph,
    name="jobs_agent",
    connection="ServiceBusConnection",  # app-setting name
    queue_name="langgraph-jobs",
)

# Or a topic subscription trigger
app.register_service_bus(
    graph=graph,
    name="events_agent",
    connection="ServiceBusConnection",
    topic_name="events",
    subscription_name="langgraph",
)

func_app = app.function_app
```

#### HTTP vs Service Bus

| | HTTP (`register`) | Service Bus (`register_service_bus`) |
|---|---|---|
| Invocation | Synchronous `POST /api/graphs/{name}/invoke` | One graph run per queue/topic message |
| Caller gets result | Yes — in the HTTP response | No — use `result_handler` to route output |
| Delivery | At-most-once (caller retries) | At-least-once (binding abandons/retries) |
| Backpressure | Caller-driven | Queue buffers + Functions scale-out |
| Best for | Interactive request/response | Background jobs, decoupled pipelines |
| Errors | Returned to caller | **Not swallowed** → message abandoned/retried |

**Message mapping.** By default the message body is decoded as UTF-8 and parsed
as JSON: a JSON object is passed through as the graph input verbatim; any other
value (or non-JSON text) is wrapped as
`{"messages": [{"role": "human", "content": <text>}]}`. Pass `input_mapper=...`
to customize.

**Thread state and locking.** Runs are **threadless** by default. Pass
`thread_id_factory=...` to derive a checkpoint `thread_id` from a message; when
it returns a value **and** the graph has a checkpointer, the app-level
`thread_lock` guards the run so concurrent deliveries for the same logical
thread do not mutate checkpoints concurrently. Each delivery waits up to
`lock_timeout=5.0` seconds (configurable on `register_service_bus`) before
raising `ThreadContentionError` so the binding abandons and retries. The value
must be finite and non-negative; `0.0` restores immediate contention failure.
The adapter makes one `ThreadLock.acquire(..., timeout=...)` call, delegating
blocking/backoff to the backend rather than busy-spinning. Async acquisition
runs in a worker thread, not on the event loop. HTTP lock behavior is unchanged.

**Wait budget and delivery safety.** The ceiling is per acquisition, not per
message across redeliveries, and assumes the lock backend honors its timeout
(configure bounded SDK/network timeouts on distributed backends too). Waiting,
graph execution, result handling and settlement must fit within your queue or
subscription's message lock duration, the host's `maxAutoLockRenewalDuration`
for single-message triggers, and `functionTimeout`, with safety margin.
Renewal can fail: this adapter neither renews broker locks nor reads remaining
message/session lock lifetime. If sessions are explicitly enabled, budget for
session lock/renewal limits as well; sessions remain **off by default**.
Sessions require a session-enabled queue/subscription, a sender-supplied
`SessionId`, and explicit `session=True`. The application lock stays enabled
unless `session_exclusive=True` asserts an isolated single-entity writer topology;
see the safety requirements and live proof below.
They order related messages within that entity; they do not exclude HTTP,
Durable, other queues or other subscriptions writing the same checkpoint.
See [session ordering and mixed surfaces](production-guide.md#service-bus-sessions-and-mixed-surfaces).
`clientRetryOptions` controls broker operations, not function-execution retries.
See the [Service Bus binding settings](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-service-bus).

Host cancellation during async acquisition waits for the bounded worker to
finish, releases any late-acquired token, then propagates cancellation without
running the graph. Synchronous acquisition cannot be interrupted by Python;
allow shutdown grace for its wait and backend cleanup. Abrupt process termination
cannot guarantee cleanup. Waiting reduces rapid abandon/redelivery cycles but
**cannot prevent dead-lettering** after `maxDeliveryCount` is exhausted. Delivery
remains **at-least-once**, not exactly-once: make graphs and result handlers
idempotent (for example using a durable message-ID record).

**Error handling and telemetry.** Graph exceptions are **not swallowed** — a
failing run propagates so the message is retried / dead-lettered per your
queue's policy. This package ships **no** Service Bus output binding; pass a
`result_handler(result, msg)` to publish results. Message bodies and
application-property values are **never** included in telemetry; runs carry a
`trigger_type="service_bus"` correlation field so they are distinguishable from
HTTP runs in your observer. See
[`examples/service_bus_agent/`](../examples/service_bus_agent/) for a full wiring.


### Durable async run lifecycle (experimental)

For long-running graphs, opt into an **experimental** Durable Functions-backed
async run control plane with `LangGraphApp(async_runs="durable")`. This adds a
create / poll / cancel HTTP surface on top of the classic invoke/stream routes:

```python
from azure_functions_langgraph import LangGraphApp

app = LangGraphApp(async_runs="durable")  # requires the `durable` extra
app.register(graph=graph, name="my_agent")
func_app = app.function_app
```

```bash
pip install "azure-functions-langgraph[durable]"
```

| Route | Purpose |
|---|---|
| `POST /api/graphs/{name}/runs` | Start a run — returns `202` with a `run_id` and `pending` status |
| `GET /api/runs/{run_id}` | Poll normalized run status (`pending` / `running` / `success` / `error` / `cancelled`) |
| `POST /api/runs/{run_id}/cancel` | Request termination of a run: `202` with `cancel_requested` while it is in flight, `200` with the real status once it finished, `404` unknown, sanitized `502` on a backend failure |

The graph executes inside a Durable **activity**, never the orchestrator, so
Durable replay determinism is preserved — no LLM, tool, network, clock, random,
or graph user code runs in the orchestrator. Durable history is **not** a
replacement for LangGraph checkpoints; reuse a checkpointer for conversation
state as usual. Durable dependencies stay optional behind the `durable` extra.
See [`examples/durable_async_agent/`](../examples/durable_async_agent/) for a full
create → poll → result → cancel walkthrough.

Activity output is converted to plain JSON **before** the observer reports
completion and before Durable persists the result. LangChain messages, including
those nested in lists or dictionaries, retain structured `message_to_dict` data.
Other custom values follow the existing JSON codec's string fallback. If encoding
fails (for example, circular containers, unsupported dictionary keys, non-finite
numbers, or a failing string conversion), the activity returns a normalized
`error` with a generic serialization message and emits `on_run_failed`, never
`on_run_completed`. It does not retry graph execution just to encode its output.
Durable delivery remains **at-least-once**: duplicate deliveries or host failure
can still execute the graph again, so graph side effects must be idempotent.

Durable is a long-running execution/polling choice, not a global single-writer
service. Activities use the configured `thread_lock` for checkpointed runs with
a thread ID; separate apps must share a distributed lock configuration when
they write the same thread. Terminating an orchestration does not forcibly
stop an already-running activity or synchronous tool. `cancel_requested` is an
acknowledgement, not a status: it means only that the termination call returned without
reporting a failure, `GET` never returns it, and a `cancelled` status means the
orchestration is terminated, not that the Activity or its tools stopped. Poll `GET` until the
status is terminal; the full contract, the thread-ownership behavior, the handling of
non-idempotent tools and the migration from the earlier `202 cancelled` body are in the
[production guide](production-guide.md#cancellation-of-durable-runs). See the
[production guide](production-guide.md#async-runs-via-durable-functions-experimental).


## Core adapter reference

### Per-graph auth

Override app-level auth settings per graph:

```python
# Per-graph authentication override
app.register(graph=public_graph, name="public", auth_level=func.AuthLevel.ANONYMOUS)
app.register(graph=private_graph, name="private", auth_level=func.AuthLevel.FUNCTION)
```

> **Important:** Per-graph overrides apply only to native graph routes. Platform-compatible
> and Durable routes use the app-level `auth_level`, and a graph stricter than the app level
> is rejected with `ValueError` when those routes are enabled; see
> [security](security.md#mixed-per-graph-auth-with-platform-or-durable-routes) and the
> [production guide](production-guide.md) before exposing them.

Example request using a function key:

```bash
curl -X POST "https://<app>.azurewebsites.net/api/graphs/echo_agent/invoke?code=<FUNCTION_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Hello!"}]}}'
```

### Custom route prefix

All routes use the default `/api` prefix set by Azure Functions. To change it,
configure `routePrefix` in your `host.json`:

```json
{
  "extensions": {
    "http": {
      "routePrefix": "v1"
    }
  }
}
```

This changes all routes (e.g. `POST /v1/graphs/{name}/invoke`). Set `routePrefix`
to `""` to remove the prefix entirely.

> **Important — `LangGraphApp(route_prefix=...)` is metadata-only.** Azure Functions resolves HTTP routes from `host.json` (the **source of truth**), not from the constructor argument. The `route_prefix` argument is recorded into the metadata snapshot consumed by tooling such as the `azure-functions-openapi-python` bridge so generated specs reflect the deployed routes, but changing it without also updating `host.json` does **not** change where requests are served. Always edit `host.json` to actually move routes, and pass the same value to `LangGraphApp(route_prefix=...)` so metadata stays in sync.

### What you get

- `POST /api/graphs/echo_agent/invoke` — invoke the agent
- `POST /api/graphs/echo_agent/stream` — stream agent responses (buffered SSE; opt into true incremental streaming with `StreamingLangGraphApp`)
- `GET /api/graphs/echo_agent/threads/{thread_id}/state` — inspect thread state
- `GET /api/health` — liveness probe (`{"status": "ok"}`)
- `GET /api/health/details` — registered-graph inventory (protected by default)

With `platform_compat=True`, you also get SDK-compatible endpoints:

- `POST /assistants/search` — list registered assistants
- `GET /assistants/{id}` — get assistant details
- `POST /assistants/count` — count assistants
- `POST /threads` — create thread
- `GET /threads/{id}` — get thread
- `PATCH /threads/{id}` — update thread metadata: the request's keys are merged into the
  thread's current metadata (a key sent again takes the new value; every other key is kept).
  The bundled stores apply the merge atomically, so the keys of every patch that succeeds
  survive concurrent patches of other keys; for one key the last writer wins. A merge that
  fails is answered with a sanitized `500`. See
  [Atomic metadata merge](ops-and-apis.md#atomic-metadata-merge).
- `DELETE /threads/{id}` — delete thread
- `POST /threads/search` — search threads
- `POST /threads/count` — count threads
- `POST /threads/{id}/runs/wait` — run and wait for result
- `POST /threads/{id}/runs/stream` — run and stream result (buffered SSE)
- `POST /runs/wait` — threadless run
- `POST /runs/stream` — threadless stream (buffered SSE)
- `GET /threads/{id}/state` — get thread state
- `POST /threads/{id}/state` — update thread state
- `POST /threads/{id}/history` — get state history

### Request format

Native classic and true-stream invoke/stream routes accept only `config.configurable` from
clients, with `thread_id` as the sole default key. Unapproved fields return HTTP
400 before authorization, locking or graph execution. Execution controls such as
`recursion_limit`, callbacks and unknown top-level keys cannot be supplied by a
client; bind trusted defaults on the server (for example with `graph.with_config`).
The existing input depth, node-count and body-size limits still apply.

Migration: if your application intentionally exposes a custom configurable field,
register it explicitly with
`app.register(graph, "agent", allowed_configurable_keys=frozenset({"locale"}))`.
Only allow keys whose values are safe for an untrusted client to choose. Opting
in to `checkpoint_id` or `checkpoint_ns` deliberately grants checkpoint-selection
control and requires corresponding ownership checks in the classic RUN authorizer
or the [transport-specific streaming authorizer](security.md#true-stream-authorization).
Internal `__*` keys remain forbidden even when allow-listed. `thread_id` remains
subject to validation and RUN authorization; an allow-list is not authentication.
This policy covers native sync/async invoke, buffered stream and experimental
true-streaming routes, not Platform, Durable or Service Bus transports.
The true-stream restriction is breaking for consumers that previously sent
arbitrary config; move execution controls to server-owned defaults and opt in to
safe application fields as described in [configuration](configuration.md#native-client-configuration-admission).
Relative to that true-stream change, classic native admission semantics and the
`thread_id` contract are unchanged. Relative to 0.12.2, classic admission itself
became stricter in 0.13.0 (#740); see [Upgrading](#upgrading).

Native invoke responses keep the `{"output": ...}` envelope and use the shared
JSON codec: nested LangChain messages retain structured `message_to_dict` data,
Pydantic models use JSON-mode dumps, and other custom values (including datetime,
set and bytes values nested in dictionaries) use the codec's `str` fallback.
Response encoding finishes before `on_run_completed`. If encoding fails, the
adapter returns a sanitized HTTP 500 with the same `X-Request-ID`, emits one
`on_run_failed` event instead of completion, and releases the thread lock.
Graph execution is not retried to serialize its output.

```json
{
    "input": {
        "messages": [{"role": "human", "content": "Hello!"}]
    },
    "config": {
        "configurable": {"thread_id": "conversation-1"}
    }
}
```

## Production and distributed deployment

Keep the core adapter independent of deployment infrastructure. Choose a
checkpointer for graph state, a `ThreadLock` for writer exclusion, and a
Platform `ThreadStore` only if you expose Platform-compatible routes. The
[production deployment decision table](production-guide.md#distributed-deployment)
covers scale-out, lease lifetime/recovery, mixed surfaces and optional triggers.
No lock backend is switched automatically.

### Persistent storage (v0.4+)

Platform thread records default to process-local memory, independently of graph
checkpoints. Azure-hosted apps warn when the final store is still in memory;
`AZFUNC_LANGGRAPH_THREAD_STORE=distributed` or
`AZFUNC_LANGGRAPH_LOCK_BACKEND=distributed` makes that configuration fail before
routes are exported. Set `app.thread_store` before `app.function_app`.
Local single-process development needs neither setting nor external storage.
The smoke-tested [Managed Identity storage recipe](../examples/managed_identity_storage/)
wires an explicit Azure Table store; a distributed lock alone does not share
thread records.

Use Azure Blob Storage for checkpoint persistence and Azure Table Storage for thread metadata:

```python
import azure.functions as func

from azure.storage.blob import ContainerClient
from langgraph.graph import END, START, StateGraph
from typing import TypedDict

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore


class AgentState(TypedDict):
    messages: list[dict[str, str]]


def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


# Build graph with Azure Blob checkpointer
container_client = ContainerClient.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", "checkpoints"
)
saver = AzureBlobCheckpointSaver(container_client=container_client)

builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile(checkpointer=saver)

# Deploy with Azure Table thread store
thread_store = AzureTableThreadStore.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", table_name="threads"
)

# Production: always set auth_level explicitly
app = LangGraphApp(platform_compat=True, auth_level=func.AuthLevel.FUNCTION)
app.thread_store = thread_store
app.register(graph=graph, name="echo_agent")
func_app = app.function_app
```

Checkpoints and thread metadata survive Azure Functions restarts. This example
wires persistence, not distributed exclusion: before scaling or mixing native
and Platform writers, also supply a distributed `thread_lock` as described in
the [production guide](production-guide.md#distributed-thread-locking).

#### Persistent storage with Managed Identity

The recommended production wiring uses **Managed Identity** instead of connection strings, so no secrets land in App Settings. Install the `azure-identity` extra and pass `DefaultAzureCredential` to both clients:

```bash
pip install azure-functions-langgraph[azure-blob,azure-table,azure-identity]
```

```python
from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

credential = DefaultAzureCredential()

container_client = ContainerClient(
    account_url="https://<account>.blob.core.windows.net",
    container_name="langgraph-checkpoints",
    credential=credential,
)
table_client = TableClient(
    endpoint="https://<account>.table.core.windows.net",
    table_name="langgraphthreads",
    credential=credential,
)

checkpointer = AzureBlobCheckpointSaver(container_client=container_client)
thread_store = AzureTableThreadStore.from_table_client(table_client=table_client)
```

Required role assignments on the storage account (or narrower scopes):

| Role | Used by |
| --- | --- |
| `Storage Blob Data Contributor` | `AzureBlobCheckpointSaver` |
| `Storage Table Data Contributor` | `AzureTableThreadStore` |

`DefaultAzureCredential` walks a chain of credentials. In Azure Functions it picks up the Function App's Managed Identity; locally it falls back to `AzureCliCredential` (`az login`) — the same code path works in both environments without conditional wiring.

For a complete runnable example (Managed Identity in prod, Azurite + connection string locally), see [`examples/managed_identity_storage/`](../examples/managed_identity_storage/).

#### Native async checkpoint I/O

By default `AzureBlobCheckpointSaver` is configured with a synchronous
`ContainerClient`. Its async methods (`aget_tuple`, `alist`, `aput`,
`aput_writes`, `adelete_thread`) remain fully available: when a graph is driven
through the async endpoints (`graph.ainvoke` / `graph.astream`) without an aio
client, they run the synchronous implementation in a thread executor and emit a
one-time warning — correct, but not truly non-blocking.

To get **native, non-blocking** blob I/O on the async path, pass an
`azure.storage.blob.aio.ContainerClient` as `aio_container_client` alongside the
synchronous `container_client`. Both clients share the same container; the sync
methods use the sync client and the async methods use the aio client, so
checkpoints written by one path are readable by the other:

```python
from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AioDefaultAzureCredential
from azure.storage.blob import ContainerClient
from azure.storage.blob.aio import ContainerClient as AioContainerClient

container_client = ContainerClient(
    account_url="https://<account>.blob.core.windows.net",
    container_name="langgraph-checkpoints",
    credential=DefaultAzureCredential(),
)
aio_container_client = AioContainerClient(
    account_url="https://<account>.blob.core.windows.net",
    container_name="langgraph-checkpoints",
    credential=AioDefaultAzureCredential(),
)

checkpointer = AzureBlobCheckpointSaver(
    container_client=container_client,
    aio_container_client=aio_container_client,
)
```

> The caller owns the lifecycle of both clients (open/close), exactly as with
> the synchronous client. `aio_container_client` is fully optional and
> backward-compatible — omitting it preserves the previous executor-fallback
> behaviour.

#### Checkpoint store security

The checkpointer backends persist graph state using LangGraph's default
serializer, which can restore **arbitrary Python types** from a checkpoint
payload. Treat the checkpoint store as part of your threat model — checkpoint
blobs are **not** trusted-free data:

- **Restrict storage access** with RBAC / Managed Identity (above) and private
  endpoints so only the Function App identity can read or write checkpoints.
- **Enable strict deserialization** by setting `LANGGRAPH_STRICT_MSGPACK=true`
  (plus any allowed modules your graphs need) in your Function App
  **application settings**. The env var is read at **import time**, so it must
  be set before the app imports LangGraph — configure it as an app setting, not
  at runtime. Upstream defaults to permissive (`false`).

See the upstream advisory
[GHSA-g48c-2wqr-h844](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-g48c-2wqr-h844)
and [security](security.md#checkpoint-store-security) for the full
rationale.

### Table status index (opt-in, experimental)

Off by default. With the default constructor the store writes no extra rows, sends no extra
requests and keeps the table format unchanged. `search(status=...)` scans every thread of that
status (about 1,000 entities per request), so `limit` and `offset` do not bound the I/O. Real
Azure scan measurements are in #797; the index's real-Azure evidence is recorded below.

```python
store = AzureTableThreadStore.from_connection_string(
    conn, "langgraphthreads",
    status_index=True,                      # write hints; read them only while the marker is ready
    status_index_offset_cap=200,            # deeper offsets scan instead
    status_index_state_ttl_seconds=30.0,    # how long a process caches the marker
)
```

`from_table_client(client, status_index=True, ...)` takes the same options. The table client must
support `upsert_entity`, `submit_transaction` and `delete_entity` (`azure.data.tables.TableClient`
does). The `ThreadStore` protocol is unchanged.

**Layout.** In the same table, `PartitionKey="idx:<status>"` and
`RowKey="<inverted created_at, 19 digits>~<thread_id>"` for the terminal statuses `idle`,
`interrupted` and `error` (`busy` is not indexed and keeps the scan). A hint carries only a random
`u` value. One marker row (`PartitionKey="idx-meta"`, `RowKey="status-index"`) holds the state
`building`, `ready` or `disabled` (no row means `absent`); a `clock` row in the same partition is used by repair.
Every query of the store filters `PartitionKey eq 'thread'`, so these rows are never returned as threads.

**What is served from the index.** Only `search(status=<idle|interrupted|error>)` with no `metadata`
filter, while the marker is `ready` and `offset <= status_index_offset_cap`. Unfiltered search,
metadata filters, `status="busy"` and `count()` always scan, as before. In every other case the
reader falls back to the scan automatically (logged at DEBUG). Results and order equal the scan:
`created_at` descending, thread id ascending on ties, `limit=0`, every `offset`.

**Consistency contract.**

- *Hints are never wrong.* A reader verifies each hint against the primary row (status equal to the
  requested status and a matching `created_at` key) and returns a thread only then. A stale hint is
  skipped, never returned.
- *Hints are never missing while every writer is index-aware.* A writer upserts the hint of the
  status it is about to set **before** the primary ETag compare-and-swap, so a crash or a lost race
  leaves at most a stale hint. Superseded hints are not deleted eagerly (lazy deletion): a thread can
  have up to three hints and the old ones are removed by `repair_status_index()`.
- *Index and primary row are not transactional* (Azure Table cannot span partitions in one
  transaction), and a reader does not see one consistent snapshot of both: a thread moving between
  statuses during a search can appear under neither or under the old status for that call, exactly
  as with a scan that races a write.
- *Mixed fleets break completeness.* An old-version instance, or one with `status_index` off, that
  creates a thread or changes its status after the backfill writes **no hint**, and while the
  marker is `ready` readers will omit that thread from indexed searches. The package cannot see
  other writers. `audit_status_index()` detects this (`missing`); an unclean `audit_status_index(mark_ready=True)`
  demotes a `ready` marker to `building` so readers scan again.
- *Marker propagation.* A process caches the marker for `status_index_state_ttl_seconds` (default 30);
  after `disable_status_index()` other processes can still read the index for up to that long. Use `0` to read it on every search.

**Cost.** Per request count (fake table, `tests/test_table_status_index.py`; not Azure timings):

| Operation | Writes / gets off | Writes / gets on |
|---|---|---|
| `create` | 1 / 0 | 2 / 0 |
| acquire run lock (idle to busy) | 1 / 1 | 1 / 1 |
| release (busy to a terminal status) | 1 / 1 | 2 / 1 |
| `update(status=<new terminal status>)` | 1 / 1 | 2 / 1 |
| `update(metadata=...)`, `merge_metadata`, same status | 1 / 1 | 1 / 1 |
| `delete` | 1 / 1 | 1 / 1 |

A Platform run (acquire, release) goes from 2 to 3 writes. `search(status="idle", limit=10)` over
3,000 threads (1,500 idle) read 1,500 entities in 2 requests by scan, and 29 entities in 3 requests
through the index (2 once the marker is cached). A deep window verifies every row in front of it: `offset=100` read 225
entities in 10 requests, which is why the offset is capped. Stale hints in the window add reads.
Hint writes concentrate on one partition per status.

**Rollout.**

1. Deploy a version with the feature off everywhere.
2. Deploy `status_index=True` to **every** instance (they now write hints; readers still scan
   because the marker is not `ready`).
3. Backfill from an admin tool or Timer Trigger: `store.rebuild_status_index()` (sets the marker to
   `building`; idempotent; use `max_threads=` / `resume_after=result.last_row_key` for slices).
4. `store.audit_status_index(mark_ready=True)`. Only a clean audit (no `missing`) sets `ready`.
5. Periodically run `audit_status_index()` and `repair_status_index(grace_seconds=600)`. Repair only
   deletes: it examines hints older than the grace period measured with the table service
   `Timestamp` (not the host clock), verifies each against the primary row and deletes it with an
   ETag condition, so a concurrent writer's fresh hint wins.

**Rollback.** `store.disable_status_index()` sets the marker to `disabled` (readers scan after the
cache TTL), then turn `status_index` off in the application, then optionally
`disable_status_index(delete_rows=True)` to delete the hint rows. **Re-enabling always requires a
new backfill and a clean audit**, because writes made while the index was off were not hinted.

**Verification status.** Fake-table and Azurite failure-injection coverage is supplemented by
the real-Azure run below. The feature remains experimental: in-region latency, hot-partition
throughput, sustained independent-process writers and maintainer acceptance remain open.

#### Real-Azure status-index evidence — 2026-11-24 (#801)

**Scope and limits first.** These are real Azure Table SDK measurements against one temporary
`Standard_LRS` / `StorageV2` account in `koreacentral`, not emulator results. Latency is
**client-to-Azure from a local workstation, not in-region**, including SDK decoding and oracle
assertions. No VM/container/Functions compute was created. Sequential measurements on one
client are not throughput, throttling, sustained multi-process or production certification.
They inform, but do not make, the maintainer's write-cost, consistency-contract or support-tier
decision. No issue checklist was changed.

**Repository reality.** Base `e62aea4` includes the index from #910. The older issue proposal
saying recovery/backfill are not implemented is superseded by its 2026-11-21 checkpoint and
newer comments. The [2026-11-20 Azurite rehearsal](https://github.com/yeongseon/azure-functions-langgraph-python/issues/801#issuecomment-7031497779)
is an issue comment, not checked-in results; its driver was a scratch script. This evidence is
placed beside the existing contract rather than claiming a repository rollout script existed.
The existing integration fixture was emulator-only; this PR adds the env-var selector for its
existing scenarios. The original benchmark still measures scans/prototype, not the shipped index;
the small companion `tools/benchmark_table_status_index.py` reuses its generator and transport.
The cost table's old `delete` entry (zero gets) lagged #935: deletion now reads first, off and on.

**Method.** Python 3.11.15, `azure-data-tables` 12.7.0, `azure-core` 1.41.0. Seed 7;
`tools/benchmark_table_store.py:populate`, base 2025-01-01 UTC, paired timestamps five seconds
apart, UUID thread IDs; metadata tenant (50 values), kind (3), user (5,000). Status counts:

| Threads | idle | busy | interrupted | error |
|---:|---:|---:|---:|---:|
| 1,000 | 814 | 85 | 72 | 29 |
| 10,000 | 8,016 | 967 | 707 | 310 |
| 100,000 | 80,067 | 10,113 | 6,897 | 2,923 |

One fresh table per size, deleted before the next size. At most 100,000 **primary threads**;
the enabled table additionally held 89,887 hints and a marker at that size (not 100,000 total
physical rows). Population uses 100-primary-row transactions and excludes index hot-path write
cost, which is measured separately. Three timed repetitions plus one separate untimed
`tracemalloc` pass for each read. Memory is additional traced Python allocation, not RSS or
instance sizing. The new transport decodes response bodies for row/byte counts; this instrumentation
also contributes to measured time and memory. HTTP counts include every send, including retries;
rows count successful GET response entities, not server-side rows inspected. Bytes are response
body bytes, not headers, TLS overhead or request upload bytes.

The index retains its default offset cap 200 and marker TTL 30 seconds. Off runs precede on runs,
so cache expiry can add one marker GET to the next on run. A marker response adds one received
row, not a consumed query row. These explain the differing request counts below; no retry rate
or HTTP-status histogram was captured, so totals alone do not prove absence of retries.

**Read results.** Every cell's returned IDs/count matched an independent oracle from seeded rows
(`created_at` descending, thread ID ascending), including ties and fallback. 72 workload/mode/size
cells, four executions each, zero mismatches. In the following table `off → on`; request series
are timed reps 1/2/3/**memory pass**. Rows collapse only when identical in all four executions.
Latency is median/max milliseconds across the three timed reps; memory is MiB.
Searches default to limit 10, offset 0 unless shown.

| Threads | Operation | HTTP requests off → on | GET rows off → on | Traced MiB off → on | Median/max ms off → on |
|---:|---|---|---|---|---|
| 1,000 | idle | 1/1/1/1 → 2/2/2/2 | 814 → 28 | 3.9 → 0.1 | 126.5/128.1 → 34.7/40.0 |
| 1,000 | interrupted | 1/1/1/1 → 2/2/2/2 | 72 → 28 | 0.3 → 0.1 | 23.7/81.5 → 65.9/72.3 |
| 1,000 | error | 1/1/1/1 → 2/2/2/2 | 29 → 28 | 0.1 → 0.1 | 28.1/76.4 → 44.0/70.6 |
| 1,000 | busy (scan) | 1/1/1/1 → 1/1/1/1 | 85 → 85 | 0.4 → 0.4 | 45.5/86.3 → 27.9/38.4 |
| 1,000 | idle limit=0 | 1/1/1/1 → 0/0/0/0 | 814 → 0 | 3.9 → 0.0 | 114.6/171.6 → 0.0/0.0 |
| 1,000 | idle offset=100 | 1/1/1/1 → 9/9/9/9 | 814 → 224 | 3.9 → 0.5 | 136.1/175.4 → 268.8/362.0 |
| 1,000 | idle offset=200 | 1/1/1/1 → 16/16/16/16 | 814 → 420 | 3.9 → 0.8 | 122.7/169.9 → 604.1/635.8 |
| 1,000 | idle offset=201 (scan) | 1/1/1/1 → 1/1/1/1 | 814 → 814 | 3.9 → 3.9 | 116.3/202.5 → 184.1/190.3 |
| 1,000 | idle limit=1000 | 1/1/1/1 → 60/60/60/60 | 814 → 1,628 | 3.9 → 4.7 | 153.2/220.8 → 1775.8/2406.5 |
| 1,000 | unfiltered | 1/1/1/1 → 1/1/1/1 | 1,000 → 1,000 | 4.8 → 4.8 | 148.4/186.1 → 123.4/140.5 |
| 1,000 | tenant=t7 | 1/1/1/1 → 1/1/1/1 | 1,000 → 1,000 | 4.8 → 4.8 | 158.8/202.4 → 152.0/163.6 |
| 1,000 | count idle | 1/1/1/1 → 1/1/1/1 | 814 → 814 | 3.9 → 3.9 | 142.7/195.7 → 216.2/219.0 |
| 10,000 | idle | 9/9/9/9 → 2/2/2/2 | 8,016 → 28 | 23.1 → 0.1 | 1342.7/1529.0 → 67.1/125.7 |
| 10,000 | interrupted | 1/1/1/1 → 2/2/2/2 | 707 → 28 | 3.4 → 0.1 | 161.3/236.9 → 74.3/112.3 |
| 10,000 | error | 1/1/1/1 → 2/2/2/2 | 310 → 28 | 1.5 → 0.1 | 125.6/127.8 → 86.6/94.5 |
| 10,000 | busy (scan) | 1/1/1/1 → 1/1/1/1 | 967 → 967 | 4.6 → 4.6 | 231.0/232.3 → 208.2/252.1 |
| 10,000 | idle limit=0 | 9/9/9/9 → 0/0/0/0 | 8,016 → 0 | 23.1 → 0.0 | 1487.5/1587.3 → 0.0/0.0 |
| 10,000 | idle offset=100 | 9/9/9/9 → 10/9/9/9 | 8,016 → 225/224/224/224 | 23.1 → 0.5 | 1689.5/1773.0 → 676.8/683.1 |
| 10,000 | idle offset=200 | 9/9/9/9 → 16/16/16/16 | 8,016 → 420 | 23.1 → 0.9 | 1552.5/1566.0 → 1192.1/1474.1 |
| 10,000 | idle offset=201 (scan) | 9/9/9/9 → 9/9/9/9 | 8,016 → 8,016 | 23.1 → 23.1 | 1404.7/1475.2 → 1381.7/1760.9 |
| 10,000 | idle limit=1000 | 9/9/9/9 → 75/74/74/74 | 8,016 → 2997/2996/2996/2996 | 23.1 → 5.6 | 1185.8/1332.2 → 5815.8/6385.7 |
| 10,000 | unfiltered | 10/10/10/10 → 10/10/10/10 | 10,000 → 10,000 | 28.5 → 28.5 | 1604.8/1711.4 → 1489.6/1620.1 |
| 10,000 | tenant=t7 | 10/10/10/10 → 10/10/10/10 | 10,000 → 10,000 | 28.3 → 28.3 | 1466.5/1606.5 → 1595.0/1613.1 |
| 10,000 | count idle | 9/9/9/9 → 9/9/9/9 | 8,016 → 8,016 | 23.1 → 23.1 | 1290.1/1344.9 → 1306.6/1453.1 |
| 100,000 | idle | 81/81/81/81 → 3/2/2/2 | 80,067 → 29/28/28/28 | 210.9 → 0.1 | 12963.1/14143.8 → 480.4/804.2 |
| 100,000 | interrupted | 7/7/7/7 → 2/2/2/2 | 6,897 → 28 | 20.1 → 0.1 | 1410.2/1550.3 → 412.1/432.1 |
| 100,000 | error | 3/3/3/3 → 2/2/2/2 | 2,923 → 28 | 9.8 → 0.1 | 861.8/1001.7 → 465.6/547.3 |
| 100,000 | busy (scan) | 11/11/11/11 → 11/11/11/11 | 10,113 → 10,113 | 28.5 → 28.5 | 2165.6/2231.5 → 2059.4/2116.6 |
| 100,000 | idle limit=0 | 81/81/81/81 → 1/0/0/0 | 80,067 → 1/0/0/0 | 210.9 → 0.0 | 12809.6/12882.3 → 0.0/8.2 |
| 100,000 | idle offset=100 | 81/81/81/81 → 10/9/9/9 | 80,067 → 225/224/224/224 | 210.9 → 0.5 | 12947.1/15276.3 → 3658.4/3761.3 |
| 100,000 | idle offset=200 | 81/81/81/81 → 17/16/16/16 | 80,067 → 421/420/420/420 | 210.9 → 0.8 | 13387.4/13591.0 → 6312.7/7251.4 |
| 100,000 | idle offset=201 (scan) | 81/81/81/81 → 81/81/81/81 | 80,067 → 80,067 | 210.9 → 210.9 | 13937.2/13952.2 → 14310.6/15215.1 |
| 100,000 | idle limit=1000 | 81/81/81/81 → 75/75/75/75 | 80,067 → 2,997 | 210.9 → 5.6 | 14255.1/15194.8 → 31917.2/33003.8 |
| 100,000 | unfiltered | 100/100/100/100 → 100/100/100/100 | 100,000 → 100,000 | 265.0 → 265.0 | 16787.2/16893.3 → 16460.8/16494.6 |
| 100,000 | tenant=t7 | 100/100/100/100 → 100/100/100/100 | 100,000 → 100,000 | 262.8 → 262.8 | 15987.3/16224.1 → 16618.4/16767.2 |
| 100,000 | count idle | 81/81/81/81 → 81/81/81/81 | 80,067 → 80,067 | 210.9 → 210.9 | 13272.4/13995.5 → 13763.1/14341.0 |

Idle limit=10 received about 8.5 KB on the index at all sizes, versus 370,306 / 3,645,755 /
36,412,133 bytes by scan. Offset=100 received about 67 KB; offset=200 about 125 KB.
At 10k and 100k, limit=1000 consumed **2,016** query entities but fetched **2,996**
(plus any marker): two 994-hint pages and 1,008 verified primaries. The second full hint page
was fetched to consume only 14 hints. At 1k it exhausted 814 hints and primaries (1,628 rows).
Thus returned rows are not rows read, and the index is not always cheaper: rare statuses at 1k
add a second request, offset=200 costs 16 requests versus one at 1k, and limit=1000 is slower
at **every** size here. Even fixed 2-request searches rose from tens to hundreds of milliseconds
at 100k; the OR-filter verification query may inspect more of the primary partition, but this
run cannot isolate service scanning, client effects or network variance. Do not call it O(1)
latency or extrapolate a supported load. No shipped-code fix or search expansion is included.

**Write amplification.** Three independent create/run/update/delete sequences per mode at each
size. Counts were identical in all repetitions and sizes. Writes are POST/PUT/PATCH/DELETE
sends; reads are GET sends, not a model. Primary seeding batches are excluded.

| Operation | Writes / GETs off → on | Extra writes |
|---|---|---:|
| create | 1/0 → 2/0 | +100% |
| acquire idle → busy | 1/1 → 1/1 | 0 |
| release busy → idle | 1/1 → 2/1 | +100% |
| update idle → error | 1/1 → 2/1 | +100% |
| merge metadata | 1/1 → 1/1 | 0 |
| delete | 1/1 → 1/1 | 0 (leaves stale hints) |

A run's acquire+release goes from **2 writes + 2 GETs to 3 writes + 2 GETs**:
+50% writes, +25% total HTTP requests. Write latency samples (all three, milliseconds):

| Threads | Operation | off samples → on samples |
|---:|---|---|
| 1,000 | create | 9.5/11.8/9.9 → 181.7/15.5/24.2 |
| 1,000 | acquire | 86.1/18.6/24.6 → 25.4/15.1/18.6 |
| 1,000 | release idle | 21.3/16.8/26.8 → 37.0/27.2/27.7 |
| 1,000 | update error | 25.1/19.0/24.3 → 47.4/31.9/27.9 |
| 1,000 | metadata | 22.6/16.4/24.7 → 15.7/24.4/69.9 |
| 1,000 | delete | 21.3/16.1/23.9 → 20.9/23.1/24.1 |
| 10,000 | create | 9.1/8.9/11.4 → 23.6/17.5/16.8 |
| 10,000 | acquire | 15.5/16.0/20.5 → 20.3/15.2/14.7 |
| 10,000 | release idle | 14.5/17.6/21.0 → 30.0/22.6/34.5 |
| 10,000 | update error | 14.7/24.5/37.5 → 34.1/23.1/26.6 |
| 10,000 | metadata | 15.1/79.4/20.9 → 18.0/87.1/23.6 |
| 10,000 | delete | 15.0/19.7/20.3 → 17.8/16.6/19.1 |
| 100,000 | create | 507.0/9.4/14.7 → 48.5/31.6/25.3 |
| 100,000 | acquire | 588.6/15.1/24.6 → 26.0/24.8/155.7 |
| 100,000 | release idle | 26.0/14.0/24.2 → 100.5/37.7/42.4 |
| 100,000 | update error | 18.0/115.5/24.4 → 41.7/29.3/33.8 |
| 100,000 | metadata | 15.2/25.8/25.2 → 24.6/16.5/21.3 |
| 100,000 | delete | 14.9/24.0/23.9 → 22.9/14.6/20.0 |

**Backfill, audit, repair and rollback.** Admin operations are one timed execution, no separate
memory pass; scale admin memory and variance are NOT PROVEN. Every backfill completed, audit
reported zero missing/stale and marked ready. HTTP counts include marker writes/reads.

| Threads | Rebuild requests (scan / batches / marker), ms | Hints | Audit requests / GET rows, ms |
|---:|---|---:|---|
| 1,000 | 13 (1/11/1), 1681.7 | 915 | 7 / 1,917, 252.7 |
| 10,000 | 104 (10/93/1), 16318.0 | 9,033 | 24 / 19,035, 1679.7 |
| 100,000 | 1,001 (100/900/1), 147535.6 | 89,887 | 194 / 189,889, 14698.8 |

Before rollout, marker-absent searches sent 2/10/82 requests and read the entire idle set.
While building they sent 1/9/82 (the last includes a TTL refresh) and still scanned the idle set.
After the three index-aware write sequences were deleted, audit counted exactly six stale hints
and zero missing at every size. At 1k, `repair_status_index(0)` removed all six, costing
79 requests (72 GET, 6 DELETE, 1 clock PUT), 1,837 received rows and 1968.8 ms.
**Scale repair at 10k/100k was not run**: its verification cost is O(hints/14), regardless of
how few are stale. Zero grace is only a quiescent-table drill, not a recommended production value.
Disable cost one PUT at every size (9.8/11.4/10.0 ms); subsequent local reads scanned again
(1/9/81 requests). Multi-process marker TTL propagation at scale was not measured.

The existing 30 shared scenarios plus four U1/U2/U3/U5 probes ran on separate fresh real tables:
**34 passed, 54 unrelated tests deselected, 82.12 seconds**. These include default-off layout,
ordering/ties, limit/offset equivalence, fallback, create/update/lock/delete, controlled two-writer
ETag conflicts, simulated errors before/after real writes, resumable/idempotent backfill, a failed
batch and rerun, grace-period repair, repair losing to a real competing write, mixed-fleet omission
and audit demotion/rebuild, disable/delete-hints/re-enable, and vanished-row batch fallback.
The wrapper deliberately schedules interleavings in one process; Azure performs the actual CAS
and transaction operations. This is NOT sustained independent-process multi-writer testing or
real transport-loss fault injection. Existing U2 proves the **14-row-key + partition** filter
works, not whether 15 row keys plus partition are rejected. U1 proves identical upserts changed
ETag/Timestamp in the sampled calls (the implementation uses a fresh `u` regardless); U3 proves
ordering/pagination/conditional delete and transaction atomicity in its small table; U5 proves
sampled read-your-writes, not all concurrent-read schedules. **U4 hot-partition throughput remains
NOT PROVEN.**

**Exact commands and cleanup.** Run from the evidence worktree. The connection string was obtained
only in the same shell invocation as these commands, passed by environment-variable **name**, and
never printed or persisted. Provisioning outputs were suppressed or selected to safe fields.
The resource group had tags `purpose=benchmark repo=azure-functions-langgraph issue=801 owner=agent`.

```bash
set -euo pipefail
rg="rg-af-bench-801-$(date -u +%Y%m%d%H%M)"
account="stafbench801$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
cleanup() {
  unset BENCH_TABLE_CS
  az group delete --name "$rg" --yes --only-show-errors -o none
  az group wait --name "$rg" --deleted --interval 15 --timeout 1800 --only-show-errors
  az group exists --name "$rg" -o tsv
  az group list --query "[?starts_with(name,'rg-af-bench-')].name" -o tsv
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP
az account show --query '{name:name,state:state}' -o json
test "$(az account show --query state -o tsv)" = Enabled
az group create --name "$rg" --location koreacentral \
  --tags purpose=benchmark repo=azure-functions-langgraph issue=801 owner=agent \
  --only-show-errors -o none
az storage account create --name "$account" --resource-group "$rg" \
  --location koreacentral --sku Standard_LRS --kind StorageV2 \
  --allow-blob-public-access false --min-tls-version TLS1_2 --only-show-errors -o none
export BENCH_TABLE_CS="$(az storage account show-connection-string \
  --name "$account" --resource-group "$rg" --query connectionString -o tsv --only-show-errors)"
timeout 3600 hatch run table:python -m tools.benchmark_table_status_index \
  --connection-string-env BENCH_TABLE_CS --sizes 1000,10000,100000 --reps 3 \
  > /tmp/opencode/evidence801-index.json
timeout 1800 hatch run table:python tools/benchmark_table_store.py \
  --connection-string-env BENCH_TABLE_CS --sizes 1000,10000,100000 --reps 3 \
  > /tmp/opencode/evidence801-scan.md
TABLE_TEST_CONNECTION_STRING_ENV=BENCH_TABLE_CS timeout 1800 hatch run table:python \
  -m pytest -v -m integration tests/integration/test_table_store_integration.py \
  -k 'status_index_scenario or test_u1 or test_u2 or test_u3 or test_u5' \
  --no-cov --tb=short > /tmp/opencode/evidence801-scenarios.txt
```

The already logged-in default subscription was verified as `Visual Studio Enterprise Subscription`,
`Enabled`; no login/default change, role assignment or other resource was touched.
Created `rg-af-bench-801-202611241242` and its only account `stafbench8014f13bb`;
start 12:42:57 UTC, final deletion proof 13:46:31 UTC. The account's safe-field query confirmed
`koreacentral`, `Standard_LRS`, `StorageV2`, blob public access false and minimum TLS 1.2.
The EXIT trap ran synchronous group delete and wait; afterwards `az group exists` returned
**false**, and the `rg-af-bench-*` group-list command returned **no names**. Nothing remains.

Index benchmark total: **9,685 actual HTTP requests**, including table creation/deletion,
population, all measured reads/writes, admin operations and oracle-bearing reads.
Original scan benchmark total: **3,592**, of which **2,476** were its 84 query executions.
The integration suite's HTTP total was not instrumented, so **13,277 is a measured subtotal,
not the whole-session count**. Conservative planning estimate: below 50,000 Table requests;
at an illustrative $0.01/10,000 requests, below $0.05 transactions, with small storage/egress
for roughly one hour comfortably below $1 total and the $5 budget. This is an estimate,
not an invoice or a verified regional tariff; no compute charge was incurred.

**Still open for the maintainer.** Accept (or reject) the +50% run write cost, lazy stale hints,
cross-partition/non-snapshot consistency and all-writers-aware completeness requirement; decide
the experimental/support gate and whether the default offset cap and large-limit trade-off fit
real usage. In-region latency, U2's rejection boundary, U4 sustained throughput, scale repair,
admin memory, independent-process writers and broader failure schedules remain NOT PROVEN.

### Scale envelope

The bundled persistent backends are intended for development and small-to-medium production deployments. Plan ahead before pushing past these limits:

| Backend | Comfortable | Caution zone | Switch backends |
|---|---|---|---|
| `AzureBlobCheckpointSaver` | < 100 checkpoints/thread, < 10K threads | 100–1000 checkpoints/thread | Use Cosmos DB or Redis-backed checkpointer |
| `AzureTableThreadStore` | < 100K threads, light search load | 100K–500K threads | Use a sharded thread store or Cosmos DB |

Notes:

- **Single partition** — `AzureTableThreadStore` keys every thread under a single PartitionKey, capped by Azure Table per-partition throughput (~2000 entities/sec on Standard accounts). Search and count beyond `status` filtering are **client-side**; see [COMPATIBILITY.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/COMPATIBILITY.md).
- **Opt-in status index** — `AzureTableThreadStore(status_index=True)` can serve explicit-`status`, metadata-free `search()` from time-ordered index rows instead of a full scan; see [Table status index (opt-in, experimental)](#table-status-index-opt-in-experimental).
- **Prefix scans** — `AzureBlobCheckpointSaver` lists checkpoints via blob prefix scans; transaction count and latency grow with checkpoints-per-thread. Use the retention helpers below to keep that bounded.
- **Table JSON capacity** — metadata, values and interrupts use legacy single-string properties
  when they fit, or versioned `utf16-v1` chunks of at most 64 KiB per UTF-16 property.
  Legacy entities remain readable. The store rejects content before writing if the
  complete entity exceeds its conservative 1-MiB budget (including property names,
  overhead and 4 KiB reserved for service/lock properties), or Azure's 255-property
  limit. `TableStorageLimitError` is a `ValueError` with size and limit attributes:
  reduce thread data or supply a Blob-backed custom `ThreadStore`; data is never
  truncated. Conditional replacements remove obsolete chunks when documents shrink,
  preserve unrelated fields, and retry ETag conflicts up to three times; a metadata
  merge (`merge_metadata`) re-derives its result from the freshly read row on every attempt,
  so a conflicting writer's new keys are kept. Readers
  must be upgraded before writers: older package versions cannot read chunked JSON.
  Datetimes become strings, LangChain messages retain structured message dictionaries,
  and Pydantic models use JSON-mode dumps through the shared JSON encoder.

Platform wait and buffered-stream runs return a sanitized HTTP 500 when final
storage persistence or outer-lock release fails, rather than reporting completion.
The failure observer receives the actual exception; if graph execution failed
first, that primary exception is retained and cleanup failures are logged separately.
Outer-lock release is always attempted. A failed Table write can leave the row busy:
inspect storage errors and use the stale-lock recovery procedure below; do not
blindly retry a graph with non-idempotent side effects.

#### Retention helpers

`AzureBlobCheckpointSaver` exposes two helpers for scheduled cleanup (e.g. from a Timer-triggered Function):

```python
# Keep only the most recent 50 checkpoints per (thread, namespace)
saver.delete_old_checkpoints(thread_id="conversation-1", keep_last=50)

# Or delete everything older than a known checkpoint id
saver.delete_checkpoints_before(
    thread_id="conversation-1",
    before_checkpoint_id="01HXY...",
)
```

Both helpers only delete checkpoint marker, metadata, and write blobs. They intentionally preserve channel value blobs (under `values/`) and the `latest.json` pointer so retained checkpoints remain fully usable.

> **Note** — `delete_old_checkpoints` / `delete_checkpoints_before` are safe but **not exhaustive**. Channel value blobs that were referenced *only* by the now-deleted checkpoints become orphaned and are not removed. For long-running threads with frequent checkpointing, those orphans can dominate the storage footprint over time. Run `collect_orphaned_values()` (below) on a schedule as the second step.

#### Garbage-collecting orphaned channel values

After pruning checkpoints, `collect_orphaned_values()` walks the surviving checkpoints, builds the set of `(channel, version)` pairs they reference, and removes any `values/` blob outside that set. Default is **dry-run** so you can audit first:

```python
# Dry run — see what would be deleted, change nothing
audit = saver.collect_orphaned_values(thread_id="conversation-1")
print(audit.would_delete)

# Real run — actually delete orphans
result = saver.collect_orphaned_values(thread_id="conversation-1", dry_run=False)
print(f"Deleted {len(result.deleted)} orphaned value blobs")
```

The helper is concurrency-safe by two complementary mechanisms:

1. **Recent-write grace period** — value blobs whose `last_modified` is within `grace_period_seconds` (default **300s**) are deferred to a future GC pass and recorded in `result.skipped_recent`. This protects the window between a value blob being uploaded and its checkpoint commit marker (`latest.json`) being finalized.
2. **Per-orphan re-scan** — immediately before each delete, the survivor set is recomputed; an *older* value blob that a newly finalized checkpoint started referencing after the snapshot is preserved (such blobs appear in `would_delete` but not `deleted`).

Per namespace, the helper **fails closed** — if `latest.json` is missing **or** any surviving checkpoint blob is unreadable / fails deserialization, the namespace is skipped entirely (recorded in `result.skipped_namespaces`) so a misconfigured or transiently-unavailable store cannot trigger destructive deletion.

Set `grace_period_seconds=0` to disable the recent-write guard during an offline maintenance window when no concurrent checkpoint writes are possible.

#### DB checkpointer backends

For workloads that already run a managed database (or need state shared across multiple Function instances), thin DX helpers wrap the official LangGraph DB checkpoint packages without reimplementing storage:

| Backend | Helper | Extra | When to use |
| --- | --- | --- | --- |
| Postgres | `create_postgres_checkpointer` | `pip install azure-functions-langgraph[postgres]` | Production, multi-instance, existing Postgres infra |
| SQLite | `create_sqlite_checkpointer` | `pip install azure-functions-langgraph[sqlite]` | Local dev and single-instance deployments |
| Cosmos DB | `create_cosmos_checkpointer` | `pip install azure-functions-langgraph[cosmos]` | Azure-native serverless/global production |

Each helper owns the connection lifetime and emits clear ImportErrors pointing at the right extra. The Postgres and SQLite helpers accept a connection string and (by default) call upstream `setup()` on cold start so the checkpoint tables exist. The Cosmos DB helper accepts an endpoint and key, temporarily wires the upstream `COSMOSDB_ENDPOINT` / `COSMOSDB_KEY` environment variables, and directly instantiates the upstream `CosmosDBSaver`; that helper path is deprecated because unrelated threads can observe those process-global values during construction:

> **Authentication — configure upstream credentials once at process startup.** The supported `langgraph-checkpoint-cosmosdb` range (`>=0.2.0,<0.5`; verified with 0.4.0) exposes no endpoint, credential, or client constructor injection. `CosmosDBSaver` reads `COSMOSDB_ENDPOINT` and optional `COSMOSDB_KEY` directly from the process environment. Set those application settings before the Functions worker starts and instantiate the upstream saver directly. With `COSMOSDB_KEY` absent, upstream uses `DefaultAzureCredential` (Managed Identity, `az login`, or service principal). With `COSMOSDB_KEY` present, it uses key authentication.

```python
import os
from langgraph_checkpoint_cosmosdb import CosmosDBSaver

os.environ["COSMOSDB_ENDPOINT"] = "https://<account>.documents.azure.com:443/"
# Leave COSMOSDB_KEY unset → upstream uses DefaultAzureCredential (Managed Identity)
checkpointer = CosmosDBSaver(database_name="langgraph", container_name="checkpoints")
```

> Grant the Function App's identity a Cosmos DB data-plane role (e.g. *Cosmos DB Built-in Data Contributor*) on the account. For key auth, configure both `COSMOSDB_ENDPOINT` and `COSMOSDB_KEY` as startup application settings and use the same direct constructor. Do not set either value temporarily around construction: environment variables are process-global, so a lock cannot prevent unrelated threads from observing them. `create_cosmos_checkpointer` remains available for compatibility but emits `DeprecationWarning` and provides no credential-isolation guarantee.

```python
import os
from azure_functions_langgraph.checkpointers.postgres import create_postgres_checkpointer

checkpointer = create_postgres_checkpointer(
    os.environ["LANGGRAPH_POSTGRES_CONNECTION_STRING"],
    setup=True,  # set False once your deployment pipeline owns migrations
)
graph = builder.compile(checkpointer=checkpointer)
```

The helpers do not hide `builder.compile(checkpointer=...)` and do not reimplement DB checkpoint storage — they centralize connection conventions and emit clear ImportErrors pointing at the right extra. The Postgres and SQLite helpers run `setup()` once at cold start; the deprecated Cosmos DB helper directly instantiates the saver (no `setup()` call, no context manager). See [`examples/postgres_checkpoint_production/`](../examples/postgres_checkpoint_production/), [`examples/sqlite_checkpoint_local/`](../examples/sqlite_checkpoint_local/), and [`examples/cosmos_checkpoint_azure/`](../examples/cosmos_checkpoint_azure/) for full Azure-Functions wiring.

| Backend | Comfortable | Caution zone | Switch backends |
| --- | --- | --- | --- |
| `create_sqlite_checkpointer` | local dev, single-instance prod | multi-process write contention | Use Postgres |
| `create_postgres_checkpointer` | multi-instance Functions, existing Postgres infra | very high write QPS without read replicas | Add connection pooling / read replicas, or shard |
| `create_cosmos_checkpointer` | Azure-native serverless, global distribution | high RU cost with large checkpoints | Tune RU allocation, use provisioned throughput |


#### Run lock semantics

When using `AzureTableThreadStore` with Platform-compatible runs, each graph execution acquires an **atomic run lock** (ETag compare-and-swap) on the thread before invoking the graph. On completion — success or failure — the lock is released with owner-token fencing plus its own ETag compare-and-swap.

| Operation | Concurrency | Mechanism |
| --- | --- | --- |
| Lock acquisition (`try_acquire_run_lock`) | Atomic — exactly one caller wins | ETag CAS |
| Lock release (`release_run_lock`) | Atomic — owner-fenced, ETag CAS | Owner-token check + replacement with ETag CAS |

**Owner fencing and CAS retries:** If an optional `owner_token` is supplied and no longer matches the thread's current `lock_token`, or the thread is no longer `busy` (e.g. `reset_stale_locks()` already reclaimed it, or a newer run re-acquired it), the release is a no-op — the current entity is returned unchanged and a warning is logged. On an ETag conflict, release reads the latest entity and checks ownership again, for at most three conditional-write attempts. A same-owner renewal or metadata update therefore does not strand the thread after a single conflict; a newer owner is never overwritten. Successful release atomically persists the terminal status and optional values, preserves other current properties, and removes `lock_token` and `lock_expires_at` using replacement semantics (merging `None` does not delete Azure Table properties). Exhaustion raises `RunLockReleaseError` (a `RuntimeError` subclass); Platform run handlers report a sanitized HTTP 500 rather than success, while still releasing local exclusion. Missing or concurrently deleted threads raise `KeyError`.

**Renewal and ETag conflicts:** A metadata-only write (for example `PATCH /threads/{id}`) changes the row's ETag without changing its owner, so a conflict on the renewal's conditional write is not proof that ownership was lost. `renew_run_lock` therefore re-reads the row after a conflict and re-checks that its token still owns a `busy` thread before writing again, for at most three attempts, so up to two conflicting writes in a row are absorbed. A replaced token, a released thread, or one reclaimed by `reset_stale_locks()` returns `False` and writes nothing, so a newer owner's record is never overwritten. If all three attempts lose the race (the token still matched at each read), it raises `RunLockRenewalConflictError` (a `RuntimeError` subclass): whether ownership survived the last conflict is not known, the lock was neither shown to be lost nor extended, and the Platform renewal worker reports it as a failed renewal (the run ends with the sanitized HTTP 500 below, not as a success), so sustained write churn on one thread can still fail a run. The bound is on attempts, not on elapsed time: each read and write is subject only to the SDK's own timeouts, and the stop timeout below applies to a slow renewal as before. Storage errors, including a deleted row, propagate unchanged and are not retried or mistaken for lost ownership. Custom stores should likewise return `True` only after extending the lock, `False` only for lost ownership, and raise for operational errors.

**Renewal failure:** Platform runs renew store ownership every 20 seconds. A
negative renewal result or exception is retained for cleanup: wait and buffered
stream handlers return a sanitized HTTP 500 instead of reporting completion,
notify the failure observer, and do not persist final graph values. An earlier
graph error remains the primary failure. Cleanup sets the renewal stop event and joins the worker for at most
`RENEWAL_STOP_TIMEOUT` (5 seconds, a fixed module constant, not a setting). If
the worker is still blocked inside a store call and no earlier failure was
recorded, cleanup receives a terminal `RunLockRenewalStopTimeoutError` (a
`RunLockRenewalError`): the run ends with `error` status and final graph values
are not persisted, even if that slow renewal call would later have succeeded.
The blocked worker is an untracked daemon thread that exits on its own when its
call returns (at most one per run whose renewal never returns; there is no
pool). The timeout does not release store ownership: the later
`release_run_lock` remains owner-token and ETag compare-and-swap fenced, so a
late `renew_run_lock` from an abandoned worker returns `False` instead of
mutating a newer owner's record. This is verified by tests for the Table-store
fake and `InMemoryThreadStore`; custom stores must implement the same
owner-token check. Cleanup then always attempts owner-fenced store release and
outer-lock release; host cancellation propagates after cleanup. A run whose own release the store
refuses as stale is a failure too (`RunLockReleaseRejectedError`): the store returns the unchanged
current thread (busy under a newer owner, reclaimed, or finished by another run), and the handler
returns the same sanitized HTTP 500 with one failed observer event instead of reporting completion.
The bundled stores report exactly whether the release was applied through the optional
`ReleaseOutcomeProvider` capability (`release_run_lock_outcome`, which returns a
`RunLockReleaseOutcome(thread, applied)`; `release_run_lock` itself is unchanged and the bundled
implementations of the outcome method call it, so an override of it is still reached), so the
handler does not have to infer it from the returned thread. That
stays exact when a reclaim left the thread at the status the run asked for, and when another run
acquired the thread and finished before the stale release arrived: the returned thread then looks
like a normally finished one, but the release was not applied and the run fails. A custom store
without the capability is judged by the returned thread alone, so it must return the thread as it
stands after the release (the requested status, no `lock_token`); a store that returns a snapshot
taken before the write, or leaves the token on the released row, makes every run fail with that
500. Such a store cannot expose a stale release that left the thread at the requested status (a
reclaim to that status, or another owner that already finished); implementing
`release_run_lock_outcome` closes that gap. `POST /threads/{id}/state` judges its own
release the same way: a release the store refuses as stale fails the update with the
sanitized HTTP 500 `Internal error releasing thread state lock` (no observer event; the
update is not rolled back). A graph error that already failed the run
stays the reported failure; on `runs/stream` a refused release also replaces the buffered error
frame with the sanitized JSON 500, like any other release failure. Host cancellation still
propagates without a terminal observer event. Still configure
bounded Azure SDK network timeouts: Python cannot interrupt a hung synchronous
SDK or graph call, so a stuck call keeps its thread. An orphaned `busy` thread is
recovered with `reset_stale_locks()`. This does not roll back side effects, so
graph operations must remain idempotent. A newer owner's store lock is never cleared.

**What can still go wrong:** Owner fencing and CAS protect against *concurrent* writers, not against a writer that never arrives. If a Function host instance is terminated during graph execution (scale-in, deployment, crash), **no** release call is ever made — there is no owner-token or CAS check to lose, because `release_run_lock` is simply never invoked — so the thread remains in `busy` status indefinitely.

**Recovery:** Use `reset_stale_locks()` from a periodic Timer Trigger to reclaim orphaned locks:

```python
# Reset threads stuck in 'busy' for more than 10 minutes
count = thread_store.reset_stale_locks(older_than_seconds=600)
```

Each candidate is re-read and re-verified (still `busy`, still older than the threshold), and the reset replaces the row without its `lock_token` and `lock_expires_at` using ETag CAS. That **revokes the previous owner**: a worker that comes back later fails the owner-token check, so its release cannot undo the recovery status or publish stale `values`, and its renewal returns false. A thread that was renewed, released or re-acquired since the scan is never stomped. Choose `older_than_seconds` comfortably above your longest expected graph execution time. A live Platform run refreshes its `updated_at` every 20 seconds through its renewal worker, so the threshold mainly bounds how long a dead owner's lock survives; a holder that does not renew, or whose renewal is blocked or failing, can still be reclaimed while it is working if the threshold is too short. ETag CAS protects against re-acquire races, not against that. See [`examples/maintenance_timer/`](../examples/maintenance_timer/) for a complete Timer Trigger wiring.

> **Stale-lock cleanup caveat:** `reset_stale_locks` issues a projection query (`select=["RowKey", "updated_at"]`) to find candidates, then re-reads each one and relies on the Azure Tables SDK to expose that row's ETag via either `entity.metadata["etag"]` or `entity["etag"]`. Rows where **neither shape** populates an ETag are skipped (logged at DEBUG) so a stale lock is never reset without a writable ETag for CAS; such rows are retried on the next scan once the SDK returns a usable ETag. Revoking the token does not stop the old graph and does not fence Blob checkpoint writes. A row that an earlier release of this library reclaimed (status `error` or `idle` but still carrying the old `lock_token`) is protected as well: release and renewal also require the thread to be `busy`.

#### Native endpoint thread locking

Native invoke/stream endpoints (`POST /api/graphs/{name}/invoke` and `.../stream`) use an **in-process per-thread lock** when the graph has a checkpointer and the request includes `config.configurable.thread_id`. This prevents concurrent writes to single-writer checkpointers (e.g. `AzureBlobCheckpointSaver`) within the same Python worker process.

> **Important:** The **default** backend is **single-process only**. For shared
> checkpoint writers across workers/hosts, use a distributed `ThreadLock`.
> `AzureTableThreadStore` adds Platform run-lock CAS, but native, Durable and
> Service Bus runs do not acquire that store lock. Table storage alone cannot
> coordinate mixed surfaces.


#### Distributed thread locking (v0.6+)

The `thread_lock` parameter accepts any object satisfying the [`ThreadLock`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/src/azure_functions_langgraph/locks/base.py) protocol, so multi-instance deployments can swap the default `InProcessThreadLock` for a distributed backend. The package ships with `AzureBlobLeaseThreadLock`, which uses **Azure Blob lease compare-and-swap** as the underlying primitive — no additional infra beyond a Blob container.

```python
import azure.functions as func
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContainerClient

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

container = ContainerClient.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", "langgraph-locks"
)
try:
    container.create_container()
except ResourceExistsError:
    # Expected on every cold start after the first: create_container() is a
    # create operation, not an idempotent get-or-create -- the Azure Storage
    # SDK raises when the container already exists. In production, provision
    # "langgraph-locks" once via your deployment infrastructure (Bicep/ARM/
    # Terraform) instead of relying on app-startup creation; this try/except
    # only keeps the sample runnable standalone. Any other storage error
    # (e.g. a permissions failure) is intentionally left to propagate.
    pass

app = LangGraphApp(
    auth_level=func.AuthLevel.FUNCTION,
    thread_lock=AzureBlobLeaseThreadLock(container_client=container),
)
```

| Backend | Distributed? | Infra required | Use case |
| --- | --- | --- | --- |
| `InProcessThreadLock` (default) | No — single Python worker only | None | Local dev, single-instance deployments |
| `AzureBlobLeaseThreadLock` | Yes — Azure Blob lease CAS | One Blob container | Multi-instance production (Flex Consumption / Elastic Premium) |
| Custom `ThreadLock` implementation | Yours to define | Yours to provide | Redis, Cosmos DB, Postgres advisory locks, etc. |

**Opt-in write guard.** `AzureBlobLeaseThreadLock(guard_checkpoint_writes=True)` hands each run a guard bound to its own lock token, and `AzureBlobCheckpointSaver` rejects checkpoint writes with `ThreadLeaseLostError` once the lock has *detected* loss (`AzureBlobCheckpointSaver(require_ownership_guard=True)` additionally fails closed on writes that carry no guard). It is off by default and is **not fencing**: detection lags and a check can pass just before an upload. Contract, limits and operating guidance: [production guide](production-guide.md#opt-in-guard-for-blob-checkpoint-writes-790).

**Safety guard — `AZFUNC_LANGGRAPH_LOCK_BACKEND`.** Set this environment variable to `distributed` in multi-instance environments to fail-fast at startup if a distributed backend was not wired. When the value is `distributed` (or any non-empty value other than `inprocess`) and `thread_lock` resolves to the default `InProcessThreadLock`, `LangGraphApp.__post_init__` raises `RuntimeError` — this prevents accidentally deploying an in-process lock to a horizontally-scaled Function App. See [production guide](production-guide.md#distributed-thread-locking) for the full scale-out matrix.

### Upgrading

Sections are in ascending order. **Upgrading to 0.13.0? Read the "0.12.x → 0.13.0"
section at the end first; it contains breaking changes.**

#### v0.3.0 → v0.4.0

Fully backward-compatible. No breaking changes.

- **New optional extras**: `pip install azure-functions-langgraph[azure-blob,azure-table]` for persistent storage
- **New platform endpoints**: thread CRUD, state update/history, threadless runs, assistants count
- **New protocols**: `UpdatableStateGraph`, `StateHistoryGraph` (available from `azure_functions_langgraph.protocols`)

#### v0.4.0 → v0.5.0

Fully backward-compatible. No breaking changes.

- **Metadata API**: `app.get_app_metadata()` returns an immutable snapshot of all registered routes and graph info
- **OpenAPI bridge**: `azure_functions_langgraph.openapi.register_with_openapi` integrates with `azure-functions-openapi-python`
- **CloneableGraph protocol**: thread-isolated graph cloning for safe concurrent execution

#### 0.12.x → 0.13.0

0.13.0 contains **three breaking changes**. Each fails loudly (an HTTP 400 or a
startup `RuntimeError`) instead of changing behavior silently. The remaining
items are behavior changes worth checking and opt-in additions.

**Breaking changes**

- **Classic native client configuration is restricted (#740).** `LangGraphApp`
  native invoke and stream routes accept only `config.configurable`, and only
  `thread_id` by default. A request that sends anything else (execution controls
  such as `recursion_limit` or callbacks, unknown top-level keys, or custom
  configurable fields) now gets a sanitized HTTP 400 before authorization, locking
  or graph execution. Bind trusted defaults on the server with
  `graph.with_config(...)`, and allow the application fields that clients may
  choose with
  `app.register(graph, "agent", allowed_configurable_keys=frozenset({"locale"}))`.
  Allowing `checkpoint_id` or `checkpoint_ns` grants checkpoint selection and
  needs ownership checks in your authorizer. Details: [Request format](#request-format)
  and [configuration](configuration.md#native-client-configuration-admission).
- **True-stream native configuration is restricted the same way (#770).**
  `StreamingLangGraphApp` invoke and stream apply the same admission policy and
  the same migration. Platform, Durable and Service Bus transports are not affected.
- **Platform routes need a shared thread store when a scale-out guard is set (#742).**
  With `platform_compat=True`, setting `AZFUNC_LANGGRAPH_LOCK_BACKEND` to any value
  other than `inprocess` (for example `distributed`) now makes `app.function_app`
  raise `RuntimeError` while the thread store is still the default
  `InMemoryThreadStore`; before, only `AZFUNC_LANGGRAPH_THREAD_STORE` did. A
  distributed lock alone does not share thread records. Assign a shared store such
  as `AzureTableThreadStore` to `app.thread_store` before accessing
  `app.function_app`, or unset both variables for single-process local
  development. See [Persistent storage](#persistent-storage-v04).

**Behavior changes to check**

- **Native `RUN` authorization (#722).** The `authorize=...` hook is now also
  evaluated with `AuthorizationAction.RUN` for native sync and async invoke and
  buffered stream routes, before the graph runs. A policy that does not allow
  `RUN` now denies those runs. See [security](security.md).
- **Buffered stream frames (#731).** Non-dictionary events are encoded as plain
  JSON instead of the old `{"data": "<Python repr>"}` envelope; JSON-decode each
  frame's `data:` value rather than assume an object. See
  [Streaming behavior](#streaming-behavior).
- **Azure Table chunked JSON (#728).** Large thread documents can be written as
  chunked properties. Upgrade every reader before any writer: older package
  versions cannot read chunked JSON. See [Persistent storage](#persistent-storage-v04).
- **Cosmos DB emulator (#721).** With `create_cosmos_checkpointer`,
  `COSMOS_DISABLE_SSL` is honored only for loopback endpoints (`localhost`,
  `127.0.0.1`, `::1`). For any other host, such as a Docker network name, it is
  ignored and TLS verification stays on; use a trusted certificate
  (`REQUESTS_CA_BUNDLE`) or a loopback endpoint. An ambiguous endpoint raises
  `ValueError`.

**Opt-in additions (no action needed)**

- `StreamingLangGraphApp(authorize=...)` for true-stream routes (#782).
- Service Bus `session=True` and `session_exclusive=True` (#754, #757). The default
  path and Basic-tier support are unchanged.

**A limit that is unchanged.** Blob marker leases do not fence checkpoint writes
after lease loss. See "Operating envelope for long-running graphs" in the
[production guide](production-guide.md#operating-envelope-for-long-running-graphs-characterized-765).

## When to use

- You have LangGraph agents and want to deploy them on Azure Functions
- You want serverless deployment without LangGraph Platform costs
- You need HTTP endpoints for your compiled graphs with minimal setup
- You want thread-based conversation state via LangGraph checkpointers
- You need durable state persistence with Azure Blob/Table Storage

## Documentation

- **Start here:** [5-minute tutorial](tutorial-5-min.md) — compile a graph → expose as HTTP → run locally → deploy to Azure
- Project docs live under `docs/`
- Smoke-tested examples live under `examples/`
- Product requirements: `PRD.md`
- Design principles: `DESIGN.md`

## Ecosystem

This package is part of the **Azure Functions Python DX Toolkit**.

**Design principle:** `azure-functions-langgraph` owns LangGraph runtime exposure. `azure-functions-validation-python` owns validation. `azure-functions-openapi-python` owns API documentation.

| Package | Role |
|---------|------|
| [azure-functions-openapi-python](https://github.com/yeongseon/azure-functions-openapi-python) | OpenAPI spec generation and Swagger UI |
| [azure-functions-validation-python](https://github.com/yeongseon/azure-functions-validation-python) | Request/response validation and serialization |
| [azure-functions-db-python](https://github.com/yeongseon/azure-functions-db-python) | SQLAlchemy-powered DB integration helpers (poll-based pseudo trigger, input/output/client injection) |
| **azure-functions-langgraph-python** | LangGraph deployment adapter for Azure Functions |
| [azure-functions-scaffold-python](https://github.com/yeongseon/azure-functions-scaffold-python) | Project scaffolding CLI |
| [azure-functions-logging-python](https://github.com/yeongseon/azure-functions-logging-python) | Structured logging and observability |
| [azure-functions-doctor-python](https://github.com/yeongseon/azure-functions-doctor-python) | Pre-deploy diagnostic CLI |
| [azure-functions-cookbook-python](https://github.com/yeongseon/azure-functions-cookbook-python) | Dogfood examples — runnable recipes that exercise the full toolkit |

## For AI Coding Assistants

If you are an AI coding assistant (Copilot, Cursor, Claude, etc.), see:

- [`llms.txt`](llms.txt) — Concise package summary and API overview
- [`llms-full.txt`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/llms-full.txt) — Complete API reference with signatures, patterns, and examples

## Disclaimer

This project is an independent community project and is not affiliated with,
endorsed by, or maintained by Microsoft or LangChain.

Azure and Azure Functions are trademarks of Microsoft Corporation.
LangGraph and LangChain are trademarks of LangChain, Inc.

## License

MIT
