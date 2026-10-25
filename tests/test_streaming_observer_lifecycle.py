"""True-stream lifecycle regressions for terminal-frame close and cancellation."""

from collections.abc import AsyncIterator, Iterator

import anyio
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.observability import RunContext, RunRejectedReason
from azure_functions_langgraph.observability_otel import OTelRunObserver
from azure_functions_langgraph.streaming import StreamingLangGraphApp


class CountingObserver(OTelRunObserver):
    """Count callbacks as well as exported spans (duplicate terminals are bugs)."""

    def __init__(self, provider: TracerProvider) -> None:
        super().__init__(tracer=provider.get_tracer(__name__))
        self.events: list[str] = []
        self.errors: list[BaseException] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append("started")
        super().on_run_started(ctx)

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append("completed")
        super().on_run_completed(ctx)

    def on_run_failed(self, ctx: RunContext, error: BaseException) -> None:
        self.events.append("failed")
        self.errors.append(error)
        super().on_run_failed(ctx, error)

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None:
        self.events.append("rejected")
        super().on_run_rejected(ctx, reason)


@pytest.fixture
def telemetry() -> Iterator[tuple[CountingObserver, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield CountingObserver(provider), exporter
    finally:
        provider.shutdown()


class Graph:
    checkpointer = True

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome

    def invoke(self, input: dict[str, int]) -> dict[str, int]:
        return input

    def stream(self, input: dict[str, int], **kwargs: str) -> Iterator[dict[str, int]]:
        try:
            yield {"frame": 1}
            if self.outcome == "graph_error":
                raise ValueError("private graph failure")
            yield {"frame": 2}
        finally:
            if self.outcome == "cleanup_error":
                raise RuntimeError("private cleanup failure")

    async def astream(self, input: dict[str, int], **kwargs: str) -> AsyncIterator[dict[str, int]]:
        try:
            yield {"frame": 1}
            if self.outcome == "graph_error":
                raise ValueError("private graph failure")
            yield {"frame": 2}
        finally:
            if self.outcome == "cleanup_error":
                raise RuntimeError("private cleanup failure")

    async def ainvoke(self, input: dict[str, int], **kwargs: str) -> dict[str, int]:
        return input


def request() -> Request:
    async def receive() -> dict[str, bytes | str]:
        return {
            "type": "http.request",
            "body": b'{"input": {}, "config": {"configurable": {"thread_id": "t"}}}',
        }

    return Request({"type": "http", "headers": []}, receive)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("outcome", ["success", "graph_error", "cleanup_error", "cap"])
@pytest.mark.parametrize("close_at", ["end", "error", "data", "exhaustion"])
async def test_stream_terminal_callback_when_consumer_closes(
    telemetry: tuple[CountingObserver, InMemorySpanExporter],
    async_mode: bool,
    outcome: str,
    close_at: str,
) -> None:
    # Given a true-stream handler with a counting observer and a real OTel exporter.
    observer, exporter = telemetry
    lock = InProcessThreadLock()
    app = StreamingLangGraphApp(
        observer=observer, thread_lock=lock, max_stream_events=1 if outcome == "cap" else 10
    )
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
    frames: list[str] = []

    # When the consumer closes at a suspension point or consumes the entire body.
    async for frame in response.body_iterator:
        frames.append(frame)
        if frame.startswith(f"event: {close_at}"):
            break
    await response.body_iterator.aclose()

    # Then each start has exactly one terminal callback and one finished span.
    expected = "completed" if outcome == "success" and close_at != "data" else "failed"
    assert observer.events == ["started", expected]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == (
        StatusCode.OK if expected == "completed" else StatusCode.ERROR
    )
    if close_at in ("end", "exhaustion"):
        assert frames[-1] == "event: end\ndata: {}\n\n"
    token = lock.acquire("agent", "t")
    assert token is not None
    lock.release("agent", "t", token)


async def test_invoke_terminal_callback_when_async_execution_is_cancelled(
    telemetry: tuple[CountingObserver, InMemorySpanExporter],
) -> None:
    # Given an invoke blocked inside async graph execution.
    observer, exporter = telemetry
    entered = anyio.Event()

    class BlockingGraph(Graph):
        async def ainvoke(self, input: dict[str, int], **kwargs: str) -> dict[str, int]:
            entered.set()
            await anyio.sleep_forever()
            raise AssertionError("unreachable after cancellation")

    lock = InProcessThreadLock()
    app = StreamingLangGraphApp(observer=observer, thread_lock=lock)
    app.register(BlockingGraph("success"), "agent", async_mode=True)
    propagated = anyio.Event()

    async def invoke() -> None:
        try:
            await app._handle_invoke(
                request(), app._registrations["agent"], True, JSONResponse, observer, lock
            )
        except anyio.get_cancelled_exc_class():
            propagated.set()
            raise

    # When the host cancels the running invocation.
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(invoke)
        await entered.wait()
        tasks.cancel_scope.cancel()

    # Then cancellation propagates and the span is retired exactly once.
    assert propagated.is_set()
    assert observer.events == ["started", "failed"]
    assert len(exporter.get_finished_spans()) == 1
    assert exporter.get_finished_spans()[0].status.status_code == StatusCode.ERROR


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("fails", [False, True])
async def test_invoke_terminal_callback_when_execution_finishes(
    telemetry: tuple[CountingObserver, InMemorySpanExporter], async_mode: bool, fails: bool
) -> None:
    observer, exporter = telemetry

    class InvokeGraph(Graph):
        def invoke(self, input: dict[str, int], **kwargs: str) -> dict[str, int]:
            if fails:
                raise ValueError("private graph failure")
            return input

        async def ainvoke(self, input: dict[str, int], **kwargs: str) -> dict[str, int]:
            return self.invoke(input)

    lock = InProcessThreadLock()
    app = StreamingLangGraphApp(observer=observer, thread_lock=lock)
    app.register(InvokeGraph("success"), "agent", async_mode=async_mode)
    response = await app._handle_invoke(
        request(), app._registrations["agent"], async_mode, JSONResponse, observer, lock
    )
    assert response.status_code == (500 if fails else 200)
    assert observer.events == ["started", "failed" if fails else "completed"]
    assert len(exporter.get_finished_spans()) == 1


async def test_stream_terminal_callback_when_response_construction_fails(
    telemetry: tuple[CountingObserver, InMemorySpanExporter],
) -> None:
    observer, exporter = telemetry
    lock = InProcessThreadLock()
    app = StreamingLangGraphApp(observer=observer, thread_lock=lock)
    app.register(Graph("success"), "agent")
    failure = RuntimeError("response construction failed")

    def broken_response(*args: str, **kwargs: str) -> None:
        raise failure

    with pytest.raises(RuntimeError) as raised:
        await app._handle_stream(
            request(),
            app._registrations["agent"],
            False,
            JSONResponse,
            broken_response,
            observer,
            lock,
        )
    assert raised.value is failure
    assert observer.events == ["started", "failed"]
    assert observer.errors == [failure]
    assert len(exporter.get_finished_spans()) == 1
