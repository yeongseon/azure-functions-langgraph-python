# Official LangGraph example on Azure Functions

Proof that a graph copied from the **official LangGraph documentation** can be
hosted by `LangGraphApp` without rewriting its nodes, edges or state schema.
This is an *official-example adaptation*, not a custom example like the others
in [`examples/`](../README.md), and it is adoption evidence, not an adapter
feature. For the default deployment path see the
[5-minute tutorial](../../docs/tutorial-5-min.md).

## Source and attribution

| Item | Value |
| --- | --- |
| Upstream page | <https://docs.langchain.com/oss/python/langgraph/graph-api-state> ("Define and update state", retrieved 2026-11-05) |
| Upstream source repo | <https://github.com/langchain-ai/docs>, pinned to commit `42bc913ad89c7f288667184ae9fc485d9fa28023` (`main` at retrieval; the page and snippets were last changed in `6072057f63c7a5d6bc71ec306accbf8df6f39c39`) |
| Copied | The page's three Python snippets `langgraph-graph-api-state-{define,update-node,build-graph}-py` (`src/snippets/code-samples/`), verbatim, in page order |
| Not copied | The final `graph.invoke(...)` snippet: invoking is the host's job |
| License | MIT, Copyright (c) 2025 LangChain. The notice and attribution are kept in [`upstream_graph.py`](upstream_graph.py) |
| Why this graph | Standalone `StateGraph`, one node, no model call, no API key, deterministic output |

## Upstream baseline (original LangGraph run)

### Current local wheel evidence (2026-11-25)

The repeatable checker now includes this graph and the
[official conversation graph](../official_langgraph_conversation/). See that
README for the exact published/candidate commands, owned-process cleanup and
artifact layout. The three pinned source snippets above were fetched again
from their immutable raw GitHub URLs on 2026-11-25 before running the baseline.
Nodes, edges, schema and the original compile call remain unchanged.

