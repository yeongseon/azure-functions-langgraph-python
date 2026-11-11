"""The guard against a REAL lease-losing lock, composed with the real saver and real LangGraph.

``tests/test_lease_loss_characterization.py`` pins today's UNGUARDED behavior: a stale
owner's checkpoint write is accepted. This module is its companion for the opt-in
guard. Two fake Function App instances share one fake lock container (virtual
service time) and one fake checkpoint container; the real ``AzureBlobLeaseThreadLock``
detects loss through its real renewal bookkeeping, and the real
``AzureBlobCheckpointSaver`` consults the guard it hands out.

Everything here is fake-backed. It proves the local guard contract and its documented
limits, NOT real-Azure lease rejection and NOT fencing:

* detection lags (no clock inference: until a renewal tick fails, the old owner still
  believes it owns the key);
* a check can pass an instant before an upload that is then accepted (the residual
  check-then-upload window);
* a run that finished (all writes done) and only then detected loss is still reported
  as failed, and nothing it already persisted is rolled back.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
import json
import re
from typing import Any, cast

import azure.functions as func
from langchain_core.runnables import RunnableConfig
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.locks.azure_blob import AzureBlobLeaseThreadLock
from azure_functions_langgraph.locks.guard import (
    ThreadLeaseLostError,
    config_with_ownership_guard,
)
from tests.ownership_guard_support import RecordingObserver
from tests.test_blob_lease_evaluation import _TimedContainer
from tests.test_checkpointers_azure_blob import (
    MockContainerClient,
    _checkpoint,
    _config,
    _metadata,
)
from tests.test_lease_loss_characterization import _fake_azure_sdk as _fake_azure_sdk
from tests.test_ownership_guard_langgraph import Graph, State, build_graph

pytestmark = pytest.mark.filterwarnings("ignore:.*auto_renew=False.*:UserWarning")

_GRAPH = "agent"
_THREAD = "thread-1"
_CHECKPOINT_BIN = re.compile(r"/checkpoints/([^/]+)/checkpoint\.bin$")


class Cluster:
    """Two Functions instances (own lock bookkeeping, own saver) over shared fake storage."""

    def __init__(self, stack: ExitStack, *, guarded: bool) -> None:
        self.lock_container = _TimedContainer()
        self.checkpoints = MockContainerClient()
        self.lock_a = AzureBlobLeaseThreadLock(
            container_client=self.lock_container, guard_checkpoint_writes=guarded
        )
        stack.callback(self.lock_a.close)
        self.lock_b = AzureBlobLeaseThreadLock(container_client=self.lock_container)
        stack.callback(self.lock_b.close)
        self.saver_a = AzureBlobCheckpointSaver(container_client=cast(Any, self.checkpoints))
        self.saver_b = AzureBlobCheckpointSaver(container_client=cast(Any, self.checkpoints))

    def guarded_config(
        self, token: str, *, thread_id: str = _THREAD, checkpoint_id: str | None = None
    ) -> RunnableConfig:
        """The config a transport would hand to the graph for A's acquisition."""
        guard = self.lock_a.ownership_guard(_GRAPH, thread_id, token)
        built = config_with_ownership_guard(dict(_config(thread_id, "", checkpoint_id)), guard)
        return cast(RunnableConfig, built)

    def b_takes_over_and_writes(self) -> str:
        """A never renewed, so its lease lapses on the virtual service; B acquires and writes."""
        self.lock_container.clock.now = 61
        token_b = self.lock_b.acquire(_GRAPH, _THREAD)
        assert token_b is not None
        self.saver_b.put(
            _config(_THREAD),
            _checkpoint("cp-b", channel_values={"k": "b"}, channel_versions={"k": "v-b"}),
            _metadata(step=9, run_id="run-b"),
            {"k": "v-b"},
        )
        return token_b

    def takeover_and_a_notices(self) -> None:
        """B takes over; A's next renewal tick sees the foreign lease and marks it lost."""
        self.b_takes_over_and_writes()
        self.lock_a._renew_all_once()

    def checkpoint_ids(self) -> set[str]:
        return {
            match.group(1)
            for path in self.checkpoints.blobs
            if (match := _CHECKPOINT_BIN.search(path))
        }


@pytest.fixture(params=[True, False], ids=["guarded", "unguarded"])
def cluster(request: pytest.FixtureRequest) -> Iterator[Cluster]:
    with ExitStack() as stack:
        yield Cluster(stack, guarded=bool(request.param))


@pytest.fixture
def guarded_cluster() -> Iterator[Cluster]:
    with ExitStack() as stack:
        yield Cluster(stack, guarded=True)


# ---------------------------------------------------------------------------
# 1. The saver against a real, losing lock
# ---------------------------------------------------------------------------


