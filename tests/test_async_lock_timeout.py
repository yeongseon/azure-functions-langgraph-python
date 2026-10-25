"""Timeout forwarding through the cancellation-safe lock scope."""

from __future__ import annotations

import pytest

from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.locks._async import async_thread_lock


class RecordingLock(InProcessThreadLock):
    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.timeouts.append(timeout)
        return super().acquire(graph_name, thread_id, timeout)


@pytest.mark.asyncio
async def test_async_scope_forwards_configured_timeout() -> None:
    # Given a synchronous backend that records the acquisition budget.
    lock = RecordingLock()

    # When an async caller requests a bounded wait.
    async with async_thread_lock(lock, "agent", "thread", timeout=0.25) as token:
        assert token is not None

    # Then the backend receives exactly that budget, once.
    assert lock.timeouts == [0.25]
