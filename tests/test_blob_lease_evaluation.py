"""Controlled two-instance evidence for the #697 renewal decision.

Virtual service seconds model lease expiry, not Azure transport behavior.
The original SDK fakes supply marker handling; these extend lease ownership
with expiry and stale-owner checks instead of sleeping for real lease TTLs.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
import threading

import pytest

from azure_functions_langgraph.locks.azure_blob import AzureBlobLeaseThreadLock
from tests.test_locks_azure_blob import (
    FakeHttpResponseError,
    MockBlobClient,
    MockBlobLease,
    MockContainerClient,
    _install_fake_azure_modules,
)

# Importing the fixture makes the existing fake SDK available to this module.
__all__ = ["_install_fake_azure_modules"]
pytestmark = pytest.mark.filterwarnings("ignore:.*auto_renew=False.*:UserWarning")


@dataclass(slots=True)
class _Clock:
    """Mutable service clock, advanced only by the test controller."""

    now: float = 0.0


class _TimedLease(MockBlobLease):
    def __init__(self, blob: _TimedBlob, duration: int) -> None:
        super().__init__(blob)
        self.blob = blob
        self.duration = duration
        self.expires_at = float("inf") if duration == -1 else blob.clock.now + duration
        self.renew_failure = False
        self.release_failure = False

    def renew(self) -> None:
        if self.renew_failure:
            raise TimeoutError("controlled renewal failure")
        if self.blob._active_lease is not self or self.released:
            raise FakeHttpResponseError(error_code="LeaseLost", status_code=409)
        self.expires_at = self.blob.clock.now + self.duration
        self.renew_count += 1

    def release(self) -> None:
        if self.release_failure:
            raise TimeoutError("controlled release failure")
        if self.blob._active_lease is not self:
            raise FakeHttpResponseError(error_code="LeaseLost", status_code=409)
        super().release()


class _TimedBlob(MockBlobClient):
    def __init__(self, container: _TimedContainer, name: str) -> None:
        super().__init__(container, name)
        self.clock = container.clock

    def acquire_lease(self, lease_duration: int, lease_id: str | None = None) -> _TimedLease:
        active = self._active_lease
        if isinstance(active, _TimedLease) and self.clock.now >= active.expires_at:
            self._active_lease = None
        super().acquire_lease(lease_duration, lease_id)
        lease = _TimedLease(self, lease_duration)
        self._active_lease = lease
        return lease


class _TimedContainer(MockContainerClient):
    def __init__(self) -> None:
        super().__init__()
        self.clock = _Clock()
        self.markers: dict[str, _TimedBlob] = {}

    def get_blob_client(self, blob: str) -> _TimedBlob:
        if blob not in self.markers:
            self.markers[blob] = _TimedBlob(self, blob)
        return self.markers[blob]


@pytest.fixture
def hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[_TimedContainer, AzureBlobLeaseThreadLock, AzureBlobLeaseThreadLock]]:
    """Independent lock bookkeeping over one shared fake service."""
    monkeypatch.setattr(
        AzureBlobLeaseThreadLock, "_renewal_worker", lambda self: self._shutdown_event.wait()
    )
    container = _TimedContainer()
    with ExitStack() as stack:
        first = AzureBlobLeaseThreadLock(container_client=container)
        stack.callback(first.close)
        second = AzureBlobLeaseThreadLock(container_client=container)
        stack.callback(second.close)
        yield container, first, second


def test_renewed_run_excludes_second_host_beyond_60_seconds(
    hosts: tuple[_TimedContainer, AzureBlobLeaseThreadLock, AzureBlobLeaseThreadLock],
) -> None:
    # Given: a run holds the default finite, automatically renewed lease.
    container, first, second = hosts
    token = first.acquire("graph", "thread")
    assert token is not None
    # When: execution continues for three lease lifetimes with healthy ticks.
    for tick in range(1, 10):
        container.clock.now = tick * 20
        first._renew_all_once()
        # Then: independent host bookkeeping cannot acquire the same key.
        assert second.acquire("graph", "thread") is None
    first.release("graph", "thread", token)
    assert second.acquire("graph", "thread") is not None


@pytest.mark.parametrize("renewal_failure", [False, True])
def test_expiry_does_not_fence_original_graph_writes(
    hosts: tuple[_TimedContainer, AzureBlobLeaseThreadLock, AzureBlobLeaseThreadLock],
    renewal_failure: bool,
) -> None:
    # Given: either an opted-out lease or a default lease with failed renewal.
    container, renewed, second = hosts
    with ExitStack() as stack:
        first = renewed
        if not renewal_failure:
            first = AzureBlobLeaseThreadLock(container_client=container, auto_renew=False)
            stack.callback(first.close)
        token = first.acquire("graph", "thread")
        assert token is not None
        lease = container.get_blob_client(first._blob_name("graph", "thread"))._active_lease
        assert isinstance(lease, _TimedLease)
        lease.renew_failure = renewal_failure
        writes: list[str] = []
        finish_graph = threading.Event()

        def graph() -> None:
            assert finish_graph.wait(2)
            writes.append("original still wrote")

        pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        stack.callback(finish_graph.set)
        running_graph = pool.submit(graph)
        # When: the lease expires while the graph still has work to do.
        for tick in range(1, 4):
            container.clock.now = tick * 20
            if renewal_failure:
                first._renew_all_once()
        container.clock.now = 61
        assert first.acquire("graph", "thread") is None
        assert second.acquire("graph", "thread") is not None
        writes.append("new owner wrote")
        finish_graph.set()
        running_graph.result(timeout=2)
        first.release("graph", "thread", token)
        # Then: stale release preserves the new lease, but writes have no fence.
        assert second.acquire("graph", "thread") is None
        assert writes[-2:] == ["new owner wrote", "original still wrote"]


@pytest.mark.parametrize("duration", [60, -1])
def test_failed_close_requires_expiry_or_operator_recovery(
    hosts: tuple[_TimedContainer, AzureBlobLeaseThreadLock, AzureBlobLeaseThreadLock],
    duration: int,
) -> None:
    # Given: a held lease whose service-side release fails during shutdown.
    container, _, second = hosts
    with ExitStack() as stack:
        first = AzureBlobLeaseThreadLock(container_client=container, lease_duration=duration)
        stack.callback(first.close)
        assert first.acquire("graph", "thread") is not None
        blob = container.get_blob_client(first._blob_name("graph", "thread"))
        lease = blob._active_lease
        assert isinstance(lease, _TimedLease)
        lease.release_failure = True
        # When: close is attempted, it cannot guarantee a service release.
        first.close()
        assert second.acquire("graph", "thread") is None
        container.clock.now = 121
        # Then: finite leases recover; infinite leases need an explicit break.
        if duration == -1:
            assert second.acquire("graph", "thread") is None
            blob._active_lease = None
        assert second.acquire("graph", "thread") is not None


def test_acquisition_timeout_is_not_a_graph_deadline(
    hosts: tuple[_TimedContainer, AzureBlobLeaseThreadLock, AzureBlobLeaseThreadLock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: another host already owns the key.
    container, first, second = hosts
    assert first.acquire("graph", "thread") is not None
    monkeypatch.setattr(
        "azure_functions_langgraph.locks.azure_blob.time.monotonic", lambda: container.clock.now
    )
    monkeypatch.setattr(
        "azure_functions_langgraph.locks.azure_blob.time.sleep",
        lambda seconds: setattr(container.clock, "now", container.clock.now + seconds),
    )
    # When: the contender exhausts its acquisition wait budget.
    result = second.acquire("graph", "thread", timeout=1)
    # Then: no ownership is granted and the first graph remains protected.
    assert result is None
    assert 1 <= container.clock.now <= 1.1
    assert first.acquire("graph", "thread") is None


def test_cancel_cannot_stop_sync_graph_or_replace_renewal(
    hosts: tuple[_TimedContainer, AzureBlobLeaseThreadLock, AzureBlobLeaseThreadLock],
) -> None:
    # Given: a synchronous graph is running under the first host's token.
    container, first, second = hosts
    started = threading.Event()
    finish = threading.Event()
    writes: list[str] = []

    def graph() -> None:
        started.set()
        assert finish.wait(2)
        writes.append("completed")

    token = first.acquire("graph", "thread")
    assert token is not None
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(graph)
        try:
            assert started.wait(2)
            # When: cancellation is requested after the graph thread started.
            cancelled = worker.cancel()
            for tick in range(1, 5):
                container.clock.now = tick * 20
                first._renew_all_once()
            # Then: work continues; renewal must still exclude another host.
            assert cancelled is False
            assert second.acquire("graph", "thread") is None
        finally:
            finish.set()
        worker.result(timeout=2)
    first.release("graph", "thread", token)
    assert writes == ["completed"]
    assert second.acquire("graph", "thread") is not None
