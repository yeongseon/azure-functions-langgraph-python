# Functions and Foundry: bounded paired measurement

For [#996](https://github.com/yeongseon/azure-functions-langgraph-python/issues/996),
three deterministic upstream graphs were sent through two Functions hosts on
2026-11-28 UTC. Foundry provisioning reached a project, then stopped at a
data-plane permission denial. **There is no successful Foundry hosted-agent
measurement and no performance ranking.** Raw sanitized output excerpts are in
[the measurement notes](evidence/functions-foundry-996.txt).

## What was already proven

- [#994](https://github.com/yeongseon/azure-functions-langgraph-python/issues/994):
  the [basic](https://github.com/yeongseon/azure-functions-langgraph-python/tree/main/examples/official_langgraph_basic) and
  [conversation](https://github.com/yeongseon/azure-functions-langgraph-python/tree/main/examples/official_langgraph_conversation) graphs
  passed upstream baselines, installed-wheel checks and standalone adapter Azure
  calls. Those standalone hosts use the adapter, not a handwritten wrapper.
- [#1056](https://github.com/yeongseon/azure-functions-langgraph-python/pull/1056):
  the [existing-app example](https://github.com/yeongseon/azure-functions-langgraph-python/tree/main/examples/existing_function_app#azure-evidence-2026-11-27)
  discovered all seven original/graph functions and preserved its HTTP trigger.
  This run reused that Blueprint insertion, rather than creating another API.
- [#995](https://github.com/yeongseon/azure-functions-langgraph-python/issues/995):
  the existing-app retry established live owner/non-owner admission and denial
  non-mutation. Its accepted restart did **not** establish worker turnover; its
  telemetry correlation was not executed. Those gaps are not filled here.

The missing work was an explicitly handwritten Functions control, first-call
timings, a state schema without `messages`, and the Foundry path. Existing
successful cloud calls were not rerun solely to reproduce earlier evidence.

## Same graph inputs and provenance

| Graph | Pinned upstream / changes outside graph logic | Input / expected output |
| --- | --- | --- |
| `official_basic` | Existing MIT-attributed graph, docs commit `42bc913ad89c7f288667184ae9fc485d9fa28023`; nodes, edges, schema and compile unchanged | `messages=[{"role":"human","content":"Hi"}]` → `Hi`, `Hello!`, `extra_field=10` |
| `official_conversation` | Existing memory guide at `15c95b5f33287dff462acb53e1f34aaed69d5c21`; same `call_model` body, `MessagesState`, nodes and edges; existing state-aware fake replaces paid model, Blob saver replaces Postgres | Thread `paired-a`: `Hi` then `Continue` → observed human-turn counts `1` then `2`; `paired-b`: `Separate` → count `1` |
| `official_schema` | [Define input and output schemas snippet](https://github.com/langchain-ai/docs/blob/42bc913ad89c7f288667184ae9fc485d9fa28023/src/snippets/code-samples/langgraph-graph-api-state-input-output-py.mdx), MIT Copyright (c) 2025 LangChain; only final print/invoke removed and attribution added | `{"question":"hi"}` → `{"answer":"bye"}`; input/output/internal schemas contain no `messages` |

The last graph adds the missing non-conversational boundary; `official_basic`
still has a `messages` field and would not satisfy it alone. The exact fetched
snippet is preserved in the notes. No nodes or schema fields were adapted to
make either Functions host accept it. No paid model was deployed or called.

## Experiment setup and measurement boundary

Source: `origin/main` at `07a2967980d2bdebfb79b4ff7a773bf3e1a35ea5`.
`uv build --wheel` produced adapter `0.14.2`, SHA256
`fca98285bc97dd2929674e329f3343ba8eb3404a4512eceb2ab433102dd98be3`.
Each scratch app bundled that wheel under `wheels/`; requirements were
`./wheels/azure_functions_langgraph-0.14.2-py3-none-any.whl[azure-blob]` and
`langchain>=1,<2`. Both hosts also use the wheel's Blob saver; only path (2)
uses its HTTP adapter. The unpublished Blueprint method's successful indexing
distinguishes the candidate from published PyPI `0.14.2`.

Azure CLI **2.83.0**, Core Tools **4.8.0**, uv **0.11.7**. Both runs used
`infra/flex.bicep`: **FC1 Flex Consumption, Python 3.12 configured, 512 MB,
Korea Central, maximum 40 instances, no always-ready instances**, Standard_LRS
storage, `identityBasedStorage=false`. The template creates no Insights
component; no implicit workspace or telemetry resources were needed.

The remote-build publisher reported local Python `3.10.12` differing from the
configured `3.12`, but completed successfully. This is a recorded tool warning,
not a running-worker inventory. Neither exact cloud Python patch version nor
resolved deployment dependency versions was captured in this run; the earlier
#995 process inventory is not a substitute for these deployments.

Each host used a fresh group and storage account. **No cloud resource was
pre-provisioned** for the measurements. The workstation's Azure login, installed
CLI/Core Tools/uv, source checkout and wheel were prerequisites, excluded from
setup timing. The existing-app path simulates an existing application by keeping
the example's HTTP and timer declarations; it is not a test on a maintainer's
long-lived production app. The timer's firing/recovery was not measured.

Host/deployment storage stayed connection-string based. The same account held
`paired-conversation` checkpoints via `AZURE_STORAGE_CONNECTION_STRING` and
`LANGGRAPH_BLOB_CONTAINER`. No RBAC assignments or Entra registrations were
created. Function keys are app-level shared secrets, not user/thread ownership.

## Host-code delta

### (1) Handwritten direct Functions wrapper

One `FunctionApp`, one function-key-protected HTTP route
`POST /api/graphs/{name}/invoke`, graph lookup, JSON parsing, optional LangGraph
`config`, synchronous invocation and message serialization were user-written:

```python
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.route(route="graphs/{name}/invoke", methods=["POST"])
def invoke(req: func.HttpRequest) -> func.HttpResponse:
    body = req.get_json()
    graph = {"official_basic": basic, "official_conversation": conversation,
             "official_schema": schema}[req.route_params["name"]]
    result = graph.invoke(body["input"], config=body.get("config"))
    return func.HttpResponse(
        json.dumps({"output": result}, default=lambda value:
                   message_to_dict(value) if isinstance(value, BaseMessage)
                   else str(value)), mimetype="application/json")
```

This excerpt is the measured minimal control, **not a recommended production
wrapper**: it does not implement the adapter's validation/error policy, state
endpoint, locks, health, streaming or authorization hooks. Full entrypoint is
retained in the notes. Matching the happy-path envelope/serializer makes the
outputs comparable, not the complete host contracts equivalent.

### (2) Adapter in an existing Function App

The existing example's HTTP/timer declarations were retained byte-for-byte.
Graph imports were renamed to avoid module collisions, and two registrations
were added beside its original one:

```python
langgraph_app = LangGraphApp()
langgraph_app.register(graph=graph, name="official_basic")
langgraph_app.register(graph=conversation, name="official_conversation")
langgraph_app.register(graph=schema, name="official_schema")
app.register_functions(langgraph_app.to_blueprint())
```

The graph/storage code was shared with path (1); all registrations preceded
export. Discovery reported **13 functions**: existing HTTP/timer, two health
functions and invoke/stream/state for each of three graphs. The direct host
reported **one** HTTP function. A discoverable state route does not by itself
establish persistence; the two stateless graphs have no checkpointer.

## Observed calls and timings

`curl --max-time 120` measured request `time_total` including connection/TLS and
response transfer. A fresh curl process was used for every request. Sequence:
basic no key/wrong key/valid key, schema no key/wrong key/valid key, then the three
conversation calls. Path (2) additionally called existing HTTP between schema
and conversation. The publisher had already probed host status; there was a
30-second wait before publish and indexing/key retrieval before calls.
**These are first authorized graph calls after publish, not controlled
scale-from-zero or cold-start measurements.** Calls within one app can warm
later calls, and instance selection was not controlled.

| Observation | Direct wrapper (1) | Existing-app adapter (2) |
| --- | --- | --- |
| Group create | 1.608 s | 1.546 s |
| Template deployment | 99.476 s | 69.573 s |
| Checkpoint settings update | 3.892 s | 6.927 s |
| Remote-build publish | 102.382 s | 102.426 s |
| Basic no key / wrong key | 401 / 401 | 401 / 401 |
| Basic first authorized invoke | 200; **4.098063 s**, `Hi`, `Hello!`, `extra_field=10` | 200; **4.326918 s**, same output |
| Schema no key / wrong key | 401 / 401 | 401 / 401 |
| Schema first authorized invoke | 200; **0.077716 s**, `{"output":{"answer":"bye"}}` | 200; **4.196643 s**, same output |
| Conversation first authorized invoke (`a`) | 200; **0.940789 s**, `Hi`, count 1 | 200; **4.616160 s**, same contents |
| Conversation second invoke (`a`) | 200; **0.397221 s**, `Hi`, count 1, `Continue`, count 2 | 200; **0.330489 s**, same contents |
| Conversation new thread (`b`) | 200; **4.613037 s**, `Separate`, count 1 | 200; **1.109934 s**, same contents |
| Existing HTTP with key | Not part of this host | 200; **4.076948 s**, `existing HTTP still works` |

One sample per cell, separate sequential apps, uncontrolled workstation/cache
and cloud scheduling. The adapter's schema call was much slower in this sample;
the new-thread call was faster. Neither observation establishes an adapter
overhead, latency advantage, price or throughput claim. Setup rows are command
durations, not full elapsed onboarding time.

### Persistent-state evidence

Both hosts returned the previous messages and count `2` on the second `paired-a`
call, while `paired-b` had only its own turn. An independent authenticated
`az storage blob list` against each owned checkpoint container returned
checkpoint/metadata/write/value blobs and `latest.json` for both threads.
This is evidence of real Blob artifacts and same-thread continuity, **not**
independent decoding of checkpoint contents, process replacement, restart
recovery, multi-instance locking, ownership or fenced writes. No restart was
attempted; default in-process coordination remains unsuitable as a general
shared-writer guarantee. No Functions stream/state call was measured here.

## (3) Foundry: captured permission blocker

A separate fresh group in Korea Central was used. ARM API `2025-06-01` accepted
creation of an **S0 AIServices** account with `allowProjectManagement=true`,
custom subdomain and system identity (response state `Creating`, 8.118 s).
A child project with system identity and empty properties reported `Succeeded`
(2.146 s). Parent terminal provisioning was **not** observed before the next
request; no parent-success claim is made.

Authenticated `GET /api/projects/verify/agents?api-version=v1`, requested with
the `https://ai.azure.com` token audience, returned exit `1` / HTTP **403** in
1.902 s. Captured stderr, redacted before storage:

```text
ERROR: Forbidden
code: UserError
innerError.code: ForbiddenError
Identity(object id: <redacted>) does not have permissions for
Microsoft.CognitiveServices/accounts/AIServices/agents/read actions.
statusCode: 403
environment/location: koreacentral
```

The denial ended the attempt immediately, well inside the 30-minute bound.
No alternate identity, resource, region, role grant or privilege escalation was
used. No agent version, source upload/build, session, container, registry,
Insights/workspace or paid-token model deployment was created. Hosted runtime,
first invoke, quota/capacity, conversation persistence and schema handling are
**untested**, not failed runtime tests. This proves the missing permission at
that request, not that all other prerequisites would succeed after a grant.

For a future explicitly authorized run, the maintainer must arrange the
documented **Foundry Project Manager** project-scope access for the deploying
identity, including the denied agent-read action, and verify propagation.
That is a maintainer action, not a grant performed by this experiment. Then
verify parent/project readiness, hosted-version provisioning and session
capacity, capture terminal status/logs before deletion, and invoke the same
graphs without paid model calls. Any platform-created agent identity also needs
explicit cleanup tracking; none was created here.

### Documented Foundry deltas, not measured outcomes

The [existing-graph guide](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/langchain-hosted-agents#host-an-existing-agent)
was read on 2026-11-28, documentation source commit
`313dddc45e4714103e4d7f0023d98bb8e559f459`. It describes
`langgraph.json` graph exports/factories and
`python -m langchain_azure_ai.agents.hosting.run --protocol responses`, with
`langchain-azure-ai[hosting]>=1.3.0`. Source deployment also requires host
entrypoint/dependency configuration and `PYTHONNOUSERSITE=1`; its sample targets
Python 3.13. **That runtime was not deployed in this experiment.**

Responses history/`previous_response_id` or conversation IDs are not native
Functions `config.configurable.thread_id`. Invocations session routing is not
conversation-history persistence. The guide says default hosts require a
`messages` schema; a custom-state graph needs custom host mapping, rather than
pretending the question/answer graph is a chat agent. Its durable Foundry
checkpointer is async-only, unlike this example's sync call path. Those are
documented integration choices to measure later, not tested modifications or
Foundry API work shipped here.

Further references: [deployment permissions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent#required-permissions),
[regions and quotas](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/limits-quotas-regions),
[source deployment](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-code).

## Cleanup and acceptance status

Each Functions group was deleted immediately after its HTTP/Blob evidence was
saved. The Foundry group was deleted immediately after stderr capture. All
deletions were awaited and verified **before this document was written**:

```text
az group exists -n <direct-owned-group>: false
az group exists -n <existing-owned-group>: false
az group exists -n <foundry-owned-group>: false
az resource list --tag purpose=996-verify --query 'length(@)': 0
az group list --query "[?starts_with(name, 'DefaultResourceGroup-')].name": []
```

The same five unrelated groups were untouched. No subscription/tenant changes,
role assignments or app registrations were made. No resources were left running
while writing documentation.

| #996 acceptance item | Evidence / remaining limitation |
| --- | --- |
| Same upstream graphs through all three paths, conversation and non-messages state | Both Functions paths measured on all three graphs; Foundry blocked before agent deployment, so the full three-path item remains incomplete |
| Graph/host changes, configuration/resources, first-call timing, persistent state | Recorded above for Functions; precise Foundry provisioning boundary and untested runtime/storage/protocol behavior stated |
| Sanitized raw notes and explicit untested paths | Linked output excerpts preserve request timings, responses, discovery, storage listing and the Foundry error; sensitive identifiers/credentials removed; no performance ranking |
| Reuse #994/#995; link from #913 and inform #371 | Prior evidence audited above; linked from the [onboarding baseline](onboarding-baseline.md). This informs [#371](https://github.com/yeongseon/azure-functions-langgraph-python/issues/371), but its explicit maintainer hold on upstream outreach remains in force |

Use this as partial adoption evidence, not release certification, production
authorization, a complete Foundry comparison, or permission to submit an
upstream listing. No required Azure CI/release gate, workflow, version or
dependency policy changes accompany this record. Refs #996.
