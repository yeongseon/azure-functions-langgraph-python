"""Context propagation across synchronous stream iterations (#862).

``_aiter_graph_events`` pulls a synchronous ``graph.stream()`` generator via a
separate ``asyncio.to_thread(next, ...)`` per event and ``close()`` via another.
These tests pin down what that guarantees:

- the consumer's ``ContextVar`` values (as of each pull) are visible inside
  every ``next()`` and inside ``close()`` (trace/callback context propagation);
- every sync step runs off the event-loop thread;
- ordering and finalization hold under a bounded executor with interleaved
  streams;
- values *set by the generator* are scoped to the single step that set them
  (each step runs in a fresh copy of the caller's context); they are neither
  carried to the next step nor leaked to the consumer. ``astream`` differs
  because an async generator runs in the consumer's own context.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import threading
from typing import Any

import pytest

from azure_functions_langgraph.streaming import _aiter_graph_events

_VAR: ContextVar[str] = ContextVar("stream_ctx_862", default="unset")
_GEN_VAR: ContextVar[str] = ContextVar("stream_gen_862", default="unset")


class _SyncGraph:
    def __init__(self, steps: int, tag: str = "") -> None:
        self.steps = steps
        self.tag = tag
        self.seen: list[str] = []
        self.threads: list[int] = []
        self.gen_seen: list[str] = []
        self.close_seen: list[str] = []
        self.close_threads: list[int] = []

    def stream(self, *_a: Any, **_k: Any) -> Iterator[dict[str, Any]]:
        try:
            for i in range(self.steps):
                self.seen.append(_VAR.get())
                self.threads.append(threading.get_ident())
                self.gen_seen.append(_GEN_VAR.get())
                _GEN_VAR.set(f"gen-{self.tag}{i}")
                yield {"i": i}
        finally:
            self.close_seen.append(_VAR.get())
            self.close_threads.append(threading.get_ident())


async def _drive(graph: Any, *, consumer_values: list[str] | None = None) -> list[Any]:
    out: list[Any] = []
    events = _aiter_graph_events(graph, {}, {}, "updates", {}, async_mode=False)
    async for event in events:
        out.append(event)
        if consumer_values is not None:
            _VAR.set(consumer_values[len(out) - 1])
    return out


@pytest.mark.asyncio
async def test_consumer_context_visible_in_every_next_and_close() -> None:
    graph = _SyncGraph(3)
    _VAR.set("step-0")
    events = await _drive(graph, consumer_values=["step-1", "step-2", "after"])
    assert [e["i"] for e in events] == [0, 1, 2]
    # Each next() observes the value the consumer held when that pull started.
    assert graph.seen == ["step-0", "step-1", "step-2"]
    # close() (finalization) observes the consumer's latest value.
    assert graph.close_seen == ["after"]


@pytest.mark.asyncio
async def test_sync_steps_and_close_run_off_event_loop_thread() -> None:
    graph = _SyncGraph(3)
    loop_thread = threading.get_ident()
    await _drive(graph)
    assert graph.threads and loop_thread not in graph.threads
    assert loop_thread not in graph.close_threads


@pytest.mark.asyncio
async def test_generator_side_context_is_step_scoped_not_carried_or_leaked() -> None:
    graph = _SyncGraph(3)
    _GEN_VAR.set("consumer")
    await _drive(graph)
    # Generator mutations are not visible to later steps (fresh copy each step)...
    assert graph.gen_seen == ["consumer", "consumer", "consumer"]
    # ...and do not leak back into the consumer's context.
    assert _GEN_VAR.get() == "consumer"


@pytest.mark.asyncio
async def test_async_stream_generator_context_is_continuous_for_contrast() -> None:
    seen: list[str] = []

    class _AsyncGraph:
        async def astream(self, *_a: Any, **_k: Any) -> AsyncIterator[dict[str, int]]:
            for i in range(3):
                seen.append(_GEN_VAR.get())
                _GEN_VAR.set(f"gen-{i}")
                yield {"i": i}

    async def run() -> None:
        _GEN_VAR.set("consumer")
        events = _aiter_graph_events(_AsyncGraph(), {}, {}, "updates", {}, async_mode=True)
        async for _ in events:
            pass

    await asyncio.create_task(run())
    assert seen == ["consumer", "gen-0", "gen-1"]


@pytest.mark.asyncio
async def test_bounded_executor_interleaved_streams_keep_context_and_order() -> None:
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(executor)
    try:
        graphs = [_SyncGraph(6, tag=f"s{n}-") for n in range(5)]

        async def run(n: int, graph: _SyncGraph) -> list[Any]:
            values = [f"s{n}-v{k}" for k in range(7)]
            _VAR.set(values[0])
            return await _drive(graph, consumer_values=values[1:])

        results = await asyncio.gather(*(run(n, g) for n, g in enumerate(graphs)))
    finally:
        executor.shutdown(wait=True)
    for n, (graph, events) in enumerate(zip(graphs, results, strict=True)):
        assert [e["i"] for e in events] == list(range(6))
        assert graph.seen == [f"s{n}-v{k}" for k in range(6)]
        assert graph.close_seen == [f"s{n}-v6"]
        assert graph.gen_seen == ["unset"] * 6
        # Bounded pool: threads are shared/reused, never more than the pool size overall.
    all_threads = {t for g in graphs for t in g.threads + g.close_threads}
    assert len(all_threads) <= 2


@pytest.mark.asyncio
async def test_early_close_runs_finalizer_with_consumer_context() -> None:
    graph = _SyncGraph(5)
    _VAR.set("early")
    events = _aiter_graph_events(graph, {}, {}, "updates", {}, async_mode=False)
    async for _ in events:
        break
    _VAR.set("after-break")
    await events.aclose()
    assert graph.close_seen == ["after-break"]
    assert threading.get_ident() not in graph.close_threads


@pytest.mark.asyncio
async def test_generator_exception_propagates() -> None:
    class _Boom:
        def stream(self, *_a: Any, **_k: Any) -> Iterator[dict[str, int]]:
            yield {"i": 0}
            raise ValueError("boom")

    events = _aiter_graph_events(_Boom(), {}, {}, "updates", {}, async_mode=False)
    got = []
    with pytest.raises(ValueError, match="boom"):
        async for e in events:
            got.append(e)
    assert got == [{"i": 0}]
