# Production Guide

This guide focuses on production hardening for `azure-functions-langgraph` deployments on Azure Functions.

## Authentication & Authorization
### Default auth behavior

`LangGraphApp` defaults to function-key HTTP access:

```python
# src/azure_functions_langgraph/app.py
auth_level: func.AuthLevel = func.AuthLevel.FUNCTION
```

This default is intentionally secure: deploying a `LangGraphApp()` without any auth configuration will require a function key on every request.

If you opt into `ANONYMOUS` at the app level, the library emits an unconditional `UserWarning` at construction time (regardless of environment). This ensures the choice is always visible in test output, CI logs, and startup output.

```python
# Opting into anonymous access (e.g. local dev) emits UserWarning:
# UserWarning: LangGraphApp is using ANONYMOUS auth. ...
app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
```

⚠️ `ANONYMOUS` is convenient for local development but too permissive for internet-facing production APIs.

### Set a production-safe app-level auth level

`FUNCTION` is the default (recommended baseline). Use `ADMIN` only for tightly controlled internal surfaces.

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

app = LangGraphApp()                                        # default: FUNCTION
app = LangGraphApp(auth_level=func.AuthLevel.ADMIN)          # only if truly internal
```

### Per-graph auth override

You can override auth for specific graphs via `register()`:

```python
import azure.functions as func

app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)

# Production default for private endpoints
app.register(graph=internal_graph, name="internal", auth_level=func.AuthLevel.FUNCTION)

