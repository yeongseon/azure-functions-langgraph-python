# Durable Async Agent — Long-Running Runs with Durable Functions

This example serves a LangGraph graph as an **asynchronous run lifecycle**
backed by [Azure Durable Functions](https://learn.microsoft.com/azure/azure-functions/durable/),
via `LangGraphApp(async_runs="durable")` (issue #408).

Instead of a single synchronous `POST /invoke` that blocks until the graph
finishes, callers **start** a run, **poll** it, and optionally **cancel** it —
the pattern you want when a run may outlive an HTTP request timeout, or when the
caller wants to fire-and-follow rather than block.

> **Not "unlimited" and not a checkpoint replacement.** This is an async run
> lifecycle over the existing graph runtime. Durable history is **not** a
> replacement for LangGraph checkpoints — configure a checkpointer as usual for
> conversation state. The Durable orchestrator only sequences a single activity
> that runs your graph; it does not re-express your graph's nodes/edges as
> Durable steps.

## How it works

```
POST /api/graphs/{name}/runs   ──►  202  { "run_id": "...", "status": "pending" }
        │  (Durable: start_new orchestration, instance_id == run_id)
        ▼
   orchestrator  ──yields──►  execute_langgraph_run  activity  ──►  graph.invoke()
        │                          (all user/graph code runs HERE, not in the
        │                           orchestrator, so replay stays deterministic)
        ▼
GET  /api/runs/{run_id}        ──►  200  { "status": "running" | "success" | ... , "output": ... }
POST /api/runs/{run_id}/cancel ──►  202  { "status": "cancelled" }
```

`run_id == Durable instance_id == platform run_id` — one stable id, no second
run registry. Durable instance state is the single source of truth for status.

Normalized statuses returned by `GET /runs/{run_id}`:

| status | meaning |
| --- | --- |
| `pending` | run accepted, orchestration not yet scheduled |
| `running` | orchestration/activity in flight |
| `success` | graph completed; `output.result` holds the graph output |
| `error` | graph raised, or the run was rejected (e.g. thread-lock contention) |
| `cancelled` | run was terminated via the cancel endpoint |

## Files

- `graph.py` — the compiled graph (a deterministic `work → respond` graph with a
  tiny artificial delay so `pending`/`running` is observable)
- `function_app.py` — `LangGraphApp(async_runs="durable")` wiring
- `host.json`, `local.settings.json.example`, `requirements.txt`

## Requirements

The Durable async surface is gated behind the optional `durable` extra:

```bash
pip install "azure-functions-langgraph[durable]"
```

Durable Functions needs a storage account for its task hub — locally this is
[Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite)
via `AzureWebJobsStorage=UseDevelopmentStorage=true` (already set in
`local.settings.json.example`).

## Local development

```bash
cp local.settings.json.example local.settings.json

pip install -r requirements.txt

# Start Azurite in another terminal (or use the VS Code extension):
#   npm install -g azurite && azurite

func start
```

### Start a run

```bash
curl -s -X POST http://localhost:7071/api/graphs/durable_async_agent/runs \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Hello!"}]}}'
```

```json
{"run_id": "9f3c...e21", "status": "pending"}
```

### Poll the run

```bash
curl -s http://localhost:7071/api/runs/9f3c...e21
```

```json
{"run_id": "9f3c...e21", "status": "running", "output": null}
```

…then, once complete:

```json
{
  "run_id": "9f3c...e21",
  "status": "success",
  "output": {
    "run_id": "9f3c...e21",
    "activity_status": "success",
    "result": {"messages": [
      {"role": "human", "content": "Hello!"},
      {"role": "assistant", "content": "Processed: Hello!"}
    ]},
    "error": null
  }
}
```

### Cancel a run

```bash
curl -s -X POST http://localhost:7071/api/runs/9f3c...e21/cancel
```

```json
{"run_id": "9f3c...e21", "status": "cancelled"}
```

## Thread state and locking

Pass `thread_id` (or `config.configurable.thread_id`) in the create-run body to
run against a checkpointed thread. When the graph has a checkpointer and a
`thread_id` is present, the app-level `thread_lock` guards the run — concurrent
runs for the same thread do not mutate checkpoints concurrently; a contended run
comes back with `status: "error"` (rejected) rather than corrupting state.

## Telemetry

Runs carry a `trigger_type="durable"` correlation field on the `RunContext`, so
durable async runs are distinguishable from HTTP invoke/stream runs in your
observer. Input/output payloads are **never** included in telemetry.

## Scope & caveats (experimental)

- Activities are **at-least-once** — Durable may re-run an activity on host
  restart. Graph runs should be idempotent (a checkpointer makes replays
  converge on the same thread state).
- This example runs one activity per run. It does **not** re-express the graph
  topology as Durable activities — that is intentionally out of scope (see the
  sibling `azure-functions-durable-graph-python` package for a manifest-first
  Durable graph runtime).
