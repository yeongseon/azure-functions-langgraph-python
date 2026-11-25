# Official LangGraph conversation: local durable-state proof

This standalone Functions app adapts the official memory guide's **complete
sync `call_model` graph**, not the earlier `StateGraph(...)` placeholder.
It uses a deterministic fake model and durable Blob checkpoints on local Azurite.
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
`x-functions-key`; the keyless first attempt must return `401` before the
authorized first invoke can return `200`. Temporary keys, storage and raw logs
are deleted and owned process groups stopped on success or failure. Port
conflicts fail rather than reusing or stopping somebody else's service.

## Recorded verification: 2026-11-25

**Plan: local Functions host only; no Azure hosting plan was exercised in this
change. The Azure lane remains opt-in and unrun, pending a separate maintainer
release-gate decision.** This evidence does not certify cloud deployment.

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

## Scope and follow-ups

- Durable storage here means graph checkpoints in local Blob storage. This is
  **not** Durable Functions orchestration; no restart, scale-out, cancellation,
  lease-loss fencing or cloud-recovery certification is claimed.
- Local container creation is convenience wiring. This single-host example uses
  the default in-process lock; shared writers need distributed coordination, and
  Blob leases are not checkpoint fencing. See the [production reference](../../docs/production-reference.md).
- A function key is a shared app secret, not authenticated user/thread ownership.
  Thread isolation is state separation only.
- #994 item 3 is a **follow-up**: reuse #993's existing-app example after Draft
  PR #998 is ready; this standalone proof neither consumes nor modifies it.
- Real Azure deployment, plan selection and any future release-gate change need
  separate maintainer authorization and evidence. Refs #994.
