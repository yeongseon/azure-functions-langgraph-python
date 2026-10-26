import asyncio

import pytest

from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
from azure_functions_langgraph.durable._state import DurableRunPayload
from azure_functions_langgraph.locks import InProcessThreadLock
from tests.test_durable import FakeAsyncGraph, FakeReg, RecordingObserver


class CancelledGraph(FakeAsyncGraph):
    async def ainvoke(self, input: str, config: None = None) -> None:
        raise asyncio.CancelledError


async def test_durable_activity_propagates_host_cancellation() -> None:
    observer = RecordingObserver()
    with pytest.raises(asyncio.CancelledError):
        await execute_langgraph_run_impl(
            DurableRunPayload("run", "agent", "input"),
            registry={"agent": FakeReg(CancelledGraph(), "agent", async_mode=True)},
            thread_lock=InProcessThreadLock(),
            observer=observer,
        )
    assert observer.events == ["started", "failed"]
