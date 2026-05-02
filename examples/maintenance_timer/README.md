# Maintenance Timer — Stale Lock Recovery

This example runs a **Timer Trigger** that periodically calls
`AzureTableThreadStore.reset_stale_locks()` to reclaim threads stuck in
`busy` status after a host crash or scale-in event.

## When to use this example

Use this when you deploy `AzureTableThreadStore` with Platform-compatible
runs and want automatic recovery from orphaned run locks.

## Files

- `function_app.py` — Timer Trigger wired to `reset_stale_locks()`
- `host.json`, `local.settings.json.example`, `requirements.txt`

## App Settings

| Setting | Description | Default |
|---|---|---|
| `AZURE_TABLE_CONNECTION_STRING` | Table Storage connection string (local dev) | — |
| `AZURE_TABLE_ENDPOINT` | Table Storage endpoint (Managed Identity) | — |
| `LANGGRAPH_TABLE_NAME` | Table name | `langgraphthreads` |
| `STALE_LOCK_THRESHOLD_SECONDS` | Minimum age (seconds) before a busy thread is considered stale | `600` |
| `STALE_LOCK_RESET_STATUS` | Status to assign (`idle` or `error`) | `error` |

Provide **either** `AZURE_TABLE_CONNECTION_STRING` (connection string path)
or `AZURE_TABLE_ENDPOINT` (Managed Identity path). The timer function tries
the connection string first, then falls back to `DefaultAzureCredential`.

## Local development

```bash
cp local.settings.json.example local.settings.json

pip install -r requirements.txt
func start
```

The timer fires every 5 minutes by default (`0 */5 * * * *`). Adjust the
CRON expression in `function_app.py` to suit your workload.

## Production

Deploy alongside your main LangGraph Function App (or as a separate app
sharing the same Table Storage account). Grant the Function App's Managed
Identity the `Storage Table Data Contributor` role on the storage account.

## How it works

1. Timer fires on schedule.
2. `reset_stale_locks(older_than_seconds=600)` queries all `busy` threads.
3. Threads whose `updated_at` is older than the threshold are reset via
   ETag CAS — threads legitimately re-acquired since the scan are skipped.
4. Count of reset threads is logged as a warning for monitoring/alerting.

**Important:** Set `STALE_LOCK_THRESHOLD_SECONDS` comfortably above your
longest expected graph execution time. ETag CAS protects against re-acquire
races, but a still-running long job will not update its `updated_at` and
could be reclaimed if the threshold is too short.
