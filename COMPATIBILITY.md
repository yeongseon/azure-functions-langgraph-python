# Compatibility Policy

## LangGraph Version Support

| Package | Supported Versions | Notes |
|---|---|---|
| `langgraph` | `>=1.0,<2.0` | Runtime dependency. CI covers the minimum supported 1.x release (1.0.0) plus the latest resolved 1.x release on every Python version (3.10–3.14). |
| `langgraph-sdk` | `>=0.3,<0.4` | Platform compat layer mirrors this SDK version's REST API shapes. |
| `pydantic` | `>=2.0` | Required for request/response models. |
| `azure-functions` | `>=1.17` | Azure Functions Python v2 programming model. |

## Platform Compatibility Layer

The `platform/` subpackage mirrors the LangGraph Platform REST API as understood by `langgraph-sdk >=0.3,<0.4`. This means:

1. **Response shapes** — JSON response structures match what `langgraph-sdk` expects. Required fields, types, and nesting are tested via contract tests in `tests/test_sdk_contracts.py`.

2. **Route paths** — URL patterns match the SDK's expected endpoints (e.g., `/assistants/search`, `/threads/{thread_id}/runs/wait`).

3. **Error format** — Error responses use `{"detail": "..."}` format matching the SDK's error parsing.

4. **SSE event format** — Streaming responses use the same SSE event structure (metadata -> data -> end events).

## SDK Feature Support Matrix

This matrix is derived directly from `platform/_common.py` (`_UNSUPPORTED_FIELDS`,
`_UNSUPPORTED_THREAD_FILTER_FIELDS`, `_preflight_run_create`) and the per-route
handlers under `platform/_assistants.py`, `platform/_threads.py`, and
`platform/_runs.py`. Anything marked **501** is rejected explicitly so callers
get a clear failure rather than silent drift.

### Assistants

| SDK call | Support | Notes |
|---|---|---|
| `assistants.search` | ✅ Full | Filters by `graph_id` (exact) and `name` (case-insensitive substring) over registered graphs. |
| `assistants.get` | ✅ Full | Returns `_registration_to_assistant` snapshot for the registered graph. |
| `assistants.count` | ✅ Full | Same filter semantics as `search`. |
| `assistants.create` / `update` / `delete` / `set_latest` | ❌ Not exposed | Assistants are derived from in-process `register()` calls; no runtime mutation. |
| `metadata` filter on `search`/`count` | ⚠️ Silently ignored | Matches return as if the filter were absent (`_assistants.py:51-52, 88-89`). Plan to surface as a filter no-op or `400` in a future release. |

### Threads (CRUD)

| SDK call | Support | Notes |
|---|---|---|
| `threads.create` | ✅ Full | `metadata` accepted; `ttl` silently dropped (`extra="ignore"`). |
| `threads.get` | ✅ Full | |
| `threads.update` | ✅ Full | Only `metadata` updates — matches SDK `PATCH` shape. |
| `threads.delete` | ✅ Full | Also clears persisted state when a checkpointer is wired. |
| `threads.search` | ⚠️ Partial | See "Search/count limits" below. |
| `threads.count` | ⚠️ Partial | See "Search/count limits" below. |

#### Search/count limits

- **Unsupported request fields** → `501`: `values`, `ids`, `sort_by`, `sort_order`, `select`, `extract` (`_common.py:_UNSUPPORTED_THREAD_FILTER_FIELDS`).
- **Metadata filtering** is **client-side** for both `InMemoryThreadStore` and `AzureTableThreadStore` — i.e. all candidate rows are fetched and filtered in-process (`_metadata_matches`). Plan accordingly for high-cardinality metadata or large thread tables.
- **Sort order** is fixed at `created_at desc`; no SDK-side override.

### Runs

| SDK call | Support | Notes |
|---|---|---|
| `runs.wait` (threaded) | ✅ Full | Buffered result; thread bound to assistant on first run. |
| `runs.wait` (threadless via `runs/wait`) | ⚠️ Partial | Requires the graph to satisfy `CloneableGraph` so the checkpointer can be disabled per call; otherwise `501` (`_runs.py:388-394`). `thread_id` in `config.configurable` is rejected with `422`. |
| `runs.stream` (threaded + threadless) | ⚠️ Partial | **Buffered SSE**, not token-level streaming (see README "Streaming behavior"). Multi-`stream_mode` lists with more than one element → `501` (`_runs.py:188-192, 463-467`). Streaming requires the graph to implement `StreamableGraph` → `501` otherwise (`_runs.py:215-219, 498-502`). |
| `runs.create` (fire-and-forget) | ❌ Not exposed | Only `wait` and `stream` variants are available. |
| `runs.get` / `list` / `cancel` / `delete` / `join` / `join_stream` | ❌ Not exposed | No async run registry yet. |