**Plan for this table: local Functions host only.** The Azure lane stays opt-in:
nothing here adds an Azure step to CI or to the release gate, which needs a
separate maintainer decision. The manual Azure runs under
[Azure evidence](#azure-evidence) (2026-11-05) and
[Azure re-verification](#azure-re-verification-2026-11-26) (2026-11-26) are
separate, one-off evidence, not release certification.

| Item | Observed result |
| --- | --- |
| Date / Python | 2026-11-25 / 3.11.15 |
| Adapter | Published PyPI **0.14.2** (`requirements.txt` unchanged) and separately built candidate wheel **0.14.2** |
| Dependencies | `azure-functions==1.26.0`, `langgraph==1.2.24`, `langchain==1.5.2`, `langchain-core==1.8.4`, `langgraph-checkpoint==4.3.2` |
| Baseline / handler | Independent pinned snippets executed; both yield `Hi`, `Hello!`, `extra_field=10` |
| Real local host | Core Tools **4.8.0**, `func start --python --port 7571 --enableAuth` |
| Local storage | Azurite **3.35.0**, explicit Blob/Queue/Table connection on **15000/15001/15002** |
| Keyless first attempt | **401**, both lanes |
| Authorized first invoke | **200**, both lanes; `Hi`, `Hello!`, `extra_field=10` |
| Cleanup | Owned hosts/emulators stopped; temporary keys and raw logs deleted |

The runtime inventories and baseline/HTTP results are recorded per example by
`scripts/certify_primary_examples.py`. The checker installs into throwaway
virtualenvs and asserts that imports come from the installed package, not `src/`.
Regular offline unit tests never install packages or start these processes;
the live local-host option requires `--local-host` explicitly.
Existing-app insertion (#994 item 3) is a follow-up to #993 / Draft PR #998.

### Historical baseline (2026-11-05)

Scratch virtualenv, Python 3.11.15, `langgraph==1.2.20`, `langchain==1.4.9`,
`langchain-core==1.7.6` (current releases on 2026-11-05), no Azure involved:

```python
result = graph.invoke({"messages": [HumanMessage("Hi")]})
```

```text
{'messages': [HumanMessage(content='Hi', ...), AIMessage(content='Hello!', ...)], 'extra_field': 10}
```

The same graph also accepts the JSON-shaped input used below
(`{"messages": [{"role": "human", "content": "Hi"}]}`); it keeps the dict
as-is because this state schema has no message reducer.

## What changed (and what did not)

The hosted module [`upstream_graph.py`](upstream_graph.py) differs from the
upstream snippets only by a license header, three provenance comments and the
removed `invoke` call:

```diff
--- upstream (as run for the baseline)
+++ upstream_graph.py (hosted)
@@ -1 +1,2 @@
+# --- upstream: langgraph-graph-api-state-define-py ---------------------------
 from langchain.messages import AnyMessage
@@ -9,2 +10,3 @@
 
+# --- upstream: langgraph-graph-api-state-update-node-py ----------------------
 from langchain.messages import AIMessage
@@ -18,2 +20,3 @@
 
+# --- upstream: langgraph-graph-api-state-build-graph-py ----------------------
 from langgraph.graph import StateGraph
@@ -24,7 +27 @@
 graph = builder.compile()
-
-
-from langchain.messages import HumanMessage
-
-result = graph.invoke({"messages": [HumanMessage("Hi")]})
-print(result)
```

State, node, edge and compile call are untouched. The intentional additions
are exactly:

- [`function_app.py`](function_app.py): the registration shim
  (`LangGraphApp().register(graph=graph, name="official_basic")`).
- [`requirements.txt`](requirements.txt): the Azure Functions dependencies;
  `langchain` (not only `langchain-core`) because the upstream snippet imports
  `langchain.messages`.
- `host.json` and `local.settings.json.example`: standard Functions config.

[`tests/test_official_langgraph_basic_example.py`](../../tests/test_official_langgraph_basic_example.py)
keeps this claim honest in CI: it pins the upstream snippets, asserts they
appear verbatim in the hosted module, runs the pristine snippets as the
baseline, and compares the registered handler's result with it.

## Run locally

```bash
cd examples/official_langgraph_basic
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

```bash
curl http://localhost:7071/api/health

curl -X POST http://localhost:7071/api/graphs/official_basic/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Hi"}]}}'
```

Observed output (published `azure-functions-langgraph==0.12.2`, Core Tools
4.8.0, Python 3.11), sanitized and pretty-printed:

```json
{
  "output": {
    "messages": [
      {"role": "human", "content": "Hi"},
      {"content": "Hello!", "type": "ai", "tool_calls": [], "...": "..."}
    ],
    "extra_field": 10
  }
}
```

The `messages` and `extra_field` values equal the baseline. The route is the
documented `POST /api/graphs/{name}/invoke`; `POST .../stream` returns
**buffered** SSE (`event: data` frames followed by `event: end`), not
incremental tokens. Experimental true streaming is a separate transport, see
[`true_streaming_agent`](../true_streaming_agent/).

## Deploy to Azure

Follow [Step 5 of the 5-minute tutorial](../../docs/tutorial-5-min.md)
unchanged. Deltas for this example only:

- Run `func azure functionapp publish` from `examples/official_langgraph_basic`
  and create `local.settings.json` first (copy the `.example`); Core Tools need
  it to detect the Python runtime.
- Call `/api/graphs/official_basic/invoke` instead of `simple_agent`, with the
  payload above.

Deployed routes use function-level auth: a request without a key gets `401`,
and the key goes in the `x-functions-key` header or `?code=`. `func start`
enforces no keys locally unless `--enableAuth` is supplied (as in the repeatable
local check above). A function key is a shared secret for the whole
function app; it does not identify a user and it does not authorize access to
a thread or run. Do not infer per-user or per-thread authorization from it;
see [security](../../docs/security.md).

### Azure evidence

Run on 2026-11-05 by the maintainer-authorized verification run, from the
example files at commit `5151e7c` of this branch (the README was added later;
no deployed file changed). Temporary resources, deleted afterwards.

| Item | Value |
| --- | --- |
| Plan / runtime | Flex Consumption, Python 3.11, `koreacentral`, Functions v4 |
| Packages | `requirements.txt` as shipped; remote build resolved `azure-functions-langgraph>=0.12.0,<0.13` at build time (`0.12.2` was the latest release; the exact resolved version was not captured) |
| Deploy | `func azure functionapp publish <APP>` (Core Tools 4.8.0, Azure CLI 2.83.0), as in the tutorial |
| `GET /api/health` (no key) | `200`, `{"status":"ok"}` |
| `POST /api/graphs/official_basic/invoke` without key | `401` |
| Same call with the function key (`x-functions-key`) | `200`; `messages` = `Hi`, `Hello!`, `extra_field` = `10` (equal to the baseline) |
| `POST /api/graphs/official_basic/stream` with key | `200`, 3 SSE events (two `data` frames, one `end`) |
| Cleanup | Resource group deleted; `az group exists` returned `false`; no tagged resources remained |

This is one deployment of the published package line on one plan and region,
exercising only the routes above. It does not certify other plans, true
streaming, thread storage or multi-user authorization.

### Azure re-verification (2026-11-26)

Run on 2026-11-26 UTC by the maintainer-authorized verification run for #994,
from the files in this directory as shipped, with the published
`azure-functions-langgraph==0.14.2` pinned in `requirements.txt`. It follows the
CLI path of [Step 5 of the tutorial](../../docs/tutorial-5-min.md) (Storage
account, Flex Consumption app, remote-build publish). Differences from the
tutorial's literal commands: `local.settings.json` was created from the
`.example` file first, as described above, and `--python --build remote` was
added to `func azure functionapp publish`. One temporary resource group,
deleted afterwards. An earlier deployment of this example the same day was
discarded because of a defect in the verification script (not in the app) and
retained no evidence; the table is the repeat on a fresh resource group.

| Item | Value |
| --- | --- |
| Plan / runtime | Flex Consumption, Python 3.11, `koreacentral`, Functions v4 |
| Tools | Azure CLI 2.83.0, Core Tools 4.8.0: `func azure functionapp publish <APP> --python --build remote` |
| Adapter | Published PyPI **0.14.2**, installed by the remote build from `requirements.txt` unchanged (pinned) |
| Other packages | The Azure build log does not print resolved versions. A `uv pip compile` of the shipped requirements (Python 3.11, manylinux x86_64) within the hour of the deployments gives `azure-functions==1.26.0`, `langgraph==1.2.24`, `langchain==1.5.2`, `langchain-core==1.8.5`, `langgraph-checkpoint==4.3.2`: a reproduction of the resolution, not the deployed inventory |
| `GET /api/health` (no key) | `200` |
| `POST /api/graphs/official_basic/invoke` without key | `401` |
| Same call with the function key (`x-functions-key`) | `200`; `messages` = `Hi`, `Hello!`, `extra_field` = `10` (equal to the baseline) |
| `POST /api/graphs/official_basic/stream` with key | `200`, 3 SSE frames (two `data`, one `end`) |
| Cleanup | Resource group deleted and the deletion awaited; no resource group or tagged resource from the run remained (see the side effect below) |

Side effect to know about: `az functionapp create` creates an Application
Insights component unless `--disable-app-insights` is passed. These runs also
left a **default Log Analytics workspace in a separate
`DefaultResourceGroup-<region code>` resource group**: no such group existed
beforehand, and the workspace's creation time falls inside the first
`az functionapp create` of the runs. Deleting the example's resource group does
not remove it, so the tutorial's one-command clean-up leaves it behind; it was
deleted separately afterwards. Anything else in the subscription that uses the
default workspace shares it, so delete it only after checking that nothing does.

This is again one deployment on one plan and region, exercising only the routes
above. First requests after a deployment can include a cold start, so no latency
is recorded. It does not certify other plans, true streaming, thread storage or
multi-user authorization.

## Known differences and limitations

- **Input type**: upstream passes a `HumanMessage`; HTTP bodies are JSON, so
  the hosted call passes a message dict. Neither LangGraph (no reducer in this
  schema) nor the adapter converts it, so the first message stays a dict in
  the output. Native `HumanMessage` input is covered by the test.
- **Output shape**: JSON cannot carry message objects. Historical `0.12.2`
  returns the AI message as a flat object on `invoke`; the tested `0.14.2`
  line (after #730) wraps it as `{"type": "ai", "data": {...}}`
  like the stream route already does. The text content is identical.
- **Streaming** is buffered SSE; the graph has one node, so you see the input
  state then the final state.
- No checkpointer is configured, so there is no thread state to inspect and
  runs are stateless.

## Findings

- **Reused as is**: the entire graph (state, node, edge, compile) and the
  registration API. No adapter change was needed and no adapter defect was
  reproduced.
- **Friction 1**: the snippet imports `langchain.messages`, so `langchain`
  must be in `requirements.txt` in addition to `langgraph`.
- **Friction 2**: `func azure functionapp publish` fails with "Can't determine
  project language" until `local.settings.json` exists. Fix: copy the example.
- **Friction 3**: JSON clients cannot send message objects; see the input and
  output differences above.
- **Friction 4** (2026-11-26): the tutorial's one-command clean-up leaves the
  default Log Analytics workspace that `az functionapp create` created; see
  [Azure re-verification](#azure-re-verification-2026-11-26).

## See Also

- [5-minute tutorial](../../docs/tutorial-5-min.md)
- [Examples index](../README.md)
- [Adapter reference](../../docs/adapter-reference.md)