# Explicitly public endpoint (only when intended)
app.register(graph=public_graph, name="public", auth_level=func.AuthLevel.ANONYMOUS)
```

Use per-graph overrides to keep a strict default while exposing narrowly scoped public routes.

⚠️ **Scope**: Per-graph `auth_level` overrides apply only to **native routes** (`/api/graphs/{name}/invoke`, `/api/graphs/{name}/stream`).
When `platform_compat=True` is enabled, all platform-compatible routes (`/api/threads/...`, `/api/runs/...`) use the **app-level** `auth_level` regardless of per-graph overrides.

### API Management integration pattern

For production, a common pattern is:

1. Set Function routes to `FUNCTION` auth.
2. Place Azure API Management (APIM) in front of the Function App.
3. Require client auth at APIM (JWT/OAuth2/subscription key/mTLS).
4. Keep Function keys private between APIM and Function App.
5. Apply APIM rate limits, IP filtering, and request validation policies.

This creates a layered security model: edge auth and governance in APIM, key-based gate at the Function layer.

- [Azure Functions authentication and authorization](https://learn.microsoft.com/en-us/azure/azure-functions/security-concepts)

## Observability
### Logging integration (recommended operator instrumentation)

This package does not emit structured log fields automatically.
The following are recommended practices for production observability when building your Function App:

- Emit structured fields (`graph_name`, `thread_id`, `assistant_id`, `run_id`, `status_code`, `duration_ms`).
- Log explicit lifecycle markers: request received, graph started, graph completed/failed.
- Include error categories (`validation_error`, `execution_error`, `storage_error`) to simplify alerting.

[`azure-functions-logging-python`](https://github.com/yeongseon/azure-functions-logging-python) provides structured logging helpers that pair well with this package.

### Application Insights correlation

Azure Functions automatically wires requests/dependencies into Application Insights when enabled in the Function App.
Use this to correlate:

- incoming HTTP request
- graph invocation log records
- downstream dependency calls (LLM/API/storage)
- final success/error outcome

This enables end-to-end traceability for each run.

### Health endpoint

`GET /health` (exposed as `GET /api/health` with the default Functions route prefix) returns a liveness and configuration response.
It confirms the app is running and lists registered graphs with their checkpointer status.

⚠️ This is a **liveness/configuration endpoint**, not a dependency-readiness check.
It does not probe Blob Storage, Table Storage, or downstream LLM availability.
For deep health checks, implement a custom endpoint or use Azure Monitor availability tests.

The `/health` endpoint inherits the **app-level** `auth_level`, not per-graph overrides.
If the app uses `FUNCTION` auth, `/health` also requires a function key — even if individual graphs are `ANONYMOUS`.

The response includes a list of graphs and whether each has a checkpointer.

```json
{
  "status": "ok",
  "graphs": [
    {
      "name": "my_agent",
      "description": "Customer support agent",
      "has_checkpointer": true
    }
  ]
}
```

Use this endpoint for liveness checks and deployment validation.

Monitor health check success rates, response-time percentiles (P50/P95/P99), and HTTP error rates by endpoint and graph name.

## Timeouts & Cancellation
### Azure Functions timeout limits

Execution timeout is governed by Azure Functions plan and `host.json`:

| Plan | Default timeout | Maximum timeout |
|------|-----------------|-----------------|
| Consumption | 5 minutes | 10 minutes |
| Flex Consumption | 30 minutes | Unlimited (configurable) |
| Premium | 30 minutes | Unlimited (configurable) |
| Dedicated (App Service) | 30 minutes | Unlimited |

### Runtime behavior in this package

Graph execution is synchronous from the HTTP handler perspective.
`graph.invoke()` and `graph.stream()` run until completion (or failure).

- No package-level timeout wrapper is applied around graph calls.
- No built-in cancellation endpoint is provided for long-running graph runs.

⚠️ If a graph exceeds platform timeout, the request fails at the Functions host boundary.

⚠️ **HTTP response ceiling**: Azure Functions enforces a hard **230-second** limit on HTTP response time regardless of `functionTimeout`.
Graph invocations that exceed 230 seconds will fail with a gateway timeout even if `functionTimeout` allows longer execution.
For workloads approaching this limit, consider async patterns (queue trigger + status polling) instead of synchronous HTTP.

### Configure timeout explicitly

Set `functionTimeout` in `host.json` to a value aligned with your plan and workload SLO.

```json
{
  "version": "2.0",
  "functionTimeout": "00:10:00",
  "logging": {
    "applicationInsights": {
      "samplingSettings": {
        "isEnabled": true,
        "excludedTypes": "Request"
      }
    }
  }
}
```

Production guidance:

1. Keep graph completion comfortably under timeout limits.
2. Cap upstream LLM/tool call timeouts so they fail fast.
3. Break long workflows into resumable steps via checkpointers.
4. Route very long orchestration to Durable Functions patterns when needed.

## Request & Input Limits

`LangGraphApp` enforces the following defaults to protect against oversized or deeply nested payloads:

| Limit | Default | Config parameter |
|-------|---------|------------------|
| Request body size | 1 MiB | `max_request_body_bytes` |
| Stream response size | 1 MiB | `max_stream_response_bytes` |
| Input JSON depth | 32 levels | `max_input_depth` |
| Input JSON nodes | 10,000 | `max_input_nodes` |

Override these in `LangGraphApp` constructor if your workload requires larger payloads:

```python
app = LangGraphApp(
    max_request_body_bytes=2 * 1024 * 1024,  # 2 MiB
    max_stream_response_bytes=4 * 1024 * 1024,  # 4 MiB
)
```

Requests exceeding these limits are rejected before graph execution begins.

## Streaming Behavior
### Current behavior (critical)

Endpoints with `stream` in the path and `Content-Type: text/event-stream` return SSE-formatted payloads,
but delivery is buffered in Azure Functions Python today.

This affects native and platform routes such as:

- `POST /api/graphs/{name}/stream`
- `POST /api/threads/{thread_id}/runs/stream`
- `POST /api/runs/stream`

⚠️ Clients receive the complete SSE body at once, not incremental chunks.

⚠️ **Buffered SSE response limit**: Stream responses are capped at **1 MiB** (`max_stream_response_bytes=1_048_576` by default).
If the accumulated SSE payload exceeds this limit, an `event: error` is injected into the SSE body rather than an HTTP 413/500.
Adjust `max_stream_response_bytes` in `LangGraphApp` if your graph produces large streaming output.

### Why this happens

This is an Azure Functions Python worker limitation for HTTP streaming/chunked transfer.
The package currently collects stream events and returns a single response body.

### Operational recommendation

For long-running production runs, prefer:

- `POST /api/threads/{thread_id}/runs/wait`
- `POST /api/runs/wait`

over the corresponding `/stream` routes to avoid UX and latency expectation mismatch.

⚠️ **Note**: Thread and run routes (`/api/threads/...`, `/api/runs/...`) are only available when `platform_compat=True` is set in `LangGraphApp`.

## Concurrency & Scale
### Thread-assistant binding and TOCTOU race

Platform routes bind a thread to its first assistant and reject assistant switches later.
There is an explicit TOCTOU window between read and update in `platform/routes.py`.

⚠️ **Operator impact**: In multi-instance deployments, concurrent requests for the same thread can race between read and write.
Without external serialization (e.g., queue-based workers), the second writer may silently overwrite the first.

See `DESIGN.md` (thread-assistant binding design decision and concurrency notes) for detailed constraints and trade-offs.

### Blob checkpointer single-writer assumption

`AzureBlobCheckpointSaver` is designed around single-writer-per-thread semantics.
The implementation documents concurrent-writer conflict resolution as a non-goal.

⚠️ **Operator impact**: Concurrent writes to the same thread/checkpoint namespace from multiple instances can produce inconsistent checkpoint state.
If your deployment runs multiple Function App instances, ensure each thread's writes are serialized (see Recommended production pattern below).

### Azure Table thread store scale envelope

`AzureTableThreadStore` uses a single partition key (`PartitionKey="thread"`) with client-side metadata filtering.
This is a design-envelope approximation, not an enforced limit, and generally works well up to roughly 100K threads (~500 entities/sec throughput).

Beyond that envelope, consider a sharded or higher-scale backend such as Cosmos DB.

### Concurrency controls

Only `multitask_strategy="reject"` is supported.
Concurrent run submissions for the same thread are rejected with HTTP 409 — no queuing or interruption is implemented.

**Operator impact**: If your workload has bursts of concurrent requests targeting the same thread,
implement client-side retry with backoff, or use the queue-based worker pattern below.

### Recommended production pattern

For multi-instance writes, serialize thread mutations through a queue-based worker pattern:

1. HTTP layer validates and enqueues run requests by `thread_id`.
2. Workers process one message per thread key at a time.
3. Storage writes remain effectively single-writer per thread.
4. Completion status is written back for polling/webhook delivery.

This removes most race windows and aligns with current storage assumptions.

### Distributed thread locking

The native invoke/stream endpoints (`POST /api/graphs/{name}/invoke` and `.../stream`) guard concurrent writes to the same `(graph_name, thread_id)` with a **per-thread lock**. The default backend, `InProcessThreadLock`, is a `threading.Lock` behind a small book-keeping layer — correct for single-instance deployments, but silently unsafe in horizontally scaled ones (two Function App instances can each acquire their own in-process lock for the same `thread_id`).

For multi-instance deployments, `LangGraphApp` accepts any object satisfying the [`ThreadLock`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/src/azure_functions_langgraph/locks/base.py) protocol via the `thread_lock` constructor argument. The package ships one distributed implementation — `AzureBlobLeaseThreadLock` — which uses **Azure Blob lease compare-and-swap** as the underlying primitive; leases naturally expire (15–60 s) so a crashed host cannot orphan a lock indefinitely.

> ⚠️ **Non-renewal caveat.** `AzureBlobLeaseThreadLock` does **not** renew leases in the background. If graph execution exceeds `lease_duration` seconds, the lease silently expires mid-execution and another instance can acquire the same lock, allowing concurrent writes to single-writer checkpointers. Pass `lease_duration=-1` (infinite) whenever graph execution can exceed 60 seconds (the maximum finite lease Azure allows). Construction emits a `UserWarning` for finite `lease_duration` so the trade-off is visible in test and CI output. Auto-renewal is tracked as a future enhancement.

```python
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

