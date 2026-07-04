# Platform-compatible SDK example

Demonstrates the package's headline feature: serving compiled LangGraph graphs as a drop-in target for [`langgraph-sdk`](https://pypi.org/project/langgraph-sdk/) clients on Azure Functions.

`LangGraphApp(platform_compat=True)` exposes the LangGraph Platform-compatible REST surface (`/assistants`, `/threads`, `/runs`, `/threads/{id}/state`, …) so existing `langgraph-sdk` code targeting LangGraph Cloud works against your local `func start` host.

## Files

- `function_app.py` — registers `echo_agent` with `platform_compat=True`
- `graph.py` — minimal echo `StateGraph`
- `sdk_client.py` — async script using `langgraph-sdk` to call `assistants.search`, `threads.create`, `runs.wait`
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
