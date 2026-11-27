# Official LangGraph conversation: durable-state proof

This standalone Functions app adapts the official memory guide's **complete
sync `call_model` graph**, not the earlier `StateGraph(...)` placeholder.
It uses a deterministic fake model and durable Blob checkpoints: on local Azurite
for the repeatable check, and on a real Storage account in the one-off
[Azure verification](#azure-verification-2026-11-26).
It is a compatibility example, not a production or multi-user template.

## Pinned upstream source and attribution

- Page: <https://docs.langchain.com/oss/python/langgraph/add-memory>.
- Source: [langchain-ai/docs, `add-memory.mdx`, “Example: using Postgres
  checkpointer” / Sync](https://github.com/langchain-ai/docs/blob/15c95b5f33287dff462acb53e1f34aaed69d5c21/src/oss/langgraph/add-memory.mdx).
- Exact commit: `15c95b5f33287dff462acb53e1f34aaed69d5c21`, retrieved 2026-11-25.
- License: [MIT, Copyright (c) 2025 LangChain](https://github.com/langchain-ai/docs/blob/15c95b5f33287dff462acb53e1f34aaed69d5c21/LICENSE),
  reproduced in [UPSTREAM_LICENSE](UPSTREAM_LICENSE).

### Extraction and necessary deviations

[`upstream_graph.py`](upstream_graph.py) retains the node body, `MessagesState`
schema (including its `add_messages` reducer), `StateGraph(MessagesState)`,
`builder.add_node(call_model)` and `builder.add_edge(START, "call_model")`.
Only indentation changes when extracting from the upstream database context.
An AST comparison against an independently retained snippet guards those statements.

| Upstream | Local substitution, outside graph logic |
| --- | --- |
| `init_chat_model(model="claude-haiku-4-5-20251001")` | [`model.py`](model.py): state-aware fake returns the number of human turns it actually sees; no model service or key |
| `PostgresSaver` context and database URI | [`graph.py`](graph.py): `AzureBlobCheckpointSaver`, explicit local connection supplied by the runner, local container bootstrap, same `builder.compile(checkpointer=checkpointer)` |
| Two `stream_events(..., version="v3")` demonstrations | Native `invoke` twice on thread `a`, then once on `b`; this validates the adapter's invoke contract, not upstream v3 streaming |
| No Functions host | [`function_app.py`](function_app.py): `LangGraphApp(auth_level=FUNCTION)` plus registration |

The upstream graph is run independently with the **same documented fake-model
substitution** and `InMemorySaver` for a credential-free baseline. This is not a
claim to have exercised Anthropic or Postgres. The real local host instead uses
Blob/Azurite, so its successful second invoke crosses an actual durable-storage
boundary rather than using the baseline's in-process saver.

### Choice and alternatives

The complete sync memory graph has an explicit conversational schema and node,
unlike the short `StateGraph(...)` introduction. Retaining those statements makes
the source-preservation claim testable. The guide's Postgres/MongoDB/Redis variants
add external database services; the fake plus existing Blob adapter avoids them.
The guide's cross-thread long-term-memory example introduces `BaseStore`, user
context and a paid model: useful, but a different boundary from per-thread
checkpoint continuity. The existing official basic graph remains the unchanged,
model-free stateless control. Neither graph depends on the Blueprint work in #993.

## Repeatable published-wheel and candidate check

From the repository root, with Python 3.11+, `uv`, Core Tools and Azurite installed:

```bash
python3.11 scripts/certify_primary_examples.py published \
  --examples official_langgraph_basic official_langgraph_conversation \
  --local-host --host-port 7571 --storage-port 15000 \
  --python python3.11 --output-dir /tmp/opencode/venv-upstream-examples
```

This is explicitly opt-in: it downloads from PyPI into throwaway environments,
installs `requirements.txt` unchanged (`azure-functions-langgraph[azure-blob]==0.14.2`),
runs `uv pip check`, records the complete resolved dependency inventory, executes
the independent baseline and registered handler, then starts the real host.
The regular offline unit suite uses real deterministic graphs and wire fakes;
its network/process test skips with a prerequisite message unless
`RUN_UPSTREAM_WHEEL_CERTIFICATION=1` is set.

For a candidate wheel built with `make build`, change the positional lane to
`candidate` and add `--wheel dist/<candidate>.whl`, using a separate output directory.
Only the adapter requirement changes; its extras and all other dependencies stay.
Both lanes assert the adapter imports from the installed distribution, never `src/`.
Without `--local-host`, the checker runs baseline/handler checks only.
The original four custom examples retain their existing construction checks.

The host runner copies app files to a temporary directory, starts Azurite on
loopback Blob/Queue/Table ports **15000/15001/15002** with an explicitly generated
connection string (not `UseDevelopmentStorage=true`), then runs
`func start --python --port 7571 --enableAuth`. A run-owned `TMPDIR` isolates
Core Tools' secrets directory. A random function key is passed only in
`x-functions-key`; both the keyless attempt and a distinct non-empty wrong-key
attempt must return `401` before the authorized first invoke can return `200`.
Temporary keys, storage and raw logs
are deleted and owned process groups stopped on success or failure. Port
conflicts fail rather than reusing or stopping somebody else's service.
The opt-in pytest command has a 300-second timeout: it sends `SIGTERM` to its
certifier process group, allows up to 30 seconds for nested-runner cleanup,
then sends `SIGKILL` and reaps the direct child. Offline tests exercise this
cleanup with lightweight processes, without Core Tools or Azurite.
The wrong-key probe was added after the recorded runs below; those historical
tables are not evidence of wrong-key rejection.

## Recorded verification: 2026-11-25

**Plan for this table: local Functions host only.** The Azure lane stays opt-in:
nothing here adds an Azure step to CI or to the release gate, which needs a
separate maintainer decision. The one-off manual Azure run of 2026-11-26 is
recorded [below](#azure-verification-2026-11-26); this table does not certify
cloud deployment.

| Item | Observed result |
| --- | --- |
| Python / host / emulator | 3.11.15 / Core Tools 4.8.0 / Azurite 3.35.0 |
| Adapter | Published PyPI wheel **0.14.2** and separately built candidate wheel **0.14.2** |
| Resolved core dependencies | `azure-functions==1.26.0`, `langgraph==1.2.24`, `langchain-core==1.8.4`, `langgraph-checkpoint==4.3.2`, `azure-storage-blob==12.31.1` |
| Upstream baseline and registered handler | Equal message contents for all three invocations |
| Keyless first attempt | `401` in both wheel lanes |
| Authorized first invoke, thread `a` | `200`; `Hi`, `observed 1 human turn(s)` |
| Authorized second invoke, thread `a` | `200`; `Hi`, `observed 1 human turn(s)`, `Continue`, `observed 2 human turn(s)` |
| Authorized first invoke, thread `b` | `200`; `Separate`, `observed 1 human turn(s)`; no thread `a` messages |
| Cleanup | Both owned host/emulator groups stopped after each example |

Each run emits `installed-versions.txt`, `fake-route-smoke.txt` (baseline and
handler contents plus runtime versions), `local-host.json` (sanitized HTTP
outcomes) and `summary.json` with artifact references. Local published evidence
was generated under `/tmp/opencode/venv-upstream-examples`; candidate evidence
under its `candidate/` subdirectory. Full dependency inventories are reproducible
run artifacts, not immutable dependency locks.

## Azure verification: 2026-11-26

Run on 2026-11-26 UTC by the maintainer-authorized verification run for #994,
from the files in this directory as shipped, with
`azure-functions-langgraph[azure-blob]==0.14.2` pinned in `requirements.txt`.
It follows the CLI path of
[Step 5 of the 5-minute tutorial](../../docs/tutorial-5-min.md) (Storage account,
Flex Consumption app, remote-build publish) plus the two application settings
below. Differences from the tutorial's literal commands: the two settings were
set, and `func azure functionapp publish` got `--python --build remote`
(`--python` because this directory has no `local.settings.json.example` for Core
Tools to detect the runtime from; `--build remote` named explicitly). One
temporary resource group, deleted afterwards.
**This is a one-off manual run, not a CI or release-gate lane.**

| Item | Value |
| --- | --- |
| Plan / runtime | Flex Consumption, Python 3.11, `koreacentral`, Functions v4 |
| Tools | Azure CLI 2.83.0, Core Tools 4.8.0 |
| Adapter | Published PyPI **0.14.2** with the `azure-blob` extra, installed by the remote build from `requirements.txt` unchanged (pinned) |
| Other packages | The Azure build log does not print resolved versions. A `uv pip compile` of the shipped requirements (Python 3.11, manylinux x86_64) within the hour of the deployment gives `azure-functions==1.26.0`, `langgraph==1.2.24`, `langchain-core==1.8.5`, `langgraph-checkpoint==4.3.2`, `azure-storage-blob==12.31.1`: a reproduction of the resolution, not the deployed inventory |
| Storage | The one `Standard_LRS` Storage account created for the app (passed to `--storage-account`) also holds the checkpoint container. It is reached through `AZURE_STORAGE_CONNECTION_STRING` (a shared key, **not** identity-based access) with `LANGGRAPH_BLOB_CONTAINER=official-conversation`; `graph.py` creates the container when the app starts |
| `GET /api/health` (no key) | `200` |
| `POST /api/graphs/official_conversation/invoke` without key | `401` |
| First invoke with the function key, thread `a` | `200`; `Hi`, `observed 1 human turn(s)` |
| Second invoke, thread `a` | `200`; `Hi`, `observed 1 human turn(s)`, `Continue`, `observed 2 human turn(s)` |
| First invoke, thread `b` | `200`; `Separate`, `observed 1 human turn(s)`; no thread `a` messages |
| Cleanup | Resource group deleted and the deletion awaited (`az group wait --deleted`); afterwards no resource group from the run was listed and no resource tagged for it remained. See the side effect below |

Same-thread continuity and cross-thread isolation were observed over HTTP against
the deployed app, with a real Storage account as the checkpoint store. The calls
could have been served by the same instance, and the run neither restarted nor
scaled the app nor inspected the container, so it shows no recovery across
restarts or instances. First requests after a deployment can include a cold
start, so no latency is recorded.

Side effect: `az functionapp create` also creates a default Log Analytics
workspace in a separate `DefaultResourceGroup-<region code>` resource group,
which deleting the example's resource group does not remove. It was deleted
separately afterwards; see
[the basic example's note](../official_langgraph_basic/README.md#azure-re-verification-2026-11-26).

## Scope and follow-ups

- Durable storage here means graph checkpoints in Blob storage: local Azurite for
  the repeatable check, one real Storage account in the Azure run above. This is
  **not** Durable Functions orchestration; no restart, scale-out, cancellation,
  lease-loss fencing or cloud-recovery certification is claimed.
- Local container creation is convenience wiring. This single-host example uses
  the default in-process lock; shared writers need distributed coordination, and
  Blob leases are not checkpoint fencing. See the [production reference](../../docs/production-reference.md).
- A function key is a shared app secret, not authenticated user/thread ownership.
  Thread isolation is state separation only.
- #994 item 3 is a **follow-up**: reuse #993's existing-app example after Draft
  PR #998 is ready; this standalone proof neither consumes nor modifies it.
- A repeatable Azure lane, other plans or regions, identity-based storage access
  and any release-gate change need separate maintainer authorization and
  evidence. Refs #994.
