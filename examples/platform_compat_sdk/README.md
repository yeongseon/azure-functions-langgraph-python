# Platform-compatible SDK example

Demonstrates the **experimental, partial** compatibility path for
[`langgraph-sdk`](https://pypi.org/project/langgraph-sdk/) clients on Azure Functions,
not a drop-in replacement for LangGraph Platform.

`LangGraphApp(platform_compat=True)` exposes assistant discovery, thread metadata,
blocking wait, buffered stream and state operations. Check your client's exact
calls against the [SDK feature support matrix](../../COMPATIBILITY.md#sdk-feature-support-matrix)
before choosing this example; unsupported lifecycle routes are not added by this opt-in.

## Before choosing this example

- **Local defaults:** the graph's in-memory checkpointer, default Platform thread
  records and default lock are process-local. For restart persistence and shared
  writers, choose checkpoint storage, a shared `ThreadStore` and distributed
  locking separately; see the [SDK adoption checklist](../../docs/adapter-reference.md#sdk-adoption-checklist).
- **Retention:** `threads.delete` removes the thread record, not its checkpoints.
  Plan explicit checkpoint deletion or scheduled pruning, including cleanup for
  deleted thread IDs; see [checkpoint retention](../../docs/adapter-reference.md#checkpoint-retention-and-thread-deletion).
- **Search cost:** metadata filtering runs in-process. Unfiltered searches,
  metadata-filtered searches and counts still scan Azure Table even with its
  optional status index; see [search and scale](../../docs/adapter-reference.md#search-and-scale).
- **Lifecycle and streaming:** `runs.create/get/list/cancel/delete/join/join_stream`
  have no registered Platform routes (HTTP 404, rather than a run handle or a 501
  feature response). `runs.stream` delivers buffered SSE after graph execution,
  not incremental tokens. See [SDK lifecycle and transport](../../docs/adapter-reference.md#sdk-lifecycle-and-transport).

## Files

- `function_app.py` — registers `echo_agent` with `platform_compat=True`
- `graph.py` — minimal echo `StateGraph` with an in-memory checkpointer
- `sdk_client.py` — async script using `langgraph-sdk` to create a thread, run it, and retrieve state
- `host.json`, `local.settings.json.example`, `requirements.txt`

## Run locally

```bash
cd examples/platform_compat_sdk
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

In a second terminal:

```bash
pip install langgraph-sdk
python sdk_client.py
```

## Captured output

This is `python sdk_client.py` output with only dynamic thread/checkpoint IDs
and timestamps normalized; all other fields come from the registered handlers.

```text
assistants: ["echo_agent"]
thread: {"thread_id":"<thread_id>","created_at":"<timestamp>","updated_at":"<timestamp>","metadata":null,"status":"idle","values":null,"assistant_id":null,"interrupts":{}}
result: {"messages":[{"role":"human","content":"Hello, SDK!"},{"role":"assistant","content":"Echo: Hello, SDK!"}]}
state: {"values":{"messages":[{"role":"human","content":"Hello, SDK!"},{"role":"assistant","content":"Echo: Hello, SDK!"}]},"next":[],"checkpoint":{"thread_id":"<thread_id>","checkpoint_ns":"","checkpoint_id":"<checkpoint_id>","checkpoint_map":null},"metadata":{"source":"loop","step":1,"parents":{}},"created_at":"<timestamp>","parent_checkpoint":{"thread_id":"<thread_id>","checkpoint_ns":"","checkpoint_id":"<checkpoint_id>","checkpoint_map":null},"tasks":[],"interrupts":[]}
```

The in-memory checkpointer is for this local demonstration only; it does not
survive host restarts. See [COMPATIBILITY.md](../../COMPATIBILITY.md) for the
supported SDK surface and storage notes.

## Verify with curl

```bash
# List registered assistants
curl -s -X POST http://localhost:7071/api/assistants/search \
  -H "Content-Type: application/json" -d '{}'

# Create a thread
curl -s -X POST http://localhost:7071/api/threads \
  -H "Content-Type: application/json" -d '{}'

# Threadless run
curl -s -X POST http://localhost:7071/api/runs/wait \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"echo_agent","input":{"messages":[{"role":"human","content":"Hi"}]}}'
```

## Production notes

- `LangGraphApp` now defaults to `AuthLevel.FUNCTION`. This example opts into `AuthLevel.ANONYMOUS` for local convenience, which emits an unconditional `UserWarning`. For production, drop the `auth_level=` kwarg (uses the FUNCTION default) and pass `?code=<FUNCTION_KEY>` on every request.
- The platform-compatible endpoints share the same buffered-SSE behavior as the native `/stream` endpoint — see [docs/production-guide.md](../../docs/production-guide.md#streaming-behavior).
- See [COMPATIBILITY.md](../../COMPATIBILITY.md) for the per-feature SDK matrix and which `RunCreate` fields return `501 Not Implemented`.
