# SQLite checkpoint (local)

Local-development example using `create_sqlite_checkpointer()` to persist
LangGraph state in a single on-disk SQLite file. Zero external services
required — just a writeable path.

## ⚠️ Not for production

SQLite is a **local-development and single-instance** backend. It does not
support concurrent writes from multiple Azure Functions instances. For
production deployments, use:

- **Postgres** — `examples/postgres_checkpoint_production/`
- **Azure Blob** — `examples/persistent_agent_blob_table/`
- **Cosmos DB** — `examples/cosmos_checkpoint_azure/`

## Files

- `function_app.py` — wires `SqliteSaver` via the bundled DX helper
- `graph.py` — turn-counting echo agent
- `host.json`, `local.settings.json.example`, `requirements.txt`

## Run locally

```bash
cd examples/sqlite_checkpoint_local
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

The helper opens the SQLite file (creating it if absent) and runs
`SqliteSaver.setup()` once on cold start. Override `LANGGRAPH_SQLITE_PATH`
in `local.settings.json` to choose a different file location.

## Verify persistence

```bash
THREAD=$(curl -s -X POST http://localhost:7071/api/threads \
  -H "Content-Type: application/json" -d '{}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["thread_id"])')

curl -s -X POST "http://localhost:7071/api/threads/$THREAD/runs/wait" \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"sqlite_agent","input":{"messages":[{"role":"human","content":"first"}]}}'

curl -s -X POST "http://localhost:7071/api/threads/$THREAD/runs/wait" \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"sqlite_agent","input":{"messages":[{"role":"human","content":"second"}]}}'
```

The second response shows `[turn 2]`. Restart `func start` and the
counter still increments because state lives in the SQLite file.

## Walkthrough output

The equivalent native-route walkthrough below uses one fixed thread and then
reads its state. Start the host with a fresh database first (for example,
`LANGGRAPH_SQLITE_PATH=/tmp/sqlite-walkthrough.sqlite func start` after removing
that file); otherwise the persistent counter continues from earlier runs.

```console
$ curl -s -X POST http://localhost:7071/api/graphs/sqlite_agent/invoke -H "Content-Type: application/json" -d '{"input":{"messages":[{"role":"human","content":"first"}]},"config":{"configurable":{"thread_id":"walkthrough-thread"}}}'
{"output":{"messages":[{"role":"human","content":"first"},{"role":"assistant","content":"[turn 1] Echo: first"}],"turn":1}}
$ curl -s -X POST http://localhost:7071/api/graphs/sqlite_agent/invoke -H "Content-Type: application/json" -d '{"input":{"messages":[{"role":"human","content":"second"}]},"config":{"configurable":{"thread_id":"walkthrough-thread"}}}'
{"output":{"messages":[{"role":"human","content":"second"},{"role":"assistant","content":"[turn 2] Echo: second"}],"turn":2}}
$ curl -s http://localhost:7071/api/graphs/sqlite_agent/threads/walkthrough-thread/state
{"values":{"messages":[{"role":"human","content":"second"},{"role":"assistant","content":"[turn 2] Echo: second"}],"turn":2},"next":[],"metadata":{"source":"loop","step":4,"parents":{}}}
```

SQLite keeps this state across local single-instance restarts. For durable,
multi-instance Azure storage, use the
[`production_persistent_agent`](../production_persistent_agent/) Blob walkthrough.

## When to use

- Local development and demos.
- Single-instance deployments where the SQLite file lives on persistent
  storage (App Service mounted volume, container volume, etc.).

For multi-instance Azure Functions deployments, use the Postgres helper
(`examples/postgres_checkpoint_production/`) or the Azure Blob
checkpointer (`examples/persistent_agent_blob_table/`) instead.