container = ContainerClient(
    account_url="https://<account>.blob.core.windows.net",
    container_name="langgraph-locks",
    credential=DefaultAzureCredential(),
)
container.create_container()  # idempotent — safe to call on cold start

app = LangGraphApp(
    auth_level=func.AuthLevel.FUNCTION,
    thread_lock=AzureBlobLeaseThreadLock(
        container_client=container,
        lease_duration=60,  # seconds; 15–60 or -1 for infinite
        blob_prefix="thread-locks/",
    ),
)
```

The Blob backend needs `Storage Blob Data Contributor` on the lock container (or narrower `Storage Blob Delegator` scope for lease-only workflows).

**Scale-out matrix.**

| Backend | Distributed? | Infra required | Failure mode | Use case |
| --- | --- | --- | --- | --- |
| `InProcessThreadLock` (default) | No | None | Lock lost on worker restart; racing instances get separate locks | Local dev, single-instance production |
| `AzureBlobLeaseThreadLock` | Yes — Azure Blob lease CAS | One Blob container | Lease expiry (15–60 s) reclaims locks after host crash; **no background renewal**, so finite leases cap safe graph runtime | Multi-instance (Consumption / Elastic Premium); prefer `lease_duration=-1` for long executions |
| Custom `ThreadLock` implementation | Yours to design | Yours to provision | Yours to reason about | Redis, Cosmos DB, Postgres advisory locks, etc. |

**Safety guard — `AZFUNC_LANGGRAPH_LOCK_BACKEND`.** In multi-instance environments, set this environment variable to `distributed` (or any non-empty value other than `inprocess`). When the value indicates a distributed backend is required and `thread_lock` resolves to the default `InProcessThreadLock`, `LangGraphApp.__post_init__` raises `RuntimeError` at construction — turning a silent-race deployment mistake into a fail-fast startup error. Set the value to `inprocess` (or leave it unset) in single-instance environments; the guard passes for any wired custom backend regardless of the environment value.

**Interaction with Platform-compatible runs.** `thread_lock` guards the *native* invoke/stream endpoints only. Platform-compatible runs (`platform_compat=True` with `AzureTableThreadStore`) use their own ETag-based run lock (`try_acquire_run_lock` / `release_run_lock` — see the *Run lock semantics* subsection under [Persistent storage in the README](https://github.com/yeongseon/azure-functions-langgraph-python#run-lock-semantics)) and are unaffected by the `thread_lock` argument. Deployments that expose both surfaces should configure both.


## Storage Configuration

### Azure Blob checkpointer

Install optional dependency:

```bash
pip install azure-functions-langgraph[azure-blob]
```

Configure with connection string from environment and construct a container client:

```python
import os

