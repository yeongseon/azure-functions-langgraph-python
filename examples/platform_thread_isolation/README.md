# Platform thread isolation example

Companion to [`platform_compat_sdk`](../platform_compat_sdk/). It makes one
behaviour explicit, using the official `langgraph-sdk` client against a
`platform_compat=True` host:

- **Same `thread_id`, repeated** → the thread's state continues across runs.
- **Different `thread_id`** → separate state; nothing from the other thread is visible.

The graph is deterministic (no model, no credentials, no network beyond
`localhost`): each run appends one `note` to the thread's `notes` list and
increments `turns`.

## Files

- `graph.py` — notebook `StateGraph` with a list reducer and an in-memory checkpointer
- `function_app.py` — registers `notebook_agent` with `platform_compat=True`
- `sdk_client.py` — `run_demo(client)`: two runs on thread A, one on thread B, repeat A, read both states
- `host.json`, `local.settings.json.example`, `requirements.txt`

## Run offline (no host needed)

```bash
pytest tests/test_platform_thread_isolation_example.py
```

The test drives `run_demo` through the same in-process SDK-to-Functions
transport bridge as the other SDK tests.

## Run locally

```bash
cd examples/platform_thread_isolation
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

In a second terminal:

```bash
pip install langgraph-sdk
python sdk_client.py
```

Output (captured from a local `func start`):

```text
thread_a: notes=['alpha-1', 'alpha-2'] turns=2
thread_b: notes=['beta-1'] turns=1
```

## What this does and does not show

- This is **state separation per `thread_id`, not authenticated ownership**.
  Anyone who can call the endpoint and knows (or guesses) a `thread_id` can
  read or continue that thread. Use function keys / your own auth in front of it.
- The in-memory checkpointer lives inside one worker process. It does **not**
  demonstrate cross-worker durability, restart survival, or production
  scale-out. Those need a **shared** checkpoint store (Azure Blob, Postgres,
  Cosmos DB, …); see [`persistent_agent_blob_table`](../persistent_agent_blob_table/)
  and [`production_persistent_agent`](../production_persistent_agent/).
- As in `platform_compat_sdk`, `ANONYMOUS` auth is a local-dev opt-in; production
  should use the default `FUNCTION` level.
