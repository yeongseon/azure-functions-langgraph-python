# Postgres checkpoint (production)

Production-oriented example using `create_postgres_checkpointer()` to
persist LangGraph state in Postgres (Azure Database for PostgreSQL,
Supabase, RDS, or self-hosted). Suitable for multi-instance Azure
Functions deployments where SQLite cannot be shared across workers.

## Files

- `function_app.py` — wires `PostgresSaver` via the bundled DX helper, env-var driven
- `graph.py` — turn-counting echo agent
- `host.json`, `local.settings.json.example`, `requirements.txt`

## Run locally

Bring up a Postgres instance (Docker is the fastest path):

```bash
docker run -d --name pg-langgraph \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=langgraph \
  -p 5432:5432 \
  postgres:16
```

Then:

```bash
cd examples/postgres_checkpoint_production
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

## Configuration

| Setting | Required | Description |
| --- | --- | --- |
| `LANGGRAPH_POSTGRES_CONNECTION_STRING` | yes | psycopg-compatible connection string. Example: `postgresql://user:pass@host:5432/db` (add `?sslmode=require` for Azure Database for PostgreSQL). |
| `LANGGRAPH_POSTGRES_SETUP` | no (default `true`) | Run `setup()` on cold start to create / migrate the checkpoint tables. Set to `false` if migrations are managed out-of-band (e.g. by a deployment pipeline). |

## Verify persistence

```bash
THREAD=$(curl -s -X POST "http://localhost:7071/api/threads?code=$KEY" \
  -H "Content-Type: application/json" -d '{}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["thread_id"])')

curl -s -X POST "http://localhost:7071/api/threads/$THREAD/runs/wait?code=$KEY" \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"postgres_agent","input":{"messages":[{"role":"human","content":"first"}]}}'

curl -s -X POST "http://localhost:7071/api/threads/$THREAD/runs/wait?code=$KEY" \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"postgres_agent","input":{"messages":[{"role":"human","content":"second"}]}}'
```

The second response shows `[turn 2]`. Restart `func start` (or scale to
multiple instances) and the counter still increments because state
lives in Postgres.

## Production tips

- Set `LANGGRAPH_POSTGRES_SETUP=false` once your deployment pipeline
  owns migrations — re-running `setup()` on every cold start is safe
  but unnecessary in production.
- Use Azure Database for PostgreSQL with Managed Identity authentication
  by composing the connection string from the Managed Identity token at
  startup.
- Pair with `examples/maintenance_timer/` if you also use
  `AzureTableThreadStore` for thread metadata.
