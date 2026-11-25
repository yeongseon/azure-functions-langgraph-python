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

Start Azurite in a separate terminal (these non-default ports avoid the usual
emulator instance):

```bash
azurite --silent --location /tmp/opencode/azurite-blueprint \
  --blobPort 14000 --queuePort 14001 --tablePort 14002
```

Configure **only your local process environment**, not checked-in credentials:

- `FUNCTIONS_WORKER_RUNTIME=python`
- `AzureWebJobsStorage`: the explicit Azurite emulator connection string, with
  `BlobEndpoint=http://127.0.0.1:14000/devstoreaccount1`,
  `QueueEndpoint=http://127.0.0.1:14001/devstoreaccount1`, and
  `TableEndpoint=http://127.0.0.1:14002/devstoreaccount1`, plus the emulator's
  documented development account/key. `UseDevelopmentStorage=true` alone uses
  default ports and is not sufficient for this setup. See
  [Azurite connection strings](https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azurite#connection-strings).

From this example directory, with your Python environment active:

```bash
func start --port 7471
```

From another terminal:

```bash
curl --fail http://localhost:7471/api/existing
curl --fail http://localhost:7471/api/health
curl --fail http://localhost:7471/api/graphs/official_basic/invoke \
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
liveness. Built-wheel real-Azure verification is still pending under #993,
using the safe cleanup work tracked by #978.