#### `RunCreate` field support

Derived from `_preflight_run_create()` in `platform/_common.py`.

| Field | Behavior |
|---|---|
| `assistant_id` | ✅ Required |
| `input` | ✅ Supported |
| `metadata` | ✅ Stored on the thread |
| `config` (incl. `configurable`) | ✅ Forwarded to the graph; `thread_id` is overridden to the route's `{thread_id}` for safety |
| `context` | ✅ Forwarded |
| `stream_mode` (single string, or 1-element list) | ✅ Supported |
| `multitask_strategy` | ⚠️ Only `reject` (default). Any other value → `501` |
| `interrupt_before` | ❌ `501` |
| `interrupt_after` | ❌ `501` |
| `webhook` | ❌ `501` |
| `checkpoint_id` (in `RunCreate`) | ❌ `501` (resumption from a specific checkpoint not yet implemented) |
| `command` | ❌ `501` (resume-with-command not yet implemented) |
| `on_completion` | ❌ `501` |
| `after_seconds` | ❌ `501` |
| `if_not_exists` | ❌ `501` |
| `feedback_keys` | ❌ `501` |
| Other unknown fields | Silently dropped (`extra="ignore"`) |

> **Why explicit 501 rather than silent drop?** Concurrent runs on the same
> thread are rejected with `409` (`multitask_strategy=reject`). Other
> unsupported features must surface immediately so SDK callers do not silently
> lose semantics — see `_preflight_run_create()`.

### State

| SDK call | Support | Notes |
|---|---|---|
| `threads.get_state` | ✅ Full | Maps the LangGraph `StateSnapshot` to the SDK `ThreadState` shape via `_snapshot_to_thread_state`. |
| `threads.update_state` | ✅ Full | Requires the graph to satisfy `UpdatableStateGraph`. |
| `threads.get_history` | ✅ Full | Requires `StateHistoryGraph`. Filtering by `metadata`, `before`, `checkpoint`, and `limit` is supported. |

### Webhooks, cron, store

| Area | Support |
|---|---|
| Webhooks (`webhook` field, `runs.create_webhook`, etc.) | ❌ Not implemented |
| Cron / scheduled runs | ❌ Not implemented |
| Long-term `Store` API (`/store/items`) | ❌ Not implemented — use a checkpointer for thread-scoped state |

## When you should use LangGraph Platform instead

This package targets the common case of "deploy LangGraph graphs on Azure
Functions and talk to them with `langgraph-sdk`." If your application relies
on any of the following, **prefer LangGraph Platform** (or another long-running
host) over this package:

- True token-level streaming (vs. buffered SSE)
- `interrupt_before` / `interrupt_after` flows
- Webhook callbacks on run completion
- Resumption from a specific `checkpoint_id` or via `command`
- `multitask_strategy` other than `reject` (interrupt, rollback, enqueue)
- Asynchronous run lifecycle (`runs.create` + `runs.get`/`cancel`/`join`)
- Cron / scheduled runs
- The long-term `Store` API

## Breaking Changes Policy

- Compatibility breaks with the mirrored SDK version are treated as **planned release work**, not incidental fixes.
- Before breaking a contract, the change must be documented in the CHANGELOG with migration guidance.
- Contract tests in `tests/test_sdk_contracts.py` serve as the automated verification gate.

## Version Testing

CI tests include:
- Unit tests with mock graphs (`tests/test_platform_routes.py`)
- SDK integration tests with real `langgraph-sdk` client via `httpx.MockTransport` (`tests/test_sdk_compat.py`)
- Contract shape tests (`tests/test_sdk_contracts.py`)

## Optional checkpoint backends

| Extra | Dependency | Python | Notes |
|---|---|---|---|
| `postgres` | `langgraph-checkpoint-postgres>=3.0,<4` | 3.10+ | Production DB checkpoint backend |
| `sqlite` | `langgraph-checkpoint-sqlite>=3.0,<4` | 3.10+ | Local development |
| `cosmos` | `langgraph-checkpoint-cosmosdb>=0.2.0,<0.3` | 3.10+ | Experimental Azure-native checkpoint backend |
