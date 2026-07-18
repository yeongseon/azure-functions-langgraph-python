# Operations & API Surface

This page consolidates the operational details of three subsystems whose behavior
was previously only discoverable by reading source and examples:

1. [Distributed thread locks](#distributed-thread-locks) — coordinating
   `(graph_name, thread_id)` access across Function App instances.
2. [Streaming guarantees & failure modes](#streaming-guarantees-failure-modes) —
   what the buffered SSE endpoints promise and how they fail.
3. [RunCreate field → status mapping](#runcreate-field-status-mapping) — why the
   platform (`/threads/.../runs`) endpoints return 501 / 422 / 409.

For the full request/response reference, see the [API Reference](api.md).

---

## Distributed thread locks

The native `invoke` / `stream` endpoints guard concurrent access to the same
`(graph_name, thread_id)` so that single-writer checkpointers (for example
`AzureBlobCheckpointSaver`) never see racing writes for one thread.

### The `ThreadLock` protocol

`azure_functions_langgraph.locks.ThreadLock` is a `runtime_checkable`
[`Protocol`][protocol] with two methods:

| Method | Signature | Contract |
| --- | --- | --- |
| `acquire` | `acquire(graph_name: str, thread_id: str, timeout: float = 0.0) -> bool` | Attempt an exclusive lock. `timeout=0.0` (default) is non-blocking; positive values block up to `timeout` seconds. Returns `True` on success, `False` if the lock is held elsewhere (same process **or**, for distributed backends, another instance). |
| `release` | `release(graph_name: str, thread_id: str) -> None` | Release a previously held lock. Must be safe to call even when the lock is not held — implementations log at DEBUG rather than raising, so a handler's `finally` never masks the underlying request failure. |

Two implementations ship in the box:

| Backend | Class | Scope |
| --- | --- | --- |
| In-process (default) | `InProcessThreadLock` | Correct **only within a single Python worker process**. |
| Distributed | `AzureBlobLeaseThreadLock` | Correct across instances via Azure Blob lease compare-and-swap. |

Any third-party backend (Redis, Cosmos DB, …) that satisfies the protocol can be
plugged in via `LangGraphApp(thread_lock=...)`.

!!! warning "The default is single-process only"
    Azure Functions Consumption and Elastic Premium plans scale horizontally.
    Two Function App instances processing requests for the same `thread_id` will
    silently race under the in-process default. **Multi-instance deployments must
    supply a distributed backend.** See the
    [production guide](production-guide.md) scale-out matrix.

### `AzureBlobLeaseThreadLock`

Coordinates locking across instances by holding an exclusive Azure Blob **lease**
on a per-thread marker blob. Each `(graph_name, thread_id)` maps to one blob;
acquiring the lock means holding its lease, releasing the lock releases the lease.

Requires the optional dependency:

```bash
pip install azure-functions-langgraph[azure-blob]
```

#### Constructor parameters

All parameters are keyword-only.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `container_client` | `azure.storage.blob.ContainerClient` | *required* | Must already exist — the lock never creates the container. |
| `lease_duration` | `int` | `60` | Seconds. Must be `15`–`60` (finite) or `-1` (infinite). Any other value raises `ValueError`. |
| `blob_prefix` | `str` | `"thread-locks/"` | Prefix grouping lock blobs inside the container. |
| `auto_renew` | `bool` | `True` | Start a per-instance daemon thread that renews every active lease at `lease_duration / 3` intervals. See below. |

#### Lease renewal & the `auto_renew` trade-off

With `auto_renew=True` (the default) a background daemon thread renews every
active lease at `lease_duration / 3` intervals until `close()` (or process exit),
so execution time is **not** bounded by `lease_duration`.

With `auto_renew=False` a finite lease silently expires mid-execution once
`lease_duration` seconds elapse, letting another instance acquire the same lock —
which allows concurrent writes to single-writer checkpointers. Construction
therefore emits a `UserWarning` when `auto_renew=False` and `lease_duration` is
finite. `auto_renew` is ignored for `lease_duration=-1` (infinite leases are not
renewable).

| `lease_duration` | Crash recovery | Long runs (>60 s) |
| --- | --- | --- |
| Finite (`15`–`60`), `auto_renew=True` | Lease auto-expires on the service if the host crashes → another instance recovers. | Safe — renewal extends the lease. |
| Finite, `auto_renew=False` | Same crash recovery. | **Unsafe** — lease expires mid-run (emits `UserWarning`). |
| Infinite (`-1`) | No auto-recovery — an operator must **break** the lease manually after a host crash. | Safe — never expires. |

#### Example: managed identity

```python
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

# The container must already exist; create it once in infra/deploy code.
container = ContainerClient(
    account_url="https://<account>.blob.core.windows.net",
    container_name="thread-locks",
    credential=DefaultAzureCredential(),
)

lock = AzureBlobLeaseThreadLock(
    container_client=container,
    lease_duration=60,   # finite + auto_renew keeps long runs safe
    auto_renew=True,
)

app = LangGraphApp(thread_lock=lock)
```

Required RBAC on the container (or storage account): **Storage Blob Data
Contributor** — the identity must read, write (create the marker blob), and manage
leases.

#### Production checklist

- [ ] Set `AZFUNC_LANGGRAPH_LOCK_BACKEND=distributed` (or pass `thread_lock=`
      explicitly) on **any** multi-instance plan (Consumption, Elastic Premium).
- [ ] Provision the lock container ahead of time — the lock never creates it.
- [ ] Grant the app identity **Storage Blob Data Contributor** on the container.
- [ ] Choose `lease_duration`: keep the default `60` with `auto_renew=True` for
      most workloads; use `-1` (infinite) only if you have an operational runbook
      to break stale leases after a host crash.
- [ ] Monitor for `Failed to renew Azure Blob lease` warnings — they mean a lease
      was dropped and another instance may now acquire the lock.

---

## Streaming guarantees & failure modes

The streaming endpoints (`POST /api/graphs/{name}/stream` and the platform
`POST /api/threads/{thread_id}/runs/stream`) are **buffered** Server-Sent Events:
the graph is fully executed and every event accumulated before the response is
returned. They are *not* incremental — the client receives the whole event log at
once.

| Guarantee | Value |
| --- | --- |
| Content-Type | `text/event-stream` |
| Headers | `Cache-Control: no-cache`, `X-Accel-Buffering: no`. Platform thread streams also add `Content-Location: /api/threads/{thread_id}/runs/{run_id}`; threadless platform streams use `/api/runs/{run_id}`; the native `POST /api/graphs/{name}/stream` sets **no** `Content-Location`. |
| Buffer limit | `max_stream_response_bytes` (see [configuration](configuration.md)) |
| Terminal event | Always an `event: end` frame, on both success and failure |

### Overflow & error behavior

The handler tracks cumulative buffered bytes. If appending the next event would
exceed `max_stream_response_bytes`, it stops the graph and **injects** a terminal
error rather than truncating silently:

```text
event: error
data: {"error": "stream response exceeded max buffered size (<N> bytes)"}

event: end
```

The response still returns **HTTP 200** with `Content-Type: text/event-stream` —
the error is delivered *in-band* as an SSE `event: error`. Even though the body is
fully buffered before it is returned, HTTP 200 is an intentional contract choice: a
client always receives a well-formed SSE log and must inspect the events (not the
HTTP status) to detect failure. The same in-band pattern
applies when the graph itself raises: an `event: error` (`"stream processing
failed"`) followed by `event: end`. In all overflow/error cases the thread run
lock is released with `status="error"`.

!!! note "Need true incremental streaming?"
    The buffered model bounds memory and guarantees a well-formed event log, but
    it defeats token-by-token streaming. If you need incremental delivery, front
    the graph with your own streaming HTTP function that yields directly from
    `graph.stream(...)` — the buffered endpoints are optimized for correctness and
    bounded memory, not latency.

---

## RunCreate field → status mapping

The LangGraph Platform-compatible endpoints (`/threads/{thread_id}/runs[/stream|/wait]`)
accept a `RunCreate` body. Not every field is supported in this release; the table
below maps common causes to response codes.

| Cause | Status | Enforced in |
| --- | --- | --- |
| `interrupt_before` set | **501** | `platform/_common.py` preflight |
| `interrupt_after` set | **501** | `platform/_common.py` preflight |
| `webhook` set | **501** | `platform/_common.py` preflight |
| `on_completion` set | **501** | `platform/_common.py` preflight |
| `after_seconds` set (delayed runs) | **501** | `platform/_common.py` preflight |
| `if_not_exists` set | **501** | `platform/_common.py` preflight |
| `checkpoint_id` set (resumption) | **501** | `platform/_common.py` preflight |
| `command` set (command resumption) | **501** | `platform/_common.py` preflight |
| `feedback_keys` set | **501** | `platform/_common.py` preflight |
| `multitask_strategy` other than `"reject"` | **501** | `platform/_common.py` preflight |
| `stream_mode` list with more than one element | **501** | `platform/_runs.py` (multi-stream-mode not supported) |
| Malformed body / schema validation failure | **422** | `platform/_runs.py` validation |
| `thread_id` supplied on a threadless run | **422** | `platform/_runs.py` validation |
| Thread already has an in-flight run (concurrent run) | **409** | `platform/_runs.py` (`try_acquire_run_lock`) |
| Unknown `thread_id` | **404** | `platform/_runs.py` |
| Graph execution raised | **500** | `platform/_runs.py` |

501 responses carry a `{"detail": "... is not supported in this release."}` body
so clients can distinguish "unsupported feature" from "bad request". Concurrent
runs are rejected because `multitask_strategy=reject` is the only supported
strategy — one in-flight run per thread.

[protocol]: https://docs.python.org/3/library/typing.html#typing.Protocol
