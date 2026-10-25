"""Observer cleanup retains cancellation and primary graph failures."""

from collections.abc import AsyncIterator

import anyio
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import pytest
from starlette.responses import JSONResponse, StreamingResponse

from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.streaming import StreamingLangGraphApp
from tests.test_streaming_observer_lifecycle import (
    CountingObserver,
    Graph,
    request,
)

pytest_plugins = ["tests.test_streaming_observer_lifecycle"]


class FailingReleaseLock(InProcessThreadLock):
    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        super().release(graph_name, thread_id, token)
        raise RuntimeError("release failed")


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("outcome", ["success", "graph_error"])
async def test_observer_fails_once_when_lock_release_raises(
    telemetry: tuple[CountingObserver, InMemorySpanExporter], async_mode: bool, outcome: str
) -> None:
    # Given a stream whose lock release fails after returning ownership.
    observer, exporter = telemetry
    lock = FailingReleaseLock()
    app = StreamingLangGraphApp(observer=observer, thread_lock=lock)
    app.register(Graph(outcome), "agent", async_mode=async_mode)
    response = await app._handle_stream(
        request(),
        app._registrations["agent"],
        async_mode,
        JSONResponse,
        StreamingResponse,
        observer,
        lock,
    )

    # When execution and cleanup finish.
    if outcome == "success":
        with pytest.raises(RuntimeError, match="release failed"):
            async for _ in response.body_iterator:
                pass
    else:
        frames = [frame async for frame in response.body_iterator]
        assert frames[-1] == "event: end\ndata: {}\n\n"

    # Then cleanup cannot strand the span or replace a primary graph error.
    assert observer.events == ["started", "failed"]
    assert isinstance(observer.errors[0], ValueError if outcome == "graph_error" else RuntimeError)
    assert len(exporter.get_finished_spans()) == 1


async def test_cancelled_stream_preserves_cancellation_when_release_fails(
    telemetry: tuple[CountingObserver, InMemorySpanExporter],
) -> None:
    # Given an async stream blocked before its first event.
    observer, exporter = telemetry
    entered = anyio.Event()

    class BlockingGraph(Graph):
        async def astream(
            self, input: dict[str, int], **kwargs: str
        ) -> AsyncIterator[dict[str, int]]:
            entered.set()
            await anyio.sleep_forever()
            yield {}

    lock = FailingReleaseLock()
    app = StreamingLangGraphApp(observer=observer, thread_lock=lock)
    app.register(BlockingGraph("success"), "agent", async_mode=True)
    response = await app._handle_stream(
        request(),
        app._registrations["agent"],
        True,
        JSONResponse,
        StreamingResponse,
        observer,
        lock,
    )
    propagated = anyio.Event()

    async def consume() -> None:
        try:
            await anext(response.body_iterator)
        except anyio.get_cancelled_exc_class():
            propagated.set()
            raise

    # When the host cancels consumption and cleanup also fails.
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(consume)
        await entered.wait()
        tasks.cancel_scope.cancel()

    # Then the original cancellation wins and the span closes exactly once.
    assert propagated.is_set()
    assert observer.events == ["started", "failed"]
    assert isinstance(observer.errors[0], anyio.get_cancelled_exc_class())
    assert len(exporter.get_finished_spans()) == 1
