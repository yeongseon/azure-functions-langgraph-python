"""Cancellation-safe adaptation of synchronous thread locks for async callers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging

from azure_functions_langgraph.locks.base import ThreadLock

logger = logging.getLogger(__name__)


async def release_thread_lock(
    thread_lock: ThreadLock, graph_name: str, thread_id: str, token: str
) -> None:
    """Release one owner token even when the caller is repeatedly cancelled."""
    caller = asyncio.current_task()
    cancellation_count = caller.cancelling() if caller is not None else 0
    release_task = asyncio.create_task(
        asyncio.to_thread(thread_lock.release, graph_name, thread_id, token)
    )
    cancellation: asyncio.CancelledError | None = None
    while not release_task.done():
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    await release_task
    if cancellation is None and caller is not None and caller.cancelling() > cancellation_count:
        cancellation = asyncio.CancelledError()
    if cancellation is not None:
        raise cancellation


@asynccontextmanager
async def async_thread_lock(
    thread_lock: ThreadLock, graph_name: str, thread_id: str | None
) -> AsyncIterator[str | None]:
    """Acquire and release an owner token without losing it to cancellation."""
    if thread_id is None:
        yield None
        return

    caller = asyncio.current_task()
    cancellation_count = caller.cancelling() if caller is not None else 0
    acquire_task = asyncio.create_task(
        asyncio.to_thread(thread_lock.acquire, graph_name, thread_id)
    )
    cancellation: asyncio.CancelledError | None = None
    while not acquire_task.done():
        try:
            await asyncio.shield(acquire_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            break

    if cancellation is None and caller is not None and caller.cancelling() > cancellation_count:
        cancellation = asyncio.CancelledError()

    try:
        token = await acquire_task
    except Exception:
        if cancellation is not None:
            logger.debug(
                "Thread lock acquisition failed after caller cancellation",
                extra={"graph_name": graph_name, "thread_id": thread_id},
                exc_info=True,
            )
            raise cancellation from None
        raise

    if cancellation is not None:
        if token is not None:
            logger.debug(
                "Thread lock acquired after caller cancellation; releasing owner",
                extra={"graph_name": graph_name, "thread_id": thread_id},
            )
            try:
                await release_thread_lock(thread_lock, graph_name, thread_id, token)
            except asyncio.CancelledError:
                pass
        raise cancellation

    try:
        yield token
    finally:
        if token is not None:
            await release_thread_lock(thread_lock, graph_name, thread_id, token)
