# True streaming agent

A LangGraph agent served with **true** incremental HTTP streaming via
[`StreamingLangGraphApp`](../../src/azure_functions_langgraph/streaming.py).

Every other example in this repo uses `LangGraphApp`, whose `/stream` endpoint
returns **buffered** SSE — chunks are collected during the run and flushed only
after it completes. This example instead uses `StreamingLangGraphApp`, which
flushes each `event: data` frame **as the graph produces it**, so a client sees
partial output arrive incrementally.

## How it differs from the buffered examples

| | `LangGraphApp` (buffered) | `StreamingLangGraphApp` (true streaming) |
|---|---|---|
| Transport | Classic `HttpRequest`/`HttpResponse` | FastAPI/ASGI (`azurefunctions-extensions-http-fastapi`) |
| `/stream` delivery | Collected, flushed after the run | Each frame flushed as produced |
| Runtime floor | Any supported | **4.34.1+** |
| Extra required | none | `azure-functions-langgraph[streaming]` |
| Mixing with classic routes | n/a | **No** — the whole app is ASGI |
| State endpoint | included | intentionally excluded (use `LangGraphApp`) |

> **App-wide switch.** Enabling true streaming converts the **entire** function
> app to the ASGI streaming model. You cannot mix classic and streaming routes
> in one app — deploy a separate app if you also need the classic buffered
> surface.

## Prerequisites

- Azure Functions runtime **4.34.1+**
- `pip install "azure-functions-langgraph[streaming]"`
- App setting **`PYTHON_ENABLE_INIT_INDEXING=1`** (see `local.settings.json.example`)
- On Linux Consumption with Python 3.12 or earlier, app setting
  **`PYTHON_ISOLATE_WORKER_DEPENDENCIES=1`**

## Run locally

```bash
pip install -r requirements.txt
cp local.settings.json.example local.settings.json
func start
```

## Prove incremental delivery with `curl -N`

`-N` disables curl's output buffering so you can watch frames land one at a time.
The graph is a three-step pipeline. With `stream_mode: "updates"` you receive one
`event: data` frame per step (the node's delta), then a terminal `event: end`:

```bash
curl -N -X POST "http://localhost:7071/api/graphs/true_streaming_agent/stream?code=<FUNCTION_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "hello world"}], "tokens": []}, "stream_mode": "updates"}'
```

```text
event: data
data: {"step_1": {"tokens": ["Echo:"]}}

event: data
data: {"step_2": {"tokens": ["Echo:", "hello"]}}

event: data
data: {"step_3": {"tokens": ["Echo:", "hello", "world"], "messages": [...]}}

event: end
data: {}
```

Each block appears as its step finishes — not all at once at the end. Swap
`stream_mode` in the request body (`"values"`, `"updates"`, `"messages"`, ...) to
change the event shape; the framing (`event: data` / `event: end`, plus
`event: error` if the graph raises mid-stream) is unchanged.

## Endpoints

- `POST /api/graphs/true_streaming_agent/invoke` — synchronous invocation (JSON)
- `POST /api/graphs/true_streaming_agent/stream` — **true** incremental SSE
- `GET /api/health` — anonymous liveness probe (`{"status": "ok"}`)
- `GET /api/health/details` — registered-graph inventory (protected)

> The Platform-compatible and thread-state surfaces are **not** part of the
> streaming app. If you need those, keep serving them from a classic
> `LangGraphApp` deployment.

## Resource authorization

This demonstration does not install an ownership policy. `FUNCTION` keys grant
application access, **not** verified user identity or thread ownership. Before
serving private state, pass `authorize=policy` to `StreamingLangGraphApp` and verify
the principal, graph entitlement and thread ownership using trusted application
storage. See the [ASGI policy example](../../docs/security.md#true-stream-authorization)
and [typed API](../../docs/api.md#streaming-authorization-experimental).
Do not trust caller-supplied identity headers or owner fields. The optional hook
receives `StreamingAuthorizationContext.request` (an ASGI request), runs once
before graph/checkpoint work and locking, and returns JSON 401/403/500 on denial
or failure. Omitting it preserves this example's permissive resource behavior.
Revocation affects later requests, not an already-admitted stream.

## Safety cap

A true stream is never buffered, so the buffered-response byte guard
(`LangGraphApp.max_stream_response_bytes`) does not apply. Instead
`StreamingLangGraphApp(max_stream_events=...)` bounds the number of frames a
single run may emit; exceeding it emits an `event: error` frame and stops.

## Disconnect and cancellation contract

The thread lock protects graph execution rather than the lifetime of the HTTP
connection. If a client disconnects while a synchronous graph is producing a
frame, the adapter waits for that one in-flight `next()` call to return, closes
the iterator, releases the lock exactly once, records the run as failed, and
then propagates cancellation. A second request for the same checkpoint thread
cannot enter during that cleanup window. Cancellation before the first frame,
between frames, and during frame production follows the same ordering.

Python cannot forcibly terminate a running worker thread. Each stream tracks at
most one synchronous iterator worker, so repeated disconnects do not create
unbounded workers for one stream; however, a graph node or tool that never
returns keeps its lock and worker occupied until process termination. Configure
bounded timeouts in every synchronous LLM, tool, and storage call. Async graphs
use native async-generator cancellation and do not use this thread bridge.

## Python local client and offline checks

From the repository root, `make example-sse-client` calls the local host above
with a standard-library client (Python 3.11+). It uses a 30-second socket timeout,
checks the SSE content type, bounds each frame to 1 MiB of UTF-8 bytes (including
line delimiters; a CR reserves both CRLF bytes conservatively) and closes the response
on normal completion, an error, or Ctrl+C. No keys or cloud credentials are used.
This local demonstration is not a production retry/authentication policy.

The parser handles UTF-8 split across network reads, multiple frames in one
read, LF/CRLF/CR line endings, comments and multi-line data. It JSON-decodes only
complete blank-line-delimited frames. A malformed JSON event, server `error`
event or connection close before `end` fails rather than silently succeeding.
It consumes the adapter's JSON SSE dialect, not a generic browser EventSource
reconnection implementation; it does not reconnect or replay runs.

`make example-sse-client CLIENT_ARGS="--stop-after 1"` closes the client after
one event. Closing a client does not prove the graph stopped or undo effects;
see the disconnect contract above and bound timeouts in graph tools.
`make example-sse-client-check` runs fake-data parser/disconnect tests without
Core Tools, Azure or model credentials. These tests verify client consumption,
not server-side cancellation, lock cleanup or incremental host delivery.

The same framing can occur on classic buffered SSE, but parsing it cannot make
a buffered response incremental. True incremental delivery still needs the
separate ASGI app and the runtime/settings listed above.
