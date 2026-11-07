"""Real LangGraph + the real Blob saver (over in-memory fakes): the guard reaches every write.

The unit tests prove each piece on its own. This module proves they compose with
the real Pregel loop, on whichever LangGraph 1.x is installed (CI runs it on
``langgraph==1.0.0`` and on the latest 1.x, and the maintainer also runs it on
1.1.x):

* the guard travels in ``config["configurable"]`` and reaches EVERY
  ``put`` / ``put_writes`` (and their async twins) in every execution mode,
  including inherited subgraph checkpoints, which is proven by
  ``require_ownership_guard=True`` (a write without a guard fails closed) and by
  ``checks == uploads`` (every blob upload was preceded by exactly one check);
* a guard that trips inside the run makes the run raise the SAME
  ``ThreadLeaseLostError`` and stops every later checkpoint write;
* the key never leaks into ``debug`` / ``checkpoints`` stream payloads and is
  never persisted;
* ``update_state`` paths that discard the incoming config (``__copy__``, INPUT)
  are covered by the adapter's scoped binding.

Fake storage only: no Azure, no cloud claim, and no fencing claim. A passing
check never proves a write could not be stale (see the residual-window test in
``test_lease_loss_guarded.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import re
import threading
from typing import Annotated, Literal, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY,
    OwnershipGuard,
    ThreadLeaseLostError,
    bind_ownership_guard,
    config_with_ownership_guard,
)
from tests.ownership_guard_support import ScriptedGuard
from tests.test_checkpointers_azure_blob import (
    AsyncMockContainerClient,
    MockContainerClient,
)
from tests.test_checkpointers_azure_blob import (
    _install_fake_azure_modules as _install_fake_azure_modules,
)

pytestmark = pytest.mark.usefixtures("_install_fake_azure_modules")


def _merge(left: list[str], right: list[str]) -> list[str]:
    """Append only new entries, so a subgraph that returns its full state does not duplicate."""
    return left + [item for item in right if item not in left]


class State(TypedDict):
    log: Annotated[list[str], _merge]


Graph = CompiledStateGraph[State, None, State, State]
Durability = Literal["sync", "async", "exit"]
Mode = Literal["invoke", "stream", "ainvoke", "astream"]
Flavor = Literal["sync", "native", "fallback"]

# invoke/stream drive the sync saver methods; ainvoke/astream drive the native
# aio methods when an aio client exists, else the executor fallback.
MODES: list[tuple[Mode, Flavor]] = [
    ("invoke", "sync"),
    ("stream", "sync"),
    ("ainvoke", "native"),
    ("astream", "native"),
    ("ainvoke", "fallback"),
    ("astream", "fallback"),
]
DURABILITIES: list[Durability] = ["sync", "async", "exit"]


class _Step:
    """A graph node that appends its name to ``log``.

    A class, because LangGraph's typed ``add_node`` needs a callable whose parameter is
    NAMED ``state`` and ``Callable[[State], State]`` has no parameter names.
    """

    def __init__(self, name: str, hook: Callable[[str], None] | None) -> None:
        self._name = name
        self._hook = hook

    def __call__(self, state: State) -> State:
        if self._hook is not None:
            self._hook(self._name)
        return {"log": [self._name]}


def build_graph(
    saver: AzureBlobCheckpointSaver,
    *,
    hook: Callable[[str], None] | None = None,
    subgraph: bool = False,
) -> Graph:
    """``a -> [sub(s1)] -> b -> c``; the optional subgraph inherits the parent's saver."""
    outer = StateGraph(State)
    outer.add_node("a", _Step("a", hook))
    outer.add_node("b", _Step("b", hook))
    outer.add_node("c", _Step("c", hook))
    outer.add_edge(START, "a")
    if subgraph:
        inner = StateGraph(State)
        inner.add_node("s1", _Step("s1", hook))
        inner.add_edge(START, "s1")
        inner.add_edge("s1", END)
        outer.add_node("sub", inner.compile(checkpointer=True))
        outer.add_edge("a", "sub")
        outer.add_edge("sub", "b")
    else:
        outer.add_edge("a", "b")
    outer.add_edge("b", "c")
    outer.add_edge("c", END)
    return outer.compile(checkpointer=saver)


