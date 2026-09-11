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

## Safety cap

A true stream is never buffered, so the buffered-response byte guard
(`LangGraphApp.max_stream_response_bytes`) does not apply. Instead
`StreamingLangGraphApp(max_stream_events=...)` bounds the number of frames a
single run may emit; exceeding it emits an `event: error` frame and stops.
