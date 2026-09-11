# Service Bus Agent — Queue-Driven LangGraph

This example drives a LangGraph graph from an **Azure Service Bus queue**
message via `LangGraphApp.register_service_bus`, instead of (or in addition to)
the HTTP endpoints. Each delivered message is mapped to a single graph
`invoke` call.

## When to use this example

Use a Service Bus trigger when work arrives as **messages** rather than
synchronous HTTP requests — background jobs, fan-out/fan-in pipelines, or
decoupling a producer from graph execution with a durable buffer and
at-least-once delivery. For request/response interactions where the caller
waits for the result, use the HTTP `register(...)` surface instead.

### HTTP vs Service Bus

| | HTTP (`register`) | Service Bus (`register_service_bus`) |
|---|---|---|
| Invocation | Synchronous `POST /api/graphs/{name}/invoke` | One graph run per queue/topic message |
| Caller gets result | Yes — in the HTTP response | No — use `result_handler` to route output |
| Delivery | At-most-once (request fails, caller retries) | At-least-once (binding abandons/retries) |
| Backpressure | Caller-driven | Queue buffers + Functions scale-out |
| Best for | Interactive request/response | Background jobs, decoupled pipelines |
| Errors | Returned to caller | **Not swallowed** → message abandoned/retried |

A single graph may be registered on **both** surfaces.

## Files

- `graph.py` — the compiled graph (single `handle` node)
- `function_app.py` — `register_service_bus` wiring for a queue trigger
- `host.json`, `local.settings.json.example`, `requirements.txt`

## App Settings

| Setting | Description | Default |
|---|---|---|
| `ServiceBusConnection` | App-setting name holding the Service Bus connection string / FQNS | — |
| `SERVICE_BUS_QUEUE_NAME` | Queue to bind the trigger to | `langgraph-jobs` |

## Local development

```bash
cp local.settings.json.example local.settings.json
# Fill in ServiceBusConnection with your namespace connection string

pip install -r requirements.txt
func start
```

Send a message to the `langgraph-jobs` queue (via the Azure portal, Service
Bus Explorer, or `az servicebus`). The function maps the message body to the
graph input and runs `invoke` once per message.

## Message mapping

By default (`default_message_mapper`), the message body is decoded as UTF-8 and
parsed as JSON:

- A JSON **object** is passed through as the graph input verbatim.
- Any other value (or non-JSON text) is wrapped as
  `{"messages": [{"role": "human", "content": <text>}]}`.

Pass `input_mapper=...` to `register_service_bus` to customize this.

## Thread state and locking

By default runs are **threadless** — no `thread_id` is derived and no lock is
taken. Pass `thread_id_factory=...` to derive a checkpoint `thread_id` from a
message; when it returns a value **and** the graph has a checkpointer, the
app-level `thread_lock` guards the run so concurrent deliveries for the same
logical thread do not mutate checkpoints concurrently. If the lock cannot be
acquired, a `ThreadContentionError` is raised so the binding abandons and
retries the message.

## Error handling

Graph exceptions are **not swallowed** — a failing run propagates so the
Service Bus binding abandons the message and it is retried / dead-lettered
according to your queue's delivery policy. This package ships **no** Service
Bus output binding; to publish a result, pass a `result_handler(result, msg)`.

## Telemetry

Message bodies and application-property values are **never** included in
telemetry. Runs carry a `trigger_type="service_bus"` correlation field on the
`RunContext` so Service Bus-driven runs are distinguishable from HTTP runs in
your observer.
