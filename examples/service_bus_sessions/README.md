# Service Bus Sessions — Offline Session-to-Thread Routing

Shows how `register_service_bus(..., session=True)` maps a Service Bus
**session id** to a graph checkpoint **`thread_id`**, and when the adapter
refuses a topology — using a **synthetic message object**. No Service Bus
namespace, credentials, network or extra runtime dependency is needed. For the
basic (non-session) queue example see [`service_bus_agent`](../service_bus_agent/).

> This is an offline illustration. It does **not** verify behaviour against a real
> Service Bus namespace or Azure.

## Run it

From the repository root (works against a source checkout, no `func`/Azure needed):

```bash
cd examples/service_bus_sessions
PYTHONPATH=../../src python session_demo.py
```

Expected output (abridged):

```text
same_session: {'seen': ['a', 'b'], 'turns': 2}
other_session: {'seen': ['c'], 'turns': 1}
redelivered: {'seen': ['a', 'b', 'b'], 'turns': 3}
missing_session: session mode requires a nonempty message session_id
missing_session_ran_graph: False
factory_mismatch: thread_id_factory must match message session_id in session mode
contention: Thread 'order-1' for graph 'agent' is currently in use; message will be retried by the broker
...
```

The scenarios are asserted by `tests/test_examples_smoke.py`:

```bash
PYTHONPATH=src python -m pytest tests/test_examples_smoke.py -k service_bus_sessions -q
```

## What it demonstrates

| Scenario | Behaviour |
|---|---|
| Two messages, same `session_id` | Same checkpoint `thread_id` → state accumulates (`turns == 2`) |
| Different `session_id` | Separate thread → independent state (`turns == 1`) |
| Missing / blank `session_id` | `ValueError` **before** the graph runs |
| `thread_id_factory` disagrees with `session_id` | `ValueError`; a factory may only repeat the session id |
| Redelivery of the same `message_id` | Graph runs **again** (`turns == 3`) — no dedup |
| Same thread already locked | `ThreadContentionError` so the broker retries |
| `session_exclusive=True` on an isolated app | Application thread lock is skipped |
| `session_exclusive=True` with HTTP graphs, a second entity, Platform/Durable routes, no `session=True`, or `cardinality != "one"` | Rejected at registration with `ValueError` |

## Who guarantees what

Keep these three mechanisms separate:

1. **Broker session ordering** — Service Bus delivers messages of one session
   to one receiver at a time, in order, while that receiver holds the session
   lock. This is a broker guarantee; the package neither provides nor renews it.
2. **Package thread lock** (`ThreadLock`) — an application-level lock keyed by
   `(graph, thread_id)` that guards checkpoint writes. In `session=True` mode it
   is **still taken by default** (it is not replaced by the session lock), with
   the bounded `lock_timeout` wait described in
   [`service_bus_agent`](../service_bus_agent/README.md#thread-state-and-locking).
   It is only skipped with `session_exclusive=True`, an explicit operator
   assertion that this entity is the single checkpoint writer and session
   ownership lasts through execution. That assertion is not verified against
   Azure here.
3. **At-least-once delivery** — neither of the above makes processing
   exactly-once. A failed or lock-expired delivery is redelivered and the graph
   runs again; make graph side effects and result handlers idempotent.

The demo's `MemorySaver` checkpointer is in-process only; use a durable
checkpointer for real deployments.

## Real wiring

`function_app.py` registers the same graph on a **session-enabled** queue
(`is_sessions_enabled` is set on the trigger by `session=True`). Copy
`local.settings.json.example`, set `ServiceBusConnection`, and `func start`.
That path requires a session-enabled queue in Azure and is not exercised by the
offline demo or tests.