def test_stale_owner_is_rejected_once_its_renewal_notices_the_loss(
    guarded_cluster: Cluster,
) -> None:
    # Given: A holds the lock and writes a checkpoint under its guard.
    c = guarded_cluster
    token_a = c.lock_a.acquire(_GRAPH, _THREAD)
    assert token_a is not None
    c.saver_a.put(
        c.guarded_config(token_a),
        _checkpoint("cp-001", channel_values={"k": "a1"}, channel_versions={"k": "v1"}),
        _metadata(run_id="run-a"),
        {"k": "v1"},
    )
    # And: A's lease lapses, B takes over and writes, then A's renewal tick notices.
    c.takeover_and_a_notices()
    before = dict(c.checkpoints.blobs)

    # When: A's still-running graph tries to commit a checkpoint and pending writes.
    with pytest.raises(ThreadLeaseLostError) as stale_put:
        c.saver_a.put(
            c.guarded_config(token_a, checkpoint_id="cp-001"),
            _checkpoint("cp-002a", channel_values={"k": "stale"}, channel_versions={"k": "v2a"}),
            _metadata(step=2, run_id="run-a"),
            {"k": "v2a"},
        )
    with pytest.raises(ThreadLeaseLostError):
        c.saver_a.put_writes(
            c.guarded_config(token_a, checkpoint_id="cp-b"),
            [("messages", "stale-write")],
            task_id="task-a",
        )

    # Then: the stale owner wrote nothing, so B's lineage is intact: no fork, B is the
    # default head, and no stale pending write is attached to B's checkpoint.
    assert stale_put.value.reason == "lost"
    assert stale_put.value.thread_id == _THREAD
    assert c.checkpoints.blobs == before
    assert c.saver_b.get_tuple(_config(_THREAD, checkpoint_id="cp-002a")) is None
    latest = c.saver_b.get_tuple(_config(_THREAD))
    assert latest is not None and latest.checkpoint["id"] == "cp-b"
    assert latest.pending_writes == []


def test_detection_lags_until_a_renewal_tick_fails(guarded_cluster: Cluster) -> None:
    # Given: A's lease already lapsed on the service and B owns it, but A has not run
    # a renewal tick yet (a frozen host, a starved renewal thread...).
    c = guarded_cluster
    token_a = c.lock_a.acquire(_GRAPH, _THREAD)
    assert token_a is not None
    c.b_takes_over_and_writes()

    # When: A's graph writes before it could have noticed.
    c.saver_a.put(
        c.guarded_config(token_a),
        _checkpoint("cp-002a", channel_values={"k": "stale"}, channel_versions={"k": "v2a"}),
        _metadata(step=2, run_id="run-a"),
        {"k": "v2a"},
    )

    # Then: the guard cannot know (there is deliberately no clock inference), so the
    # stale write is ACCEPTED: the guard reduces writes after DETECTED loss, nothing more.
    assert "cp-002a" in c.checkpoint_ids()
    assert c.lock_a._ownership_problem((_GRAPH, _THREAD), token_a) is None

    # And: the very next renewal tick is what flips it.
    c.lock_a._renew_all_once()
    assert c.lock_a._ownership_problem((_GRAPH, _THREAD), token_a) == "lost"


