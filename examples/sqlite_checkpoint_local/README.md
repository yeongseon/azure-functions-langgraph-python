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

## When to use

- Local development and demos.
- Single-instance deployments where the SQLite file lives on persistent
  storage (App Service mounted volume, container volume, etc.).

For multi-instance Azure Functions deployments, use the Postgres helper
(`examples/postgres_checkpoint_production/`) or the Azure Blob
checkpointer (`examples/persistent_agent_blob_table/`) instead.