from azure.storage.blob import BlobServiceClient
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver

conn = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
service = BlobServiceClient.from_connection_string(conn)
container = service.get_container_client("langgraph-checkpoints")

checkpointer = AzureBlobCheckpointSaver(container_client=container)
```

Use `langgraph-checkpoints` as the default production container name unless you have environment-specific naming requirements.

If you already manage Azure clients elsewhere, pass a prepared container client directly.

### Azure Table thread store

Install optional dependency:

```bash
pip install azure-functions-langgraph[azure-table]
```

Configure with connection string and table name:

```python
import os

from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

conn = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
thread_store = AzureTableThreadStore.from_connection_string(
    connection_string=conn,
    table_name="langgraphthreads",
)
```

Use `langgraphthreads` as the default production table name unless you need a custom naming scheme.

### Checkpoint retention

`AzureBlobCheckpointSaver` lists checkpoints via blob prefix scans, so per-thread cost grows with the number of stored checkpoints. For long-lived threads, schedule a Timer-triggered Function to prune old checkpoints:

```python
import os

import azure.functions as func
from azure.storage.blob import ContainerClient

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

retention_app = func.FunctionApp()


@retention_app.schedule(
    schedule="0 0 3 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def prune_checkpoints(timer: func.TimerRequest) -> None:
    del timer
    container = ContainerClient.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"],
        "langgraph-checkpoints",
    )
    saver = AzureBlobCheckpointSaver(container_client=container)

    threads = AzureTableThreadStore.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"],
        table_name="langgraphthreads",
    )
    for thread in threads.search(limit=10_000):
        saver.delete_old_checkpoints(thread.thread_id, keep_last=50)
```

Both helpers only delete checkpoint marker, metadata, and write blobs. They intentionally preserve channel value blobs (under `values/`) and the `latest.json` pointer so retained checkpoints remain fully usable.

> **Note** — `delete_old_checkpoints` / `delete_checkpoints_before` are safe but **not exhaustive**. Channel value blobs that were referenced *only* by the now-deleted checkpoints become orphaned and are not removed. For long-running threads with frequent checkpointing, those orphans can dominate the storage footprint over time. Run `collect_orphaned_values()` (below) on a schedule as the second step.

#### Garbage-collecting orphaned channel values

After pruning checkpoints, `collect_orphaned_values()` walks the surviving checkpoints, builds the set of `(channel, version)` pairs they reference, and removes any `values/` blob outside that set. Default is **dry-run** so you can audit first:

```python
audit = saver.collect_orphaned_values(thread_id="conversation-1")
print(audit.would_delete)

