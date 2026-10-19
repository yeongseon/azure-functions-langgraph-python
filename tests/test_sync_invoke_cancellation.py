"""Regression tests: retain synchronous-invoke ownership until the worker
finishes, even when the awaiting caller is cancelled (#652).

Both the Durable activity (``durable/_activity.py::_invoke_graph``) and the
streaming-app invoke handler (``streaming.py::StreamingLangGraphApp.
_handle_invoke``) previously awaited ``asyncio.to_thread(graph.invoke, ...)``
directly. Cancelling the awaiting task cannot stop the already-running
synchronous worker (a running ``concurrent.futures.Future`` cannot be
cancelled); the lock-release scope exited while the worker was still active,
so a second caller could acquire the same ``(graph_name, thread_id)`` while
the first graph invocation was still mutating protected state.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
import warnings

from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
from azure_functions_langgraph.durable._state import DurableRunPayload
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.observability import NoOpRunObserver
from azure_functions_langgraph.streaming import StreamingLangGraphApp, _import_fastapi_extension

_ext = _import_fastapi_extension()
JSONResponse = _ext.JSONResponse

_WAIT_SECONDS = 2.0


class _BlockingGraph:
    """Graph whose synchronous invoke() blocks on a controllable barrier,
    so a test can observe whether a lock/owner scope released before or
    after the worker actually finished."""

    checkpointer = object()

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def invoke(self, graph_input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
        self.calls += 1
        self.entered.set()
        try:
            if not self.proceed.wait(_WAIT_SECONDS):
                raise TimeoutError("probe controller did not release worker")
            return {"ok": True}
        finally:
            self.finished.set()


class _Registration:
    def __init__(self, graph: Any, name: str = "agent") -> None:
        self.graph = graph
        self.name = name
        self.async_mode = False


async def _wait(event: threading.Event) -> None:
    deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
    while not event.is_set():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("probe event deadline")
        await asyncio.sleep(0.001)


async def _run_cancellation_case(operation_factory: Any, graph: _BlockingGraph, lock: Any) -> None:
    task = asyncio.create_task(operation_factory())
    await _wait(graph.entered)

    task.cancel()
    # Release the worker concurrently with awaiting cancellation -- the fix
    # makes the caller wait for the worker regardless of cancellation, so
    # the worker must actually be allowed to finish for the task to resolve.
    graph.proceed.set()

    try:
        await asyncio.wait_for(task, _WAIT_SECONDS)
    except asyncio.CancelledError:
        pass

    await _wait(graph.finished)
    assert graph.finished.is_set()

    second = lock.acquire("agent", "thread-one")
    assert second is not None, (
        "a second caller must be able to acquire after the first worker finished"
    )
    lock.release("agent", "thread-one", second)


async def test_durable_activity_retains_ownership_until_sync_invoke_finishes() -> None:
    graph = _BlockingGraph()
    lock = InProcessThreadLock()
    reg = _Registration(graph)

    async def operation() -> Any:
        return await execute_langgraph_run_impl(
            DurableRunPayload(run_id="r1", graph_name="agent", input={}, thread_id="thread-one"),
            registry={"agent": reg},
            thread_lock=lock,
        )

    await _run_cancellation_case(operation, graph, lock)
    assert graph.calls == 1


async def test_streaming_invoke_retains_ownership_until_sync_invoke_finishes() -> None:
    graph = _BlockingGraph()
    lock = InProcessThreadLock()

    class _Request:
        headers: dict[str, str] = {}

        async def body(self) -> bytes:
            return json.dumps(
                {"input": {}, "config": {"configurable": {"thread_id": "thread-one"}}}
            ).encode()

    async def operation() -> Any:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            app = StreamingLangGraphApp(thread_lock=lock)
        app.register(graph=graph, name="agent")
        reg = app._registrations["agent"]
        return await app._handle_invoke(
            _Request(), reg, False, JSONResponse, NoOpRunObserver(), lock
        )

    await _run_cancellation_case(operation, graph, lock)
    assert graph.calls == 1


async def test_durable_activity_successful_invoke_releases_exactly_once() -> None:
    graph = _BlockingGraph()
    graph.proceed.set()
    lock = InProcessThreadLock()
    reg = _Registration(graph)

    result = await execute_langgraph_run_impl(
        DurableRunPayload(run_id="r1", graph_name="agent", input={}, thread_id="thread-one"),
        registry={"agent": reg},
        thread_lock=lock,
    )

    assert result.activity_status == "success"
    second = lock.acquire("agent", "thread-one")
    assert second is not None
    lock.release("agent", "thread-one", second)
