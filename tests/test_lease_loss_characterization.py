"""Characterization of stale-writer behavior after a Blob lease is lost (#765).

Mock/fake-only evidence: virtual service time models lease expiry, and the real
``AzureBlobCheckpointSaver`` runs over an in-memory container. Nothing here is
live Azure certification. The tests pin the CURRENT behavior so that any change
(for example adding checkpoint-level fencing) must update them deliberately:

* the Blob lease gives mutual exclusion on lock acquisition only;
* the saver issues unconditional ``overwrite=True`` uploads, so a stale owner
  that kept running after losing its lease can still write.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
import logging
import sys
import types
from typing import Any, cast

import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.locks.azure_blob import AzureBlobLeaseThreadLock
from tests.test_blob_lease_evaluation import _TimedContainer, _TimedLease
from tests.test_checkpointers_azure_blob import (
    FakeResourceNotFoundError,
    MockContainerClient,
    _checkpoint,
    _config,
    _metadata,
)
from tests.test_locks_azure_blob import FakeHttpResponseError, FakeResourceExistsError

pytestmark = pytest.mark.filterwarnings("ignore:.*auto_renew=False.*:UserWarning")

_GRAPH = "graph"
_THREAD = "thread-1"


@pytest.fixture(autouse=True)
def _fake_azure_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake SDK that accepts both the lock fake and the checkpoint fake container."""
    blob_mod = types.ModuleType("azure.storage.blob")
    setattr(blob_mod, "ContainerClient", (_TimedContainer, MockContainerClient))
    exceptions_mod = types.ModuleType("azure.core.exceptions")
    setattr(exceptions_mod, "ResourceNotFoundError", FakeResourceNotFoundError)
    setattr(exceptions_mod, "ResourceExistsError", FakeResourceExistsError)
    setattr(exceptions_mod, "HttpResponseError", FakeHttpResponseError)
    for name, module in (
        ("azure", types.ModuleType("azure")),
        ("azure.storage", types.ModuleType("azure.storage")),
        ("azure.storage.blob", blob_mod),
        ("azure.core", types.ModuleType("azure.core")),
        ("azure.core.exceptions", exceptions_mod),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    # Drive renewal ticks by hand so tests never wait on real time.
    monkeypatch.setattr(
        AzureBlobLeaseThreadLock, "_renewal_worker", lambda self: self._shutdown_event.wait()
    )


class _Hosts:
    """Two Functions instances (own lock bookkeeping) sharing one lock container
    and one checkpoint container."""

    def __init__(self, stack: ExitStack) -> None:
        self.lock_container = _TimedContainer()
        self.checkpoint_container = MockContainerClient()
        self.lock_a = AzureBlobLeaseThreadLock(container_client=self.lock_container)
        stack.callback(self.lock_a.close)
        self.lock_b = AzureBlobLeaseThreadLock(container_client=self.lock_container)
        stack.callback(self.lock_b.close)
        self.saver_a = AzureBlobCheckpointSaver(
            container_client=cast(Any, self.checkpoint_container)
        )
        self.saver_b = AzureBlobCheckpointSaver(
            container_client=cast(Any, self.checkpoint_container)
        )

    def lease_of(self, lock: AzureBlobLeaseThreadLock) -> _TimedLease:
        lease = lock._active_leases[(_GRAPH, _THREAD)].lease
        assert isinstance(lease, _TimedLease)
        return lease

    def lose_lease_a_and_acquire_b(self) -> tuple[str, str]:
        """A holds the lease, expires, and B reacquires. Returns (token_a, token_b)."""
        token_a = self.lock_a.acquire(_GRAPH, _THREAD)
        assert token_a is not None
        self.lock_container.clock.now = 61  # service-side expiry (A never renewed)
        token_b = self.lock_b.acquire(_GRAPH, _THREAD)
        assert token_b is not None
        return token_a, token_b


@pytest.fixture
def hosts() -> Iterator[_Hosts]:
    with ExitStack() as stack:
        yield _Hosts(stack)


def test_lock_exclusion_ends_at_expiry_but_stale_graph_can_still_write_checkpoints(
    hosts: _Hosts,
) -> None:
    # Given: A runs a graph and writes a checkpoint under its lease.
    token_a = hosts.lock_a.acquire(_GRAPH, _THREAD)
    assert token_a is not None
    hosts.saver_a.put(
        _config(_THREAD),
        _checkpoint("cp-001", channel_values={"k": "a1"}, channel_versions={"k": "v1"}),
        _metadata(run_id="run-a"),
        {"k": "v1"},
    )
    # When: A's lease expires while its graph is still running, and B takes over.
    hosts.lock_container.clock.now = 61
    token_b = hosts.lock_b.acquire(_GRAPH, _THREAD)
    assert token_b is not None
    hosts.saver_b.put(
        _config(_THREAD, checkpoint_id="cp-001"),
        _checkpoint("cp-002", channel_values={"k": "b"}, channel_versions={"k": "v2"}),
        _metadata(step=2, run_id="run-b"),
        {"k": "v2"},
    )
    # And: A's still-running graph commits a checkpoint after ownership loss.
    hosts.saver_a.put(
        _config(_THREAD, checkpoint_id="cp-001"),
        _checkpoint("cp-002a", channel_values={"k": "stale"}, channel_versions={"k": "v2a"}),
        _metadata(step=2, run_id="run-a"),
        {"k": "v2a"},
    )
    # Then: the lock layer excluded nobody from the saver. The stale write was
    # accepted and both branches now exist under the same parent: a fork.
    stale = hosts.saver_b.get_tuple(_config(_THREAD, checkpoint_id="cp-002a"))
    winner = hosts.saver_b.get_tuple(_config(_THREAD, checkpoint_id="cp-002"))
    assert stale is not None and winner is not None
    assert stale.checkpoint["channel_values"] == {"k": "stale"}
    assert winner.checkpoint["channel_values"] == {"k": "b"}
    assert stale.parent_config == winner.parent_config
    # The monotonic latest hint now points at the stale writer's id ("cp-002a" > "cp-002").
    latest = hosts.saver_b.get_tuple(_config(_THREAD))
    assert latest is not None
    assert latest.checkpoint["id"] == "cp-002a"


def test_same_checkpoint_id_is_last_writer_wins_without_conditional_write(
    hosts: _Hosts,
) -> None:
    # Given: B owns the lock and has committed checkpoint "cp-001".
    hosts.lose_lease_a_and_acquire_b()
    config = _config(_THREAD)
    hosts.saver_b.put(
        config,
        _checkpoint("cp-001", channel_values={"k": "b"}, channel_versions={"k": "v1"}),
        _metadata(run_id="run-b"),
        {"k": "v1"},
    )
    # When: the stale owner writes the same checkpoint id with different content.
    hosts.saver_a.put(
        config,
        _checkpoint("cp-001", channel_values={"k": "stale"}, channel_versions={"k": "v1"}),
        _metadata(run_id="run-a"),
        {"k": "v1"},
    )
    # Then: the saver has no ETag/If-Match fencing; the stale content replaced B's.
    result = hosts.saver_b.get_tuple(_config(_THREAD, checkpoint_id="cp-001"))
    assert result is not None
    assert result.checkpoint["channel_values"] == {"k": "stale"}
    assert result.metadata["run_id"] == "run-a"


def test_stale_pending_writes_are_attached_to_the_new_owners_checkpoint(hosts: _Hosts) -> None:
    # Given: B owns the lock and committed "cp-001".
    hosts.lose_lease_a_and_acquire_b()
    config = _config(_THREAD)
    saved = hosts.saver_b.put(config, _checkpoint("cp-001"), _metadata(run_id="run-b"), {})
    # When: the stale owner stores task writes for that checkpoint.
    hosts.saver_a.put_writes(saved, [("messages", "stale-write")], task_id="task-a")
    # Then: they appear as pending writes of B's checkpoint (no ownership check).
    result = hosts.saver_b.get_tuple(saved)
    assert result is not None
    assert [w[1:] for w in result.pending_writes or []] == [("messages", "stale-write")]


def test_definitive_renewal_loss_marks_lost_and_keeps_key_locally_occupied(
    hosts: _Hosts, caplog: pytest.LogCaptureFixture
) -> None:
    # Given: A lost the lease to B (A's renew now sees a foreign lease).
    token_a, _ = hosts.lose_lease_a_and_acquire_b()
    # When: A's next renewal tick runs.
    with caplog.at_level(logging.WARNING, logger="azure_functions_langgraph.locks.azure_blob"):
        hosts.lock_a._renew_all_once()
    # Then: the entry is lost, kept (blocking local re-acquire), and warned about.
    state = hosts.lock_a._active_leases[(_GRAPH, _THREAD)]
    assert state.lost is True
    assert state.token == token_a
    assert hosts.lock_a.acquire(_GRAPH, _THREAD) is None
    assert any("is lost (definitive lease-loss" in r.getMessage() for r in caplog.records)
    # The only enforcement signal is that log line and the private flag; nothing
    # stops the graph that is still running under token_a.


def test_lost_release_and_close_never_disturb_the_newer_owner(hosts: _Hosts) -> None:
    # Given: A is marked lost and B holds the live lease.
    token_a, _ = hosts.lose_lease_a_and_acquire_b()
    hosts.lock_a._renew_all_once()
    assert hosts.lock_a._active_leases[(_GRAPH, _THREAD)].lost is True
    b_lease = hosts.lease_of(hosts.lock_b)
    # When: A releases with a wrong token, then with its own token, then closes.
    hosts.lock_a.release(_GRAPH, _THREAD, "not-the-owner-token")
    assert (_GRAPH, _THREAD) in hosts.lock_a._active_leases
    hosts.lock_a.release(_GRAPH, _THREAD, token_a)
    hosts.lock_a.close()
    # Then: A's bookkeeping is cleared without a service release, and B still
    # holds the lease, so a third contender is excluded.
    assert (_GRAPH, _THREAD) not in hosts.lock_a._active_leases
    assert b_lease.released is False
    with ExitStack() as stack:
        contender = AzureBlobLeaseThreadLock(container_client=hosts.lock_container)
        stack.callback(contender.close)
        assert contender.acquire(_GRAPH, _THREAD) is None


def test_transient_renewal_misses_mark_lost_only_after_threshold_and_handoff_follows(
    hosts: _Hosts,
) -> None:
    # Given: A's renewals time out (transient) while the service lease lapses.
    token_a = hosts.lock_a.acquire(_GRAPH, _THREAD)
    assert token_a is not None
    hosts.lease_of(hosts.lock_a).renew_failure = True
    key = (_GRAPH, _THREAD)
    # When: ticks fail; the service-side lease expires and B acquires it.
    hosts.lock_a._renew_all_once()
    assert hosts.lock_a._active_leases[key].lost is False
    assert hosts.lock_a._active_leases[key].consecutive_failures == 1
    hosts.lock_container.clock.now = 61
    token_b = hosts.lock_b.acquire(_GRAPH, _THREAD)
    assert token_b is not None
    # Then: before the threshold A still believes it owns the key although B
    # holds the service lease: local bookkeeping lags the service truth.
    assert hosts.lock_a._active_leases[key].lost is False
    for _ in range(10):
        if hosts.lock_a._active_leases[key].lost:
            break
        hosts.lock_a._renew_all_once()
    assert hosts.lock_a._active_leases[key].lost is True
    assert hosts.lock_b._active_leases[key].token == token_b