result = saver.collect_orphaned_values(thread_id="conversation-1", dry_run=False)
print(f"Deleted {len(result.deleted)} orphaned value blobs")
```

The helper is concurrency-safe by two complementary mechanisms: a recent-write grace period (default `grace_period_seconds=300`) defers deletion of any value blob whose `last_modified` is within the window — protecting the gap between a value blob upload and the corresponding `latest.json` finalization — and a per-orphan re-scan immediately before each delete preserves any older value blob that a newly finalized checkpoint started referencing after the snapshot.

The helper **fails closed** per namespace: if `latest.json` is missing or any surviving checkpoint blob is unreadable / fails deserialization, the namespace is skipped (recorded in `result.skipped_namespaces`) so a misconfigured or transiently-unavailable store cannot trigger destructive deletion.

If you want absolute cutoffs instead of "keep N", use `delete_checkpoints_before(thread_id, before_checkpoint_id=...)`. Checkpoint ids are lexicographically sortable, so `before_checkpoint_id` can be the id of any boundary checkpoint your application picks (e.g. the last successful production checkpoint of a previous day).

#### Operational guidance for orphan GC

The per-orphan re-scan ensures correctness but has **O(orphans × surviving_checkpoints)** blob-list cost. For namespaces with thousands of orphans, this translates to significant Azure Storage transactions.

**Recommended cadence:**

| Thread profile | Retention + GC frequency | Notes |
|---|---|---|
| Short-lived (< 100 checkpoints) | Weekly or on-demand | Low orphan count; cost negligible |
| Long-lived (1,000+ checkpoints) | Daily, off-peak hours | Prune first (`delete_old_checkpoints`), then GC |
| High-throughput (many concurrent threads) | Nightly Timer trigger | Batch across threads; respect `grace_period_seconds` |

**Best practices:**

1. **Always dry-run first**: `collect_orphaned_values(thread_id, dry_run=True)` — inspect `result.candidates` count before committing.
2. **Prune checkpoints before GC**: Fewer surviving checkpoints = cheaper re-scan per orphan.
3. **Keep `grace_period_seconds` ≥ 300**: Protects against race with in-flight checkpoint writes.
4. **Monitor `result.skipped_namespaces`**: Non-empty means something is wrong with checkpoint blobs — investigate before retrying.
5. **Budget estimate**: Each orphan candidate triggers 1 list operation + 1 delete (if confirmed). At ~$0.004/10,000 transactions, a namespace with 10,000 orphans costs ~$0.008 per GC run.

### Connection string security

Do not hardcode storage secrets in source code or deployment artifacts.

Use one of these patterns:

- App Settings with Key Vault references
- Managed identity with Azure SDK identity-based auth
- Secret rotation policy with zero-downtime rollout

⚠️ Treat `AZURE_STORAGE_CONNECTION_STRING` as a high-value credential.

### Postgres checkpointer

The `create_postgres_checkpointer` helper opens a **single `psycopg` connection** for the lifetime of the worker process. This is intentional for Azure Functions' single-threaded, event-driven model, but production deployments must account for connection reliability and pooling at the infrastructure layer.

#### PgBouncer (recommended)

Azure Database for PostgreSQL Flexible Server exposes a built-in [PgBouncer](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-pgbouncer) on port 6432. Enable it in the Azure Portal under **Server parameters → pgbouncer.enabled**.

When PgBouncer is in **transaction pooling** mode, disable prepared statements:

```python
from azure_functions_langgraph.checkpointers.postgres import create_postgres_checkpointer