def expected_log(*, subgraph: bool) -> list[str]:
    return ["in", "a", "s1", "b", "c"] if subgraph else ["in", "a", "b", "c"]


def make_saver(
    *, flavor: Flavor = "sync", require: bool = False
) -> tuple[AzureBlobCheckpointSaver, MockContainerClient]:
    container = MockContainerClient()
    aio = AsyncMockContainerClient(container) if flavor == "native" else None
    saver = AzureBlobCheckpointSaver(
        container_client=container, aio_container_client=aio, require_ownership_guard=require
    )
    return saver, container


def run_config(thread_id: str, guard: OwnershipGuard | None) -> RunnableConfig:
    """The config a transport hands to the graph: built by the production helper."""
    built = config_with_ownership_guard({"configurable": {"thread_id": thread_id}}, guard)
    return cast(RunnableConfig, built)


class Uploads:
    """Every blob upload the saver performs, in order, for the sync and native-aio helpers."""

    def __init__(self, saver: AzureBlobCheckpointSaver, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: list[str] = []
        sync_upload = saver._upload_blob
        async_upload = saver._aupload_blob

        def record_sync(path: str, data: bytes, metadata: dict[str, str]) -> None:
            self.paths.append(path)
            sync_upload(path, data, metadata)

        async def record_async(path: str, data: bytes, metadata: dict[str, str]) -> None:
            self.paths.append(path)
            await async_upload(path, data, metadata)

        monkeypatch.setattr(saver, "_upload_blob", record_sync)
        monkeypatch.setattr(saver, "_aupload_blob", record_async)

    def for_thread(self, thread_id: str) -> list[str]:
        return [path for path in self.paths if path.startswith(f"threads/{thread_id}/")]

    def writes(self) -> list[str]:
        return [path for path in self.paths if "/writes/" in path]

    def checkpoint_namespaces(self) -> set[str]:
        pattern = re.compile(r"^threads/[^/]+/ns/([^/]*)/checkpoints/[^/]+/checkpoint\.bin$")
        return {match.group(1) for path in self.paths if (match := pattern.match(path))}


async def drive(
    graph: Graph, mode: Mode, config: RunnableConfig, durability: Durability
) -> list[str]:
    """Run the graph through one public entry point and return the final ``log``."""
    payload: State = {"log": ["in"]}
    if mode == "invoke":
        return cast(list[str], graph.invoke(payload, config, durability=durability)["log"])
    last: list[str] = []
    if mode == "stream":
        for chunk in graph.stream(payload, config, stream_mode="values", durability=durability):
            last = chunk["log"]
        return last
    if mode == "ainvoke":
        result = await graph.ainvoke(payload, config, durability=durability)
        return cast(list[str], result["log"])
    async for chunk in graph.astream(payload, config, stream_mode="values", durability=durability):
        last = chunk["log"]
    return last


def committed_logs(saver: AzureBlobCheckpointSaver, thread_id: str) -> list[list[str]]:
    """The ``log`` channel of every committed checkpoint of the root namespace."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    return [list(item.checkpoint["channel_values"].get("log", [])) for item in saver.list(config)]


# ---------------------------------------------------------------------------
# 1. Propagation: the guard reaches EVERY write, in every mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subgraph", [False, True])
@pytest.mark.parametrize("durability", DURABILITIES)
@pytest.mark.parametrize(("mode", "flavor"), MODES)
async def test_guard_reaches_every_checkpoint_and_pending_write(
    mode: Mode,
    flavor: Flavor,
    durability: Durability,
    subgraph: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a saver that FAILS CLOSED when a write arrives without a guard.
    saver, _ = make_saver(flavor=flavor, require=True)
    uploads = Uploads(saver, monkeypatch)
    guard = ScriptedGuard()
    graph = build_graph(saver, subgraph=subgraph)

    # When the real Pregel loop runs a guarded request through one public entry point.
    log = await drive(graph, mode, run_config("t-1", guard), durability)

    # Then the run succeeded, so no put/put_writes lost the guard (a miss raises "missing"),
    assert log == expected_log(subgraph=subgraph)
    # and every single blob upload was preceded by exactly one check.
    assert uploads.paths, "no upload happened: the test would prove nothing"
    assert guard.checks == len(uploads.paths)
    if durability != "exit":
        assert uploads.writes(), "no pending write happened: put_writes was not exercised"
    namespaces = uploads.checkpoint_namespaces()
    assert "" in namespaces
    assert (len(namespaces) > 1) is subgraph, namespaces


@pytest.mark.parametrize(("mode", "flavor"), MODES)
async def test_a_second_run_on_the_same_thread_is_guarded_too(
    mode: Mode, flavor: Flavor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a thread that already has committed state, resumed under a NEW guard.
    saver, _ = make_saver(flavor=flavor, require=True)
    graph = build_graph(saver)
    await drive(graph, mode, run_config("t-1", ScriptedGuard()), "sync")
    uploads = Uploads(saver, monkeypatch)
    second = ScriptedGuard()

    # When the next request on that thread runs.
    await drive(graph, mode, run_config("t-1", second), "sync")

    # Then every write of the second run was checked against the second guard only.
    assert uploads.paths
    assert second.checks == len(uploads.paths)


# ---------------------------------------------------------------------------
# 2. A guard that trips inside the run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trip_at", ["b", "s1"])
@pytest.mark.parametrize(("mode", "flavor"), MODES)
async def test_trip_inside_the_run_raises_the_same_error_and_stops_all_writes(
    mode: Mode, flavor: Flavor, trip_at: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a graph whose node "trip_at" loses ownership while it executes.
    saver, container = make_saver(flavor=flavor)
    guard = ScriptedGuard()
    blobs_when_tripped: list[frozenset[str]] = []

    def hook(name: str) -> None:
        if name == trip_at:
            blobs_when_tripped.append(frozenset(container.blobs))
            guard.trip()

    graph = build_graph(saver, hook=hook, subgraph=True)

    # When the run continues after the loss was detected (sync durability: each
    # step is fully saved before the next one starts, so the cut is exact).
    with pytest.raises(ThreadLeaseLostError) as raised:
        await drive(graph, mode, run_config("t-1", guard), "sync")

    # Then the very same error type surfaced from invoke/stream/ainvoke/astream
    # (LangGraph did not wrap it), and not one blob was written after the trip.
    assert raised.value.reason == "lost"
    assert blobs_when_tripped, "the trip hook never ran"
    assert frozenset(container.blobs) == blobs_when_tripped[0]
    assert all("c" not in log and "b" not in log for log in committed_logs(saver, "t-1")) or (
        trip_at == "s1"
    )


@pytest.mark.parametrize(("mode", "flavor"), MODES)
@pytest.mark.parametrize("durability", ["async", "exit"])
async def test_trip_with_async_or_exit_durability_never_commits_a_later_checkpoint(
    mode: Mode, flavor: Flavor, durability: Durability
) -> None:
    # Given a node that loses ownership under a durability mode that saves in the
    # background (a later superstep may start before the error surfaces).
    saver, _ = make_saver(flavor=flavor)
    guard = ScriptedGuard()

    def hook(name: str) -> None:
        if name == "b":
            guard.trip()

    graph = build_graph(saver, hook=hook)

    # When the run proceeds.
    with pytest.raises(ThreadLeaseLostError):
        await drive(graph, mode, run_config("t-1", guard), durability)

    # Then no committed checkpoint contains the output of a node that ran after
    # the loss, whatever the interleaving was.
    for log in committed_logs(saver, "t-1"):
        assert "b" not in log
        assert "c" not in log


async def test_rejected_checkpoint_is_not_committed_and_the_previous_one_stays_readable() -> None:
    # Given a thread with committed state, resumed by a run that loses ownership.
    saver, _ = make_saver()
    graph = build_graph(saver)
    await drive(graph, "invoke", run_config("t-1", ScriptedGuard()), "sync")
    before = committed_logs(saver, "t-1")
    guard = ScriptedGuard(trip_on=1)

    # When the next run is rejected at its first write.
    with pytest.raises(ThreadLeaseLostError):
        await drive(graph, "invoke", run_config("t-1", guard), "sync")

    # Then the previously committed history is untouched and readable.
    assert committed_logs(saver, "t-1") == before


# ---------------------------------------------------------------------------
# 3. Fail closed, opt-in default, client-controlled junk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "flavor"), MODES)
async def test_required_mode_rejects_an_unguarded_run_before_any_upload(
    mode: Mode, flavor: Flavor
) -> None:
    # Given a saver that requires a guard and a request that carries none.
    saver, container = make_saver(flavor=flavor, require=True)
    graph = build_graph(saver)

    # When the graph runs (for example Service Bus session_exclusive: no lock, no guard).
    with pytest.raises(ThreadLeaseLostError) as raised:
        await drive(graph, mode, run_config("t-1", None), "sync")

    # Then it fails closed with reason "missing" and nothing was written.
    assert raised.value.reason == "missing"
    assert container.blobs == {}


@pytest.mark.parametrize(("mode", "flavor"), MODES)
async def test_default_saver_runs_unguarded_exactly_as_before(mode: Mode, flavor: Flavor) -> None:
    # Given the default saver (opt-in feature OFF) and no guard at all.
    saver, container = make_saver(flavor=flavor)

    # When the graph runs.
    log = await drive(build_graph(saver), mode, run_config("t-1", None), "sync")

    # Then behavior is unchanged: a normal, fully checkpointed run.
    assert log == expected_log(subgraph=False)
    assert container.blobs


@pytest.mark.parametrize("junk", ["evil", {}, [], 42, True, None])
async def test_client_supplied_junk_under_the_key_is_ignored_by_default_and_rejected_when_required(
    junk: object,
) -> None:
    # Given a config whose private key holds a client-controlled (non-guard) value.
    config: RunnableConfig = {
        "configurable": {"thread_id": "t-1", OWNERSHIP_GUARD_CONFIG_KEY: junk}
    }
    default_saver, _ = make_saver()
    required_saver, required_container = make_saver(require=True)

    # When the default saver runs it, it is ignored (no guard, so no behavior change),
    log = await drive(build_graph(default_saver), "invoke", config, "sync")
    assert log == expected_log(subgraph=False)
    # and a fail-closed saver rejects it as a missing guard instead of trusting it.
    with pytest.raises(ThreadLeaseLostError) as raised:
        await drive(build_graph(required_saver), "invoke", config, "sync")
    assert raised.value.reason == "missing"
    assert required_container.blobs == {}


# ---------------------------------------------------------------------------
# 4. The key never leaks and is never persisted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subgraph", [False, True])
def test_debug_and_checkpoints_streams_expose_neither_the_key_nor_the_guard(
    subgraph: bool,
) -> None:
    # Given a guarded run that streams LangGraph's debug and checkpoints events
    # (the native request model accepts any stream_mode string).
    saver, _ = make_saver()
    guard = ScriptedGuard()
    graph = build_graph(saver, subgraph=subgraph)

    # When the events are collected.
    events = list(
        graph.stream(
            {"log": ["in"]},
            run_config("t-1", guard),
            stream_mode=["debug", "checkpoints"],
        )
    )

    # Then both modes produced events, and not one payload carries the key name,
    # the key prefix LangGraph strips, or the guard's repr.
    modes = {mode for mode, _ in events}
    assert {"debug", "checkpoints"} <= modes
    text = repr(events) + json.dumps(events, default=str)
    assert OWNERSHIP_GUARD_CONFIG_KEY not in text
    assert "__pregel_" not in text
    assert "OwnershipGuard" not in text
    assert guard.checks > 0


def test_nothing_guard_related_is_persisted() -> None:
    # Given a fully guarded run with a subgraph.
    saver, container = make_saver()
    graph = build_graph(saver, subgraph=True)
    graph.invoke({"log": ["in"]}, run_config("t-1", ScriptedGuard()))

    # When every stored blob and every checkpoint's metadata is inspected.
    assert container.blobs
    for path, record in container.blobs.items():
        assert OWNERSHIP_GUARD_CONFIG_KEY.encode() not in record.data, path
        assert b"OwnershipGuard" not in record.data, path
        assert not any(key.startswith("__") for key in record.metadata), path
    config: RunnableConfig = {"configurable": {"thread_id": "t-1", "checkpoint_ns": ""}}
    checkpoints = list(saver.list(config))

    # Then no checkpoint metadata key is private, and the saver's returned config is clean.
    assert checkpoints
    for item in checkpoints:
        assert not any(key.startswith("__") for key in item.metadata)
        assert set(item.config["configurable"]) <= {"thread_id", "checkpoint_ns", "checkpoint_id"}


# ---------------------------------------------------------------------------
# 5. update_state: configs the saver (or LangGraph) rebuilds carry no guard
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch) -> tuple[AzureBlobCheckpointSaver, Graph, Uploads]:
    """A fail-closed saver whose thread "t-1" already holds committed state."""
    saver, _ = make_saver(require=True)
    graph = build_graph(saver)
    graph.invoke({"log": ["in"]}, run_config("t-1", ScriptedGuard()))
    return saver, graph, Uploads(saver, monkeypatch)


def test_update_state_without_any_guard_fails_closed(
    seeded: tuple[AzureBlobCheckpointSaver, Graph, Uploads],
) -> None:
    # Given a fail-closed saver and an update that carries no guard anywhere.
    _, graph, uploads = seeded

    # When the state is updated (direct maintenance write).
    with pytest.raises(ThreadLeaseLostError) as raised:
        graph.update_state(run_config("t-1", None), {"log": ["u"]}, as_node="a")

    # Then it is rejected as "missing" before any upload.
    assert raised.value.reason == "missing"
    assert uploads.paths == []


def test_node_update_carries_the_guard_through_the_incoming_config(
    seeded: tuple[AzureBlobCheckpointSaver, Graph, Uploads],
) -> None:
    # Given a node update whose guard travels only in the config.
    saver, graph, uploads = seeded
    guard = ScriptedGuard()

    # When the state is updated as a node.
    graph.update_state(run_config("t-1", guard), {"log": ["u"]}, as_node="a")

    # Then LangGraph kept the guard on every write of this path.
    assert uploads.paths
    assert guard.checks == len(uploads.paths)
    assert "u" in committed_logs(saver, "t-1")[0]


def test_copy_update_discards_the_config_guard_and_needs_the_scoped_binding(
    seeded: tuple[AzureBlobCheckpointSaver, Graph, Uploads],
) -> None:
    # Given an as_node="__copy__" update: LangGraph rebuilds its put() config from
    # the STORED checkpoint, so the guard in the incoming config is dropped.
    _, graph, uploads = seeded
    guard = ScriptedGuard()

    # When it runs WITHOUT the binding, a fail-closed saver reports the gap loudly.
    with pytest.raises(ThreadLeaseLostError) as raised:
        graph.update_state(run_config("t-1", guard), None, as_node="__copy__")
    assert raised.value.reason == "missing"
    assert uploads.paths == []
    assert guard.checks == 0

    # And WITH the adapter's scoped binding the very same call is guarded.
    with bind_ownership_guard(guard):
        graph.update_state(run_config("t-1", guard), None, as_node="__copy__")
    assert uploads.paths
    assert guard.checks == len(uploads.paths)


def test_bound_guard_rejects_a_copy_update_after_loss(
    seeded: tuple[AzureBlobCheckpointSaver, Graph, Uploads],
) -> None:
    # Given a tripped guard bound around an as_node="__copy__" update.
    saver, graph, uploads = seeded
    before = committed_logs(saver, "t-1")
    guard = ScriptedGuard(trip_on=1)

    # When the update runs.
    with bind_ownership_guard(guard), pytest.raises(ThreadLeaseLostError) as raised:
        graph.update_state(run_config("t-1", guard), None, as_node="__copy__")

    # Then it is rejected before any upload and the history is unchanged.
    assert raised.value.reason == "lost"
    assert uploads.paths == []
    assert committed_logs(saver, "t-1") == before


def test_input_update_on_a_fresh_thread_is_guarded_by_the_binding(
    seeded: tuple[AzureBlobCheckpointSaver, Graph, Uploads],
) -> None:
    # Given a thread with no state yet, seeded as INPUT (LangGraph then writes
    # through the config the saver returned, which carries no guard).
    _, graph, uploads = seeded
    guard = ScriptedGuard()

    # When the adapter's scoped binding surrounds the update.
    with bind_ownership_guard(guard):
        graph.update_state(run_config("t-fresh", guard), {"log": ["seed"]}, as_node=START)

    # Then every upload of that path was checked.
    assert uploads.for_thread("t-fresh")
    assert guard.checks == len(uploads.paths)


def test_binding_is_gone_after_the_update(
    seeded: tuple[AzureBlobCheckpointSaver, Graph, Uploads],
) -> None:
    # Given a guarded update that completed.
    _, graph, uploads = seeded
    guard = ScriptedGuard()
    with bind_ownership_guard(guard):
        graph.update_state(run_config("t-1", guard), {"log": ["u"]}, as_node="a")
    checks_after_update = guard.checks

    # When a later, unguarded write arrives on the same thread.
    with pytest.raises(ThreadLeaseLostError) as raised:
        graph.update_state(run_config("t-1", None), {"log": ["late"]}, as_node="a")

    # Then the earlier guard no longer applies (fail closed again) and was not consulted.
    assert raised.value.reason == "missing"
    assert guard.checks == checks_after_update
    assert uploads.paths


# ---------------------------------------------------------------------------
# 6. Concurrency: no cross-talk through shared saver state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("repeat", range(5))
def test_concurrent_runs_on_one_saver_are_guarded_independently(
    repeat: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given two threads sharing ONE saver and ONE graph, one of whose guards trips mid-run.
    saver, _ = make_saver(require=True)
    uploads = Uploads(saver, monkeypatch)
    healthy, doomed = ScriptedGuard(), ScriptedGuard()
    both_inside = threading.Barrier(2, timeout=5)

    def hook(name: str) -> None:
        if name == "a":
            both_inside.wait()
        if name == "b" and threading.current_thread().name == "doomed":
            doomed.trip()

    graph = build_graph(saver, hook=hook)
    outcomes: dict[str, object] = {}

    def run(thread_id: str, guard: ScriptedGuard) -> None:
        try:
            outcomes[thread_id] = graph.invoke({"log": ["in"]}, run_config(thread_id, guard))
        except BaseException as exc:  # noqa: BLE001 - captured for the assertions below
            outcomes[thread_id] = exc

    workers = [
        threading.Thread(target=run, args=("t-ok", healthy), name="healthy"),
        threading.Thread(target=run, args=("t-doomed", doomed), name="doomed"),
    ]

    # When both runs execute at the same time.
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    # Then only the run whose guard tripped failed, and each guard saw only its own writes.
    assert all(not worker.is_alive() for worker in workers)
    assert isinstance(outcomes["t-doomed"], ThreadLeaseLostError)
    ok_result = outcomes["t-ok"]
    assert isinstance(ok_result, dict) and ok_result["log"] == ["in", "a", "b", "c"]
    assert healthy.checks == len(uploads.for_thread("t-ok"))
    assert doomed.checks >= 1


async def test_async_runs_on_one_saver_are_guarded_independently() -> None:
    # Given two concurrent native-async runs on one saver; the guard of one trips.
    saver, _ = make_saver(flavor="native", require=True)
    healthy, doomed = ScriptedGuard(), ScriptedGuard(trip_on=2)
    graph = build_graph(saver)

    # When both run concurrently.
    results = await asyncio.gather(
        drive(graph, "ainvoke", run_config("t-ok", healthy), "sync"),
        drive(graph, "ainvoke", run_config("t-doomed", doomed), "sync"),
        return_exceptions=True,
    )

    # Then only the doomed run failed, with the guard's own error.
    assert results[0] == expected_log(subgraph=False)
    assert isinstance(results[1], ThreadLeaseLostError)
    assert healthy.checks > 0
