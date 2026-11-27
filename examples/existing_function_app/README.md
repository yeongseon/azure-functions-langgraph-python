# LangGraph in an existing Functions app

Keep an existing Python v2 `FunctionApp`, its HTTP trigger and its timer trigger,
then attach native graph routes with the **Experimental**
`LangGraphApp.to_blueprint()` API. This example requires a build containing that
method; the currently published package may not contain it yet. Install the
candidate wheel into your local environment before running this example.

## What changes

The HTTP function at `GET /api/existing` and the five-minute timer are defined
first. The timer body is deliberately a no-op: replace it with your existing
application job. The only LangGraph integration is:

```python
langgraph_app = LangGraphApp()
langgraph_app.register(graph=graph, name="official_basic")
app.register_functions(langgraph_app.to_blueprint())
```

Do not export `langgraph_app.function_app` alongside `app`. The worker needs one
application instance. Complete graph registration before export; do not register
the same Blueprint twice. The [composition guide](../../docs/usage.md#existing-app-composition-experimental)
describes naming, health endpoint conflicts, timing, authorization and topology.

## Unchanged upstream graph

`graph.py` contains the same three snippets as
[`official_langgraph_basic/upstream_graph.py`](../official_langgraph_basic/upstream_graph.py):
LangChain's [Define and update state](https://docs.langchain.com/oss/python/langgraph/graph-api-state)
guide, pinned to `langchain-ai/docs@42bc913ad89c7f288667184ae9fc485d9fa28023`.
Its MIT notice is included. No nodes, state fields, edges or compile call change;
only Functions hosting differs. It makes no model calls and needs no cloud keys.

For input `{"messages":[{"role":"human","content":"Hi"}]}`, the baseline
keeps that input message, appends an AI message with content `Hello!`, and sets
`extra_field` to `10`. The adapter wraps the result in `output` and serializes the
AI message using LangChain's `message_to_dict`. The smoke test compares the whole
serialized output with an independently executed pinned upstream baseline.

## Run locally

Prerequisites: Python 3.11+, Azure Functions Core Tools v4, Azurite, and a local
environment with the candidate adapter wheel and `requirements.txt` dependencies.
Build the candidate from the repository root with `make build`; use your normal
isolated environment to install that wheel rather than replacing a shared SDK.

Start Azurite in a separate terminal (default ports, scratch data directory):

```bash
azurite --silent --location .azurite
```

Configure **only your local process environment**, not checked-in credentials:

- `FUNCTIONS_WORKER_RUNTIME=python`
- `AzureWebJobsStorage=UseDevelopmentStorage=true`

From this example directory, with your Python environment active:

```bash
func start
```

From another terminal:

```bash
curl --fail http://localhost:7071/api/existing
curl --fail http://localhost:7071/api/health
curl --fail http://localhost:7071/api/graphs/official_basic/invoke \
  -H 'Content-Type: application/json' \
  -d '{"input":{"messages":[{"role":"human","content":"Hi"}]}}'
```

The first response must be `existing HTTP still works`; the graph result must
match the upstream baseline above. Stop **both** the Functions host and your
Azurite process with Ctrl-C when finished. Do not terminate other local hosts.

Expected discovery: `existing_http`, `existing_timer`, `aflg_health`,
`aflg_health_details`, `aflg_official_basic_invoke`,
`aflg_official_basic_stream`, `aflg_official_basic_state`. State is discoverable
because the compiled graph supports it; without a checkpointer it does not
provide persistent thread state. Streaming is buffered SSE.

## Validation scope

`tests/test_existing_function_app_example.py` loads this entrypoint using the
repository's isolated example loader. It runs in source and installed-wheel
test lanes without importing `src/` directly. The existing HTTP response, timer
binding and complete route inventory are asserted alongside upstream parity.

Local Core Tools/Azurite results are recorded in the implementing PR. They do
not certify a deployed Azure plan or timer recovery. Core Tools does not enforce
function keys by default; deployed HTTP routes require keys, except anonymous
liveness.

## Azure evidence (2026-11-27)

One maintainer-authorized manual deployment for #993, not an automated lane, a
release certification or a claim about other plans. It deployed a copy of this
directory with the adapter built from `main` at `bd01cc2` (wheel
sha256 `d4f8568bc27c623fb8d03c7e9b48c34399bb015d48549bafd959f129fdf25a82`, `0.14.2`, built with `uv build --wheel`) bundled under
`wheels/` and referenced from `requirements.txt` by local path. `to_blueprint()`
is not in the published `0.14.2`, so a successful registration also shows the
bundled wheel, not PyPI, was installed. One temporary resource group, deleted
afterwards.

| Item | Value |
| --- | --- |
| Plan / runtime | Flex Consumption, Python 3.11, `koreacentral`, Functions v4 |
| Tools | Azure CLI 2.83.0, Core Tools 4.8.0: `func azure functionapp publish <APP> --python --build remote` |
| App setup | `az functionapp create ... --disable-app-insights true`, so no Application Insights or default Log Analytics workspace was created |
| Trigger inventory | 7 functions: `existing_http` (HTTP), `existing_timer` (timer, `0 */5 * * * *`, `runOnStartup=false`, `useMonitor=true`), `aflg_health`, `aflg_health_details`, `aflg_official_basic_invoke`, `aflg_official_basic_stream`, `aflg_official_basic_state` |
| `GET /api/health` (no key) | `200`, `{"status":"ok"}` |
| `GET /api/existing` without key / with function key | `401` / `200`, `existing HTTP still works` |
| `POST /api/graphs/official_basic/invoke` without key / wrong key | `401` / `401` |
| Same call with the function key | `200`; `messages` = `Hi`, `Hello!`, `extra_field` = `10` (equal to the upstream baseline) |
| `POST /api/graphs/official_basic/stream` with key | `200`, 3 SSE frames (two `data`, one `end`) |
| Cleanup | Resource group deleted and the deletion awaited; `az group exists` returned `false`; no tagged resource remained and no `DefaultResourceGroup-*` was created |

Not exercised: a timer firing, timer recovery after a host restart, thread
storage, other plans or regions, and multi-user authorization. First requests
can include a cold start, so no latency is recorded.
