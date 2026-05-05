"""Timer Trigger that resets stale run locks on AzureTableThreadStore.

Threads stuck in ``busy`` status (e.g. due to host crashes during graph
execution) are reclaimed so new runs can proceed.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

import azure.functions as func

app = func.FunctionApp()

logger = logging.getLogger(__name__)


def _build_thread_store():  # type: ignore[no-untyped-def]
    """Build an AzureTableThreadStore from environment variables."""
    from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore

    conn_str = os.environ.get("AZURE_TABLE_CONNECTION_STRING")
    table_name = os.environ.get("LANGGRAPH_TABLE_NAME", "langgraphthreads")

    if conn_str:
        return AzureTableThreadStore.from_connection_string(
            connection_string=conn_str,
            table_name=table_name,
        )

    # Managed Identity path
    from azure.data.tables import TableClient
    from azure.identity import DefaultAzureCredential

    endpoint = os.environ["AZURE_TABLE_ENDPOINT"]
    credential = DefaultAzureCredential()
    table_client = TableClient(
        endpoint=endpoint,
        table_name=table_name,
        credential=credential,
    )
    return AzureTableThreadStore.from_table_client(table_client)


# Lazy singleton — built on first timer invocation
_thread_store = None


@app.timer_trigger(
    schedule="0 */5 * * * *",  # every 5 minutes
    arg_name="timer",
    run_on_startup=False,
)
def reset_stale_locks(timer: func.TimerRequest) -> None:
    """Reset busy threads whose lock is older than the configured threshold."""
    global _thread_store  # noqa: PLW0603
    if _thread_store is None:
        _thread_store = _build_thread_store()

    try:
        older_than = int(os.environ.get("STALE_LOCK_THRESHOLD_SECONDS", "600"))
    except (ValueError, TypeError):
        logger.error("Invalid STALE_LOCK_THRESHOLD_SECONDS, falling back to 600")
        older_than = 600
    reset_status: Literal["idle", "error"] = "error"
    raw_status = os.environ.get("STALE_LOCK_RESET_STATUS", "error")
    if raw_status in ("idle", "error"):
        reset_status = raw_status  # type: ignore[assignment]
    else:
        logger.error("Invalid STALE_LOCK_RESET_STATUS=%r, falling back to 'error'", raw_status)

    count = _thread_store.reset_stale_locks(
        older_than_seconds=older_than,
        status=reset_status,
    )

    if count:
        logger.warning("Reset %d stale thread lock(s)", count)
    else:
        logger.info("No stale locks found")

    if timer.past_due:
        logger.info("Timer is past due — execution was delayed")