checkpointer = create_postgres_checkpointer(
    conn_string=os.environ["POSTGRES_CONN_STRING"],
    prepare_threshold=None,  # required for transaction-pooling PgBouncer
)
```

| PgBouncer Mode | `prepare_threshold` | Notes |
|---|---|---|
| Transaction pooling | `None` | Prepared statements not preserved across server reassignment |
| Session pooling | `0` (default) | Safe — connection affinity maintained |
| Disabled (direct) | `0` (default) | No proxy overhead |

#### Connection resilience

psycopg does **not** auto-reconnect on transient failures (network blip, Azure maintenance, failover). The worker process will crash on the next checkpoint write, and Azure Functions will restart it — which triggers a cold start and a fresh connection.

For most Consumption/Premium plan deployments this is acceptable because:

1. Cold starts are infrequent (minutes apart).
2. `create_postgres_checkpointer(setup=True)` re-runs migrations idempotently on restart.
3. Azure Functions' built-in retry and scale-out mask brief unavailability.

If you need **in-process reconnect** without a full cold start (e.g. Dedicated plan with long-lived workers), wrap with a retry at the application layer:

```python
import os
import functools
from azure_functions_langgraph.checkpointers.postgres import create_postgres_checkpointer

_checkpointer = None


def get_checkpointer():
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = create_postgres_checkpointer(
            conn_string=os.environ["POSTGRES_CONN_STRING"],
        )
    return _checkpointer
```

If the connection is lost, the next invoke will fail, the Functions runtime logs the error, and a subsequent cold start re-establishes the connection. For tighter control, consider a health-check Timer trigger that validates the connection and forces a restart via `sys.exit(1)` when stale.

#### Connection pool (when to consider)

The built-in helper uses a single connection. This is sufficient when:

- Each function invocation is short-lived (< 30 s).
- The plan is Consumption or Premium (short worker lifetime, auto-scale handles throughput).
- PgBouncer handles multiplexing at the infrastructure layer.

A true application-side connection pool (`psycopg_pool.ConnectionPool`) is rarely needed because Azure Functions' concurrency model routes one invocation at a time per worker. If you are on a Dedicated (App Service) plan with `FUNCTIONS_WORKER_PROCESS_COUNT > 1`, consider a pool — but first verify PgBouncer alone isn't sufficient.

#### Azure-specific timeouts

Set these App Settings to avoid silent connection drops:

| Setting | Recommended | Why |
|---|---|---|
| `PGCONNECT_TIMEOUT` | `10` | Fail fast on unreachable DB during cold start |
| `PGOPTIONS` | `-c statement_timeout=30000` | Kill runaway queries (30 s) |
| `WEBSITE_TCP_KEEPALIVE` | `1` | Enable TCP keepalive on Azure networking layer |

For Azure Database for PostgreSQL Flexible Server, also ensure:

- **SSL mode**: `require` (default; never disable in production).
- **Firewall**: Allow the Function App's outbound IPs or use VNet integration + private endpoint.
- **Connection limit**: Flexible Server defaults to `max_connections = 50-100` depending on SKU. With PgBouncer, a single Functions worker needs only 1 backend connection.


## Sources

- [Azure Functions Python developer reference](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)
- [Azure Functions host.json reference](https://learn.microsoft.com/en-us/azure/azure-functions/functions-host-json)
- [Azure Functions authentication and authorization](https://learn.microsoft.com/en-us/azure/azure-functions/security-concepts)
- [Azure Blob Storage documentation](https://learn.microsoft.com/en-us/azure/storage/blobs/)
- [Azure Table Storage documentation](https://learn.microsoft.com/en-us/azure/storage/tables/)
- [Azure Functions scale and hosting](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale)
- [Azure Database for PostgreSQL Flexible Server PgBouncer](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-pgbouncer)
- [psycopg 3 documentation](https://www.psycopg.org/psycopg3/docs/)

## See Also

- [DESIGN.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/DESIGN.md) — Key design decisions and constraints
- COMPATIBILITY.md — SDK version compatibility policy
- [azure-functions-logging-python](https://github.com/yeongseon/azure-functions-logging-python) — Structured logging
- [azure-functions-doctor-python](https://github.com/yeongseon/azure-functions-doctor-python) — Pre-deploy diagnostics
- [azure-functions-openapi-python](https://github.com/yeongseon/azure-functions-openapi-python) — API documentation
- [azure-functions-validation-python](https://github.com/yeongseon/azure-functions-validation-python) — Request/response validation
