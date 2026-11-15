"""Cross-surface execution parity: lock scope, lock order and cancellation (#861).

Every surface that writes checkpoints (Native invoke, Platform runs, Service Bus
and the Durable activity) must compete on the persistence-aligned ``ThreadLock``
identity (#858/#867), and Platform adds a thread-store CAS on top. These tests
pin, with no Azure access and no sleeps:

* same-thread cross-graph overlap for every holder/contender surface pair;
* the single Platform acquisition/release order of ``ThreadLock`` versus the
  thread-store CAS, and how the other surfaces interact with it;
* cancellation of an in-flight run on the async surfaces.

Streaming's client-disconnect behavior is pinned in ``tests/test_streaming_app.py``
(it needs the optional streaming extra).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterator
import json
from typing import Any, Literal

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
from azure_functions_langgraph.durable._state import DurableRunPayload
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.observability import RunContext
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from azure_functions_langgraph.triggers.service_bus import (
    ThreadContentionError,
    process_service_bus_message,
    process_service_bus_message_async,
)
from tests.test_service_bus import FakeServiceBusMessage

THREAD_ID = "thread-1"
Surface = Literal["native", "platform", "service_bus", "durable"]
SURFACES: list[Surface] = ["native", "platform", "service_bus", "durable"]


class Saver:
    def __init__(self, identity: str) -> None:
        self.lock_scope_identity = identity


class HookGraph:
    """Checkpointed graph that runs ``hook`` once, from inside its own run."""

    def __init__(self, identity: str) -> None:
        self.checkpointer = Saver(identity)
        self.hook: Any = None
        self.calls = 0

    def invoke(self, input: Any, config: Any = None, **_: Any) -> dict[str, Any]:
        self.calls += 1
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()
        return {"v": 1}

    async def ainvoke(self, input: Any, config: Any = None, **_: Any) -> dict[str, Any]:
        self.calls += 1
        hook, self.hook = self.hook, None
        if hook is not None:
            await hook()
        return {"v": 1}

    def stream(
        self, input: Any, config: Any = None, stream_mode: str = "values", **_: Any
    ) -> Iterator[dict[str, Any]]:
        yield self.invoke(input, config)


class Observer:
    def __init__(self) -> None:
        self.events: list[tuple[str, RunContext]] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append(("started", ctx))

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append(("completed", ctx))

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self.events.append(("failed", ctx))

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:
        self.events.append(("rejected", ctx))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]


class World:
    """One app carrying a ``holder`` and a ``contender`` graph on every surface."""

    def __init__(self, *, shared: bool, lock: Any = None, store: Any = None) -> None:
        self.lock = lock or InProcessThreadLock()
        self.app = LangGraphApp(thread_lock=self.lock, platform_compat=True)
        self.store = store or InMemoryThreadStore(id_factory=lambda: THREAD_ID)
        self.app._thread_store = self.store
        self.store.create()
        self.graphs: dict[str, HookGraph] = {}
        for role, identity in (("holder", "s1"), ("contender", "s1" if shared else "s2")):
            for suffix in ("", "-sb"):
                name = role + suffix
                self.graphs[name] = HookGraph(identity)
                if suffix:
                    self.app.register_service_bus(
                        self.graphs[name],
                        name,
                        connection="SB",
                        queue_name="q",
                        thread_id_factory=lambda _m: THREAD_ID,
                        lock_timeout=0.0,
                    )
                else:
                    self.app.register(graph=self.graphs[name], name=name)
        fa = self.app.function_app
        fa.functions_bindings = {}
        functions = {fn.get_function_name(): fn.get_user_function() for fn in fa.get_functions()}
        self._platform = {
            endpoint: functions[f"aflg_platform_runs_{endpoint}"] for endpoint in ("wait", "stream")
        }

    @property
    def scoped(self) -> Any:
        return self.app._scoped_thread_lock()

    def run(self, surface: Surface, role: str) -> str:
        """Drive ``role`` through ``surface``; return ``"ran"`` or ``"blocked"``."""
        if surface == "platform":
            status = self.platform(role).status_code
            assert status in (200, 409), status
            return "ran" if status == 200 else "blocked"
        if surface == "native":
            request = func.HttpRequest(
                method="POST",
                url=f"https://example.test/api/graphs/{role}/invoke",
                body=json.dumps(
                    {"input": {"v": 1}, "config": {"configurable": {"thread_id": THREAD_ID}}}
                ).encode(),
                headers={},
            )
            status = self.app._handle_invoke(request, self.app._registrations[role]).status_code
        elif surface == "service_bus":
            try:
                process_service_bus_message(
                    self.app._sb_registrations[role + "-sb"],
                    FakeServiceBusMessage(b'{"v": 1}'),
                    thread_lock=self.scoped,
                )
            except ThreadContentionError:
                return "blocked"
            return "ran"
        else:
            result = asyncio.run(
                execute_langgraph_run_impl(
                    DurableRunPayload(
                        run_id="r", graph_name=role, input={"v": 1}, thread_id=THREAD_ID
                    ),
                    registry=self.app._registrations,
                    thread_lock=self.scoped,
                )
            )
            return {"success": "ran", "rejected": "blocked"}[result.activity_status]
        assert status in (200, 409), status
        return "ran" if status == 200 else "blocked"

    def platform(self, role: str, endpoint: str = "wait") -> func.HttpResponse:
        request = func.HttpRequest(
            method="POST",
            url=f"https://example.test/api/threads/{THREAD_ID}/runs/{endpoint}",
            body=json.dumps({"assistant_id": role, "input": {"v": 1}}).encode(),
            headers={"Content-Type": "application/json"},
            route_params={"thread_id": THREAD_ID},
        )
        response: func.HttpResponse = self._platform[endpoint](request)
        return response

    def graph_for(self, surface: Surface, role: str) -> HookGraph:
        return self.graphs[role + "-sb" if surface == "service_bus" else role]


# --- same-thread cross-graph overlap (#867 lock scope) -------------------------


@pytest.mark.parametrize("shared", [True, False], ids=["shared-storage", "separate-storage"])
@pytest.mark.parametrize("contender", SURFACES)
@pytest.mark.parametrize("holder", SURFACES)
def test_same_thread_cross_graph_overlap(holder: Surface, contender: Surface, shared: bool) -> None:
    world = World(shared=shared)
    seen: list[str] = []
    world.graph_for(holder, "holder").hook = lambda: seen.append(world.run(contender, "contender"))

    assert world.run(holder, "holder") == "ran"

    # Graphs on one checkpoint storage collide on the thread whatever surface they come in
    # through. Separate storage stays independent, except Platform against Platform: the
    # thread-store CAS/assistant binding is thread-level and Platform-only.
    platform_pair = holder == "platform" and contender == "platform"
    expect_blocked = shared or platform_pair
    assert seen == ["blocked" if expect_blocked else "ran"]
    assert world.graph_for(contender, "contender").calls == (0 if expect_blocked else 1)
    # Nothing leaks: once the holder finished the contender runs, except that a Platform thread
    # stays bound to the first assistant that ran on it (a Platform-only 409).
    assert world.run(contender, "contender") == ("blocked" if platform_pair else "ran")


# --- Platform thread-store CAS versus ThreadLock: one acquisition/release order -----


class OrderedLock(InProcessThreadLock):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        token = super().acquire(graph_name, thread_id, timeout)
        self.events.append("lock.acquire" if token else "lock.acquire_failed")
        return token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.events.append("lock.release")
        super().release(graph_name, thread_id, token)


class OrderedStore(InMemoryThreadStore):
    def __init__(self, events: list[str]) -> None:
        super().__init__(id_factory=lambda: THREAD_ID)
        self.events = events
        self.cas_error: Exception | None = None
        self.cas_busy = False

    def try_acquire_run_lock(self, thread_id: str, **kwargs: Any) -> Any:
        self.events.append("store.acquire")
        if self.cas_error is not None:
            raise self.cas_error
        if self.cas_busy:
            return None
        return super().try_acquire_run_lock(thread_id, **kwargs)

    def release_run_lock(self, thread_id: str, **kwargs: Any) -> Any:
        self.events.append(f"store.release[{kwargs['status']}]")
        return super().release_run_lock(thread_id, **kwargs)


def _ordered_world(events: list[str]) -> tuple[World, OrderedStore]:
    store = OrderedStore(events)
    world = World(shared=True, lock=OrderedLock(events), store=store)
    graph = world.graphs["holder"]
    original = graph.invoke

    def traced(input: Any, config: Any = None, **kw: Any) -> dict[str, Any]:
        events.append("graph.invoke")
        return original(input, config=config, **kw)

    graph.invoke = traced  # type: ignore[method-assign]
    return world, store


ENDPOINTS = ["wait", "stream"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_platform_acquires_thread_lock_then_cas_and_releases_cas_then_thread_lock(
    endpoint: str,
) -> None:
    events: list[str] = []
    world, _ = _ordered_world(events)
    assert world.platform("holder", endpoint).status_code == 200
    assert events == [
        "lock.acquire",
        "store.acquire",
        "graph.invoke",
        "store.release[idle]",
        "lock.release",
    ]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_platform_graph_failure_keeps_the_same_order_and_marks_the_thread_errored(
    endpoint: str,
) -> None:
    events: list[str] = []
    world, store = _ordered_world(events)

    def boom() -> None:
        raise RuntimeError("graph failed")

    world.graphs["holder"].hook = boom
    world.platform("holder", endpoint)
    assert events == [
        "lock.acquire",
        "store.acquire",
        "graph.invoke",
        "store.release[error]",
        "lock.release",
    ]
    thread = store.get(THREAD_ID)
    assert thread is not None and thread.status == "error"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_thread_lock_contention_never_consults_the_cas(endpoint: str) -> None:
    events: list[str] = []
    world, _ = _ordered_world(events)
    held = world.scoped.acquire("holder", THREAD_ID)
    assert held is not None
    events.clear()
    assert world.platform("contender", endpoint).status_code == 409
    assert events == ["lock.acquire_failed"]
    assert world.graphs["contender"].calls == 0


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    ("cas", "status"),
    [
        (KeyError("gone"), 404),
        (ValueError("thread busy"), 409),
        ("busy", 409),
        (RuntimeError("store down"), 500),
    ],
    ids=["missing-thread", "rejected", "lost-cas", "store-error"],
)
def test_cas_refusal_releases_the_thread_lock_and_never_runs_the_graph(
    endpoint: str, cas: Any, status: int
) -> None:
    events: list[str] = []
    world, store = _ordered_world(events)
    if cas == "busy":
        store.cas_busy = True
    else:
        store.cas_error = cas
    assert world.platform("holder", endpoint).status_code == status
    assert events == ["lock.acquire", "store.acquire", "lock.release"]
    assert world.graphs["holder"].calls == 0
    assert world.scoped.acquire("holder", THREAD_ID) is not None  # nothing leaked


def test_service_bus_holding_the_thread_lock_blocks_platform_before_the_cas() -> None:
    events: list[str] = []
    world, store = _ordered_world(events)
    seen: list[int] = []
    world.graphs["holder-sb"].hook = lambda: seen.append(world.platform("holder").status_code)
    assert world.run("service_bus", "holder") == "ran"
    assert seen == [409]
    assert events == ["lock.acquire", "lock.acquire_failed", "lock.release"]  # no store.acquire
    thread = store.get(THREAD_ID)
    assert thread is not None and thread.status == "idle"


def test_platform_run_in_flight_blocks_service_bus_through_the_thread_lock_only() -> None:
    events: list[str] = []
    world, _ = _ordered_world(events)
    seen: list[str] = []
    world.graphs["holder"].hook = lambda: seen.append(world.run("service_bus", "contender"))
    assert world.platform("holder").status_code == 200
    assert seen == ["blocked"]
    # Service Bus lost on the ThreadLock; it never touched the store.
    assert events == [
        "lock.acquire",
        "store.acquire",
        "graph.invoke",
        "lock.acquire_failed",
        "store.release[idle]",
        "lock.release",
    ]


def test_durable_activity_competes_on_the_thread_lock_not_the_cas() -> None:
    events: list[str] = []
    world, store = _ordered_world(events)
    assert world.run("durable", "holder") == "ran"
    assert events == ["lock.acquire", "graph.invoke", "lock.release"]
    thread = store.get(THREAD_ID)
    assert thread is not None and thread.status == "idle" and thread.lock_token is None


# --- cancellation of an in-flight run on the async surfaces -------------------------


class WaitGraph:
    def __init__(self, identity: str = "s1") -> None:
        self.checkpointer = Saver(identity)
        self.started = asyncio.Event()

    async def ainvoke(self, input: Any, config: Any = None, **_: Any) -> dict[str, Any]:
        self.started.set()
        await asyncio.Event().wait()
        return {}


async def _cancel_in_flight(graph: WaitGraph, coro: Any) -> BaseException:
    task = asyncio.ensure_future(coro)
    await asyncio.wait_for(graph.started.wait(), 2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    return caught.value


@pytest.mark.parametrize("surface", ["native", "service_bus", "durable"])
async def test_cancelled_async_run_is_observed_as_failed_and_releases_the_thread_lock(
    surface: str,
) -> None:
    obs, graph = Observer(), WaitGraph()
    app = LangGraphApp(thread_lock=InProcessThreadLock(), observer=obs)
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    coro: Coroutine[Any, Any, Any]

    if surface == "native":
        app.register(graph=graph, name="worker", async_mode=True)
        request = func.HttpRequest(
            method="POST",
            url="https://example.test/api/graphs/worker/invoke",
            body=json.dumps(
                {"input": {"v": 1}, "config": {"configurable": {"thread_id": THREAD_ID}}}
            ).encode(),
            headers={},
        )
        coro = app._handle_invoke_async(request, app._registrations["worker"])
    elif surface == "service_bus":
        app.register_service_bus(
            graph,
            "worker",
            connection="SB",
            queue_name="q",
            thread_id_factory=lambda _m: THREAD_ID,
            async_mode=True,
        )
        coro = process_service_bus_message_async(
            app._sb_registrations["worker"],
            FakeServiceBusMessage(b'{"v": 1}'),
            thread_lock=scoped,
            observer=obs,
        )
    else:
        app.register(graph=graph, name="worker", async_mode=True)
        coro = execute_langgraph_run_impl(
            DurableRunPayload(run_id="r", graph_name="worker", input={}, thread_id=THREAD_ID),
            registry=app._registrations,
            thread_lock=scoped,
            observer=obs,
        )

    await _cancel_in_flight(graph, coro)

    # Cancellation is never swallowed into a result. Service Bus and Durable observe it as a
    # failure; Native catches ``Exception`` only, so a cancelled run emits no terminal event
    # (a surface difference that is pinned, not changed, by this behavior-preserving work).
    assert obs.names == (["started"] if surface == "native" else ["started", "failed"])
    token = scoped.acquire("worker", THREAD_ID)
    assert token is not None  # the lock was released; the thread is not stranded
    scoped.release("worker", THREAD_ID, token)