def test_a_check_can_pass_just_before_an_upload_that_is_then_accepted(
    guarded_cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: A's renewal thread notices the loss in the instant between the guard's
    # check for the commit-marker upload and that upload itself.
    c = guarded_cluster
    token_a = c.lock_a.acquire(_GRAPH, _THREAD)
    assert token_a is not None
    c.b_takes_over_and_writes()
    real_upload = c.saver_a._upload_blob

    def marker_upload_races_the_renewal(
        path: str, data: bytes, metadata: dict[str, str], overwrite: bool = True
    ) -> None:
        if path.endswith("/cp-002a/checkpoint.bin"):
            c.lock_a._renew_all_once()
        real_upload(path, data, metadata, overwrite)

    monkeypatch.setattr(c.saver_a, "_upload_blob", marker_upload_races_the_renewal)

    # When: A commits the checkpoint.
    with pytest.raises(ThreadLeaseLostError) as raised:
        c.saver_a.put(
            c.guarded_config(token_a),
            _checkpoint("cp-002a", channel_values={"k": "stale"}, channel_versions={"k": "v2a"}),
            _metadata(step=2, run_id="run-a"),
            {"k": "v2a"},
        )

    # Then: the residual window is real. The marker upload had already passed its check,
    # so the stale checkpoint IS committed, and only the NEXT check (the clean latest
    # hint) was rejected. This is why the guard is not fencing.
    assert raised.value.reason == "lost"
    assert "cp-002a" in c.checkpoint_ids()
    hint = json.loads(c.checkpoints.blobs[f"threads/{_THREAD}/ns//latest.json"].data)
    assert hint["dirty"] is True


# ---------------------------------------------------------------------------
# 2. The whole stack: native handler + real LangGraph + real saver + real lock
# ---------------------------------------------------------------------------


class SyncDurabilityGraph:
    """A structural graph that runs the compiled graph with ``durability="sync"``.

    The native handler calls ``graph.invoke(input, config=...)`` with LangGraph's default
    durability (``"async"``: saves run in the background), which would make the point of
    "ownership was lost HERE" non-deterministic. Registration is protocol-based, so a thin
    wrapper pins sync durability and offers an after-run hook.
    """

    def __init__(self, graph: Graph, *, after_run: Callable[[], None] | None = None) -> None:
        self._graph = graph
        self.checkpointer = graph.checkpointer
        self._after_run = after_run

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
        result = self._graph.invoke(
            cast(State, input), cast(RunnableConfig, config), durability="sync"
        )
        if self._after_run is not None:
            self._after_run()
        return result

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[Any]:
        yield from self._graph.stream(
            cast(State, input),
            cast(RunnableConfig, config),
            stream_mode="values",
            durability="sync",
        )


def invoke_over_http(app: LangGraphApp) -> func.HttpResponse:
    reg = app._registrations[_GRAPH]
    request = func.HttpRequest(
        method="POST",
        url=f"https://example.test/api/graphs/{_GRAPH}/invoke",
        body=json.dumps(
            {"input": {"log": ["in"]}, "config": {"configurable": {"thread_id": _THREAD}}}
        ).encode(),
        headers={"X-Request-ID": "rid-lease-loss"},
    )
    return app._handle_invoke(request, reg)


def test_native_invoke_stops_writing_after_ownership_is_lost_mid_run(cluster: Cluster) -> None:
    # Given: the app's lock is A's lock, and the graph's node "b" is where A loses the
    # lease to B (B takes over and writes; A's renewal tick then notices).
    observer = RecordingObserver()
    blobs_when_detected: list[dict[str, object]] = []

    def hook(name: str) -> None:
        if name == "b":
            cluster.takeover_and_a_notices()
            blobs_when_detected.append(dict(cluster.checkpoints.blobs))

    graph = SyncDurabilityGraph(build_graph(cluster.saver_a, hook=hook))
    app = LangGraphApp(thread_lock=cluster.lock_a, observer=observer)
    app.register(graph=graph, name=_GRAPH)

    # When: the request runs to completion.
    response = invoke_over_http(app)

    # Then: with the guard the run is reported as failed with the sanitized form, the
    # failure carries the typed error, and A wrote NOTHING after it lost ownership.
    guarded = cluster.lock_a._guard_checkpoint_writes
    assert blobs_when_detected, "the takeover hook never ran"
    if guarded:
        assert response.status_code == 500
        assert json.loads(response.get_body()) == {
            "error": "error",
            "detail": "Graph execution failed",
            "request_id": "rid-lease-loss",
        }
        assert observer.events == ["started", "failed"]
        failure = observer.failures[0]
        assert isinstance(failure, ThreadLeaseLostError) and failure.reason == "lost"
        assert dict(cluster.checkpoints.blobs) == blobs_when_detected[0]
        latest = cluster.saver_b.get_tuple(_config(_THREAD))
        assert latest is not None and latest.checkpoint["id"] == "cp-b"
    else:
        # Default (opt-in OFF): exactly today's behavior. The run "succeeds" and the stale
        # owner kept writing checkpoints after it lost ownership.
        assert response.status_code == 200
        assert observer.events == ["started", "completed"]
        assert dict(cluster.checkpoints.blobs) != blobs_when_detected[0]
    # In both modes A's cleanup never disturbs the new owner: B still holds the lease.
    assert cluster.lock_a.acquire(_GRAPH, _THREAD) is None


def test_native_invoke_that_finished_all_writes_is_still_reported_failed_after_loss(
    cluster: Cluster,
) -> None:
    # Given: a run whose every write succeeded, and whose ownership loss only becomes
    # detectable AFTER the last write (between the graph returning and the response).
    observer = RecordingObserver()
    blobs_after_last_write: list[dict[str, object]] = []

    def lose_ownership_after_the_run() -> None:
        blobs_after_last_write.append(dict(cluster.checkpoints.blobs))
        cluster.takeover_and_a_notices()

    graph = SyncDurabilityGraph(
        build_graph(cluster.saver_a), after_run=lose_ownership_after_the_run
    )
    app = LangGraphApp(thread_lock=cluster.lock_a, observer=observer)
    app.register(graph=graph, name=_GRAPH)

    # When: the handler finishes the request.
    response = invoke_over_http(app)

    # Then: guarded, the terminal check refuses to report success (the saver had nothing
    # left to reject); unguarded it reports success exactly as today. Either way the run's
    # own checkpoints stay committed: the error is NOT a rollback and NOT proof of a stale
    # write, so a blind retry of non-idempotent work would repeat the turn.
    guarded = cluster.lock_a._guard_checkpoint_writes
    assert blobs_after_last_write
    persisted = {path for path in blobs_after_last_write[0] if path in cluster.checkpoints.blobs}
    assert persisted == set(blobs_after_last_write[0])
    assert any(_CHECKPOINT_BIN.search(path) for path in persisted)
    if guarded:
        assert response.status_code == 500
        assert observer.events == ["started", "failed"]
        failure = observer.failures[0]
        assert isinstance(failure, ThreadLeaseLostError) and failure.reason == "lost"
    else:
        assert response.status_code == 200
        assert observer.events == ["started", "completed"]
