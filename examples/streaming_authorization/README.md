# Streaming authorization (allowed versus denied)

An offline, deterministic comparison of an **authorized** and a **denied** request to a
**true-streaming** route, using the `authorize=` callback of
[`StreamingLangGraphApp`](../../src/azure_functions_langgraph/streaming.py). No network,
no LLM key, no Functions host is needed to see the behavior.

It builds on [`true_streaming_agent`](../true_streaming_agent/) (same transport and
prerequisites) by adding one thing: a per-graph policy.

## Two different layers

| Layer | Set by | Answers | Proves user / thread ownership? |
| --- | --- | --- | --- |
| Azure Functions host key | `auth_level=func.AuthLevel.FUNCTION` | "Does the caller hold an application key?" | **No** — a key is shared application access, not a user identity |
| `authorize=` callback | `policy.py` | "May *this principal* run *this graph* on *this thread*?" | Only as much as your policy verifies from trusted storage |

The callback receives `StreamingAuthorizationContext` (`action`, `graph_name`,
`thread_id`, and the original ASGI `request`). It runs **once per request, after the
body is validated and before any thread lock, checkpoint, or graph work**. Return
`True` to admit, `False` for a JSON `403`, raise `UnauthenticatedError` for a `401`;
any other exception fails closed as `500`. Denial bodies are sanitized
(`{"error": "error", "detail": "Access denied", "request_id": ...}`) and never include
graph state, thread ids, or credentials. See
[true-stream authorization](../../docs/security.md#true-stream-authorization).
Revocation affects later requests, not an already-admitted stream.

> The tokens and the ownership table in `policy.py` are **fake demo data**. They are
> not token verification. A real policy must validate a signed token (or trust an
> ingress that does) and read ownership from storage the caller cannot influence;
> never trust caller-supplied identity headers or owner fields.

## Files

- `graph.py` — fake graph that records every execution in `EXECUTIONS`
- `policy.py` — the demo `authorize` callback
- `function_app.py` — `StreamingLangGraphApp(auth_level=FUNCTION, authorize=authorize)`
- `demo.py` — drives the registered stream handler offline and prints the comparison
- `host.json`, `local.settings.json.example`, `requirements.txt`

## Run the offline comparison

Requires the `streaming` extra (`pip install "azure-functions-langgraph[streaming]"`).

```bash
cd examples/streaming_authorization
python demo.py
```

```text
allowed (alice on her own thread): status=200 graph_runs=1
  body: event: data | data: {"respond": {...}} |  | event: end | data: {}
denied 401 (no credential): status=401 graph_runs=0
  body: {"error":"error","detail":"Access denied","request_id":"demo"}
denied 403 (bob on alice's thread): status=403 graph_runs=0
  body: {"error":"error","detail":"Access denied","request_id":"demo"}
denied 403 (alice, threadless run): status=403 graph_runs=0
  body: {"error":"error","detail":"Access denied","request_id":"demo"}
```

`graph_runs=0` on every denial shows the graph never started.

## Run under the Functions host

```bash
pip install -r requirements.txt
cp local.settings.json.example local.settings.json
func start
```

Core Tools does not enforce function keys locally; the host key layer can only be
verified on a deployed host. Requires runtime 4.34.1+ and
`PYTHON_ENABLE_INIT_INDEXING=1`. This example's offline checks do not prove real
Azure behavior.

## Test

```bash
pytest tests/test_streaming_authorization_example.py
```

The test is skipped when the `streaming` extra is not installed.
