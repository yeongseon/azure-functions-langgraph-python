# Durable Async Agent — Long-Running Runs with Durable Functions

This example serves a LangGraph graph as an **asynchronous run lifecycle**
backed by [Azure Durable Functions](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-overview),
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
POST /api/runs/{run_id}/cancel ──►  202  { "status": "cancel_requested" }  (poll GET for the outcome)
```

`run_id == Durable instance_id == platform run_id` — one stable id, no second
run registry. Durable instance state is the single source of truth for status.

Normalized statuses returned by `GET /runs/{run_id}`:

| status | meaning |
| --- | --- |
| `pending` | run accepted, orchestration not yet scheduled |
| `running` | orchestration/activity in flight |
| `success` | graph completed; `output.result` holds the graph output |
| `error` | graph raised, or the run was rejected (e.g. thread-lock contention); the response carries only `{"code": "run_failed", "message": "The run failed."}`, and the raw exception is in the host log |
| `cancelled` | the orchestration was terminated (it does not prove the graph or its tools stopped) |

`cancel_requested` is **not** one of these: it is only the acknowledgement of the cancel endpoint.

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
{"run_id": "9f3c...e21", "status": "cancel_requested"}
```

`cancel_requested` means the termination call returned without reporting a failure; it does
**not** mean the run is cancelled, and it does not stop an Activity that is already running, so
the graph and its tools can keep executing. Poll the run until the status is terminal:

```bash
curl -s http://localhost:7071/api/runs/9f3c...e21
```

```json
{"run_id": "9f3c...e21", "status": "cancelled", "output": null}
```

If the run had already finished, the cancel call answers `200` with that real status
(`success`, `error` or `cancelled`) and requests nothing; if it finishes while the request is in
flight, `GET` shows `success` or `error` instead of `cancelled`. An unknown run is `404`, and a
backend failure is a `502` with a generic message (the cause is in the host log). Cancellation
cannot roll back effects of tools that already ran and delivery is at-least-once, so give
side-effecting tools an idempotency key. Earlier releases answered `202 {"status": "cancelled"}`
at once; clients that treated that as final must poll `GET`, and clients that accepted only
`202` must also accept `200`.

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

- Activities are **at-least-once**. If the Activity finishes but its result is not
  durably recorded (host restart, lost result), Durable can deliver the same payload
  again and the whole graph runs again. A checkpointer does **not** make that safe:
  the adapter always invokes with the payload input, so a redelivery starts a new
  graph run on the same thread and nodes that already ran (and their external
  effects) can run again. Orchestrator replay is different: it re-drives only the
  deterministic orchestrator and never re-executes the Activity. An explicit
  application retry (a new `run_id`) is a third case and is not de-duplicated either.
- Give every non-idempotent tool call (payment, email, ticket) an idempotency key
  derived from something stable across deliveries, such as the `run_id`, and pass it
  to a provider that honors it:

  ```python
  key = f"charge:{run_id}"          # stable across redeliveries of one run
  receipt = payments.charge(key, amount)  # provider returns the first result for a repeated key
  ```

  This gives at-most-one effect per key **only if the provider enforces the key
  atomically**. A key you check in your own store *after* the effect leaves a window
  where a crash repeats the effect. A key minted per attempt (for example a random
  UUID) does nothing. A new `run_id` is a new key, so de-duplicating deliberate
  retries needs a business-level key. Prefer tool-level retries with the same key
  over relying on Durable to retry the Activity.
- The thread lock serializes concurrent attempts on a checkpointed thread (the loser
  is reported as rejected and its tools do not run) but does not de-duplicate, and a
  lock whose ownership is lost is not a fence. The deterministic contract is pinned in
  `tests/test_durable_replay_idempotency.py`; behaviour on a real Azure host is not
  covered there.
- This example runs one activity per run. It does **not** re-express the graph
  topology as Durable activities; that is intentionally out of scope.
