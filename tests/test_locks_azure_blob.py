"""Tests for AzureBlobLeaseThreadLock — the distributed Blob-lease backend."""

from __future__ import annotations

import sys
import time
import types
from typing import Any, Callable
import warnings

import pytest

pytestmark = pytest.mark.filterwarnings(
    "ignore:.*auto_renew=False.*:UserWarning"
)

# ------------------------------------------------------------------
# Fake azure.storage.blob + azure.core.exceptions used by the tests.
# ------------------------------------------------------------------


class FakeResourceExistsError(Exception):
    """Fake ResourceExistsError raised when upload_blob(overwrite=False) collides."""


class FakeHttpResponseError(Exception):
    """Fake HttpResponseError with error_code / status_code."""

    def __init__(
        self,
        message: str = "",
        *,
        error_code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class MockBlobLease:
    """Minimal mock of BlobLeaseClient returned by acquire_lease()."""

    def __init__(self, blob: "MockBlobClient") -> None:
        self._blob = blob
        self.released = False
        self.renew_count = 0

    def release(self) -> None:
        self.released = True
        self._blob._active_lease = None

    def renew(self) -> None:
        if self.released:
            raise FakeHttpResponseError(
                "LeaseIdMismatchWithLeaseOperation",
                error_code="LeaseIdMismatchWithLeaseOperation",
                status_code=409,
            )
        self.renew_count += 1


class MockBlobClient:
    """Mock of BlobClient — tracks marker existence and current lease."""

    def __init__(self, container: "MockContainerClient", name: str) -> None:
        self._container = container
        self._name = name
        self._active_lease: MockBlobLease | None = None

    def upload_blob(self, data: bytes, overwrite: bool = False) -> None:
        if self._name in self._container.blobs:
            if not overwrite:
                raise FakeResourceExistsError(self._name)
        self._container.blobs[self._name] = data

    def acquire_lease(
        self,
        lease_duration: int,
        lease_id: str | None = None,
    ) -> MockBlobLease:
        if self._name not in self._container.blobs:
            raise FakeHttpResponseError(
                "BlobNotFound",
                error_code="BlobNotFound",
                status_code=404,
            )
        if self._active_lease is not None and not self._active_lease.released:
            raise FakeHttpResponseError(
                "LeaseAlreadyPresent",
                error_code="LeaseAlreadyPresent",
                status_code=409,
            )
        lease = MockBlobLease(self)
        self._active_lease = lease
        return lease


class MockContainerClient:
    """Mock of ContainerClient — stores marker blobs and vends BlobClients."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self._blob_clients: dict[str, MockBlobClient] = {}

    def get_blob_client(self, blob: str) -> MockBlobClient:
        if blob not in self._blob_clients:
            self._blob_clients[blob] = MockBlobClient(self, blob)
        return self._blob_clients[blob]


@pytest.fixture(autouse=True)
def _install_fake_azure_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake azure.storage.blob + azure.core.exceptions per test."""
    # Purge any cached azure.storage.blob module so importlib.import_module in
    # the lock class picks up our fake below.
    for mod in list(sys.modules):
        if mod.startswith("azure.storage.blob") or mod.startswith("azure.core.exceptions"):
            monkeypatch.delitem(sys.modules, mod, raising=False)

    azure_mod = types.ModuleType("azure")
    azure_storage_mod = types.ModuleType("azure.storage")
    azure_blob_mod = types.ModuleType("azure.storage.blob")
    azure_blob_mod.ContainerClient = MockContainerClient  # type: ignore[attr-defined]

    azure_core_mod = types.ModuleType("azure.core")
    azure_core_exceptions_mod = types.ModuleType("azure.core.exceptions")
    azure_core_exceptions_mod.ResourceExistsError = FakeResourceExistsError  # type: ignore[attr-defined]
    azure_core_exceptions_mod.HttpResponseError = FakeHttpResponseError  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "azure", azure_mod)
    monkeypatch.setitem(sys.modules, "azure.storage", azure_storage_mod)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", azure_blob_mod)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core_mod)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions_mod)


def _make_lock(
    container: MockContainerClient | None = None,
    **kwargs: Any,
) -> Any:
    """Instantiate AzureBlobLeaseThreadLock through the lazy re-export."""
    from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

    return AzureBlobLeaseThreadLock(
        container_client=container or MockContainerClient(),
        **kwargs,
    )


# ------------------------------------------------------------------
# Construction & validation
# ------------------------------------------------------------------


class TestConstruction:
    def test_default_construction_ok(self) -> None:
        lock = _make_lock()
        assert lock is not None

    def test_lease_duration_infinite_ok(self) -> None:
        lock = _make_lock(lease_duration=-1)
        assert lock._lease_duration == -1

    @pytest.mark.parametrize("valid", [15, 30, 45, 60])
    def test_lease_duration_finite_ok(self, valid: int) -> None:
        lock = _make_lock(lease_duration=valid)
        assert lock._lease_duration == valid

    @pytest.mark.parametrize("invalid", [0, 1, 14, 61, 120, -2, -100])
    def test_lease_duration_out_of_range_raises(self, invalid: int) -> None:
        with pytest.raises(ValueError, match="lease_duration must be -1"):
            _make_lock(lease_duration=invalid)

    def test_wrong_container_type_raises(self) -> None:
        from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

        with pytest.raises(TypeError, match="ContainerClient"):
            AzureBlobLeaseThreadLock(container_client="not-a-container")  # type: ignore[arg-type]

    def test_missing_azure_storage_blob_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When azure-storage-blob is unavailable, construction raises ImportError."""
        # Simulate ImportError by removing azure.storage.blob and forcing
        # importlib.import_module to raise.
        monkeypatch.delitem(sys.modules, "azure.storage.blob", raising=False)
        real_import_module: Callable[..., Any]
        import importlib as _importlib

        real_import_module = _importlib.import_module

        def _fake_import_module(name: str, package: str | None = None) -> Any:
            if name == "azure.storage.blob":
                raise ImportError("simulated missing azure-storage-blob")
            return real_import_module(name, package)

        monkeypatch.setattr(_importlib, "import_module", _fake_import_module)
        from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

        with pytest.raises(ImportError, match="azure-storage-blob"):
            AzureBlobLeaseThreadLock(container_client=MockContainerClient())


# ------------------------------------------------------------------
# Protocol conformance
# ------------------------------------------------------------------


class TestProtocolConformance:
    def test_satisfies_thread_lock_protocol(self) -> None:
        from azure_functions_langgraph.locks import ThreadLock

        lock = _make_lock()
        assert isinstance(lock, ThreadLock)


# ------------------------------------------------------------------
# Blob-name safety
# ------------------------------------------------------------------


class TestBlobNaming:
    def test_percent_encodes_reserved_chars(self) -> None:
        lock = _make_lock()
        assert lock._blob_name("graph/name", "thread/id") == (
            "thread-locks/graph%2Fname/thread%2Fid"
        )

    def test_percent_encodes_hash_and_query(self) -> None:
        lock = _make_lock()
        assert lock._blob_name("g#1", "t?x") == ("thread-locks/g%231/t%3Fx")

    def test_custom_prefix(self) -> None:
        lock = _make_lock(blob_prefix="custom/prefix/")
        assert lock._blob_name("g", "t").startswith("custom/prefix/")


# ------------------------------------------------------------------
# Acquire / release happy path
# ------------------------------------------------------------------


class TestAcquireRelease:
    def test_first_acquire_returns_true(self) -> None:
        container = MockContainerClient()
        lock = _make_lock(container)
        assert lock.acquire("graph", "t1") is True
        # Marker blob must have been created.
        assert any(name.endswith("t1") for name in container.blobs)
        lock.release("graph", "t1")

    def test_release_frees_lease(self) -> None:
        container = MockContainerClient()
        lock = _make_lock(container)
        lock.acquire("graph", "t1")
        lock.release("graph", "t1")
        # After release, another lock (same container) can acquire.
        lock2 = _make_lock(container)
        assert lock2.acquire("graph", "t1") is True
        lock2.release("graph", "t1")

    def test_local_fast_path_prevents_double_acquire(self) -> None:
        """A second acquire on the same lock instance returns False without hitting Azure."""
        container = MockContainerClient()
        lock = _make_lock(container)
        assert lock.acquire("graph", "t1") is True
        assert lock.acquire("graph", "t1") is False
        lock.release("graph", "t1")

    def test_non_blocking_returns_false_on_conflict(self) -> None:
        """When another instance holds the lease, non-blocking acquire returns False."""
        container = MockContainerClient()
        holder = _make_lock(container)
        holder.acquire("graph", "t1")
        try:
            challenger = _make_lock(container)
            assert challenger.acquire("graph", "t1", timeout=0.0) is False
        finally:
            holder.release("graph", "t1")

    def test_marker_reused_across_acquires(self) -> None:
        """Marker blob is created once, then reused (idempotent)."""
        container = MockContainerClient()
        lock = _make_lock(container)
        lock.acquire("graph", "t1")
        lock.release("graph", "t1")
        # Marker persists after release.
        assert any(name.endswith("t1") for name in container.blobs)
        # Second acquire should not raise despite marker already existing.
        lock.acquire("graph", "t1")
        lock.release("graph", "t1")

    def test_release_unknown_key_is_silent(self) -> None:
        lock = _make_lock()
        # Must not raise.
        lock.release("unknown", "t99")

    def test_release_swallows_azure_errors(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """release() must swallow lease.release() exceptions (log only)."""

        lock = _make_lock()
        lock.acquire("graph", "t1")

        # Sabotage the tracked lease so its .release() raises.
        key = ("graph", "t1")
        lease = lock._active_leases[key]

        def _raise() -> None:
            raise RuntimeError("simulated Azure failure")

        lease.release = _raise

        with caplog.at_level("DEBUG", logger="azure_functions_langgraph.locks.azure_blob"):
            lock.release("graph", "t1")

        assert any("Failed to release blob lease" in rec.getMessage() for rec in caplog.records)
        # Entry should be cleaned up even on failure.
        assert key not in lock._active_leases


# ------------------------------------------------------------------
# Blocking acquire (timeout, polling)
# ------------------------------------------------------------------


class TestBlockingAcquire:
    def test_blocking_acquire_returns_false_after_deadline(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Blocking acquire polls, then returns False when deadline elapses."""
        container = MockContainerClient()
        holder = _make_lock(container)
        holder.acquire("graph", "t1")
        try:
            # Speed up time.sleep so tests remain fast.
            sleep_calls: list[float] = []
            monkeypatch.setattr(
                "azure_functions_langgraph.locks.azure_blob.time.sleep",
                lambda s: sleep_calls.append(s),
            )
            challenger = _make_lock(container)
            start = time.monotonic()
            assert challenger.acquire("graph", "t1", timeout=0.2) is False
            # Should have slept at least once during polling.
            assert len(sleep_calls) >= 1
            # Non-zero elapsed time.
            assert time.monotonic() >= start
        finally:
            holder.release("graph", "t1")

    def test_blocking_acquire_succeeds_when_released_mid_wait(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the lease is released while polling, acquire returns True."""
        container = MockContainerClient()
        holder = _make_lock(container)
        holder.acquire("graph", "t1")

        # Release after the first sleep call so the second acquire_lease succeeds.
        sleep_count = {"n": 0}

        def _fake_sleep(seconds: float) -> None:
            sleep_count["n"] += 1
            if sleep_count["n"] == 1:
                holder.release("graph", "t1")

        monkeypatch.setattr(
            "azure_functions_langgraph.locks.azure_blob.time.sleep",
            _fake_sleep,
        )

        challenger = _make_lock(container)
        assert challenger.acquire("graph", "t1", timeout=1.0) is True
        challenger.release("graph", "t1")


# ------------------------------------------------------------------
# Error propagation
# ------------------------------------------------------------------


class TestErrorPropagation:
    def test_non_lease_http_error_is_re_raised(self) -> None:
        """A 500-class HttpResponseError (not a lease conflict) must propagate."""


        # Force acquire_lease to raise a non-lease-conflict error.
        class BrokenClient(MockContainerClient):
            def get_blob_client(self, blob: str) -> MockBlobClient:
                client = MockBlobClient(self, blob)

                def _boom(lease_duration: int, lease_id: str | None = None) -> Any:
                    raise FakeHttpResponseError(
                        "InternalError",
                        error_code="InternalError",
                        status_code=500,
                    )

                # Ensure marker exists so acquire_lease is actually called.
                client.upload_blob(b"", overwrite=False)
                client.acquire_lease = _boom  # type: ignore[method-assign]
                return client

        broken = BrokenClient()
        lock = _make_lock(broken)
        with pytest.raises(FakeHttpResponseError, match="InternalError"):
            lock.acquire("graph", "t1")

    def test_is_lease_conflict_by_error_code(self) -> None:
        lock = _make_lock()
        assert lock._is_lease_conflict(
            FakeHttpResponseError(error_code="LeaseAlreadyPresent", status_code=409)
        )

    def test_is_lease_conflict_by_status_code(self) -> None:
        lock = _make_lock()
        # No error_code, only status_code=409 → still counts as lease conflict.
        assert lock._is_lease_conflict(FakeHttpResponseError(status_code=409))

    def test_is_lease_conflict_false_for_500(self) -> None:
        lock = _make_lock()
        assert not lock._is_lease_conflict(
            FakeHttpResponseError(error_code="InternalError", status_code=500)
        )



# ------------------------------------------------------------------
# Non-renewal warning
# ------------------------------------------------------------------


class TestFiniteLeaseWarning:
    """AzureBlobLeaseThreadLock emits UserWarning only when auto_renew=False."""

    def test_default_finite_lease_no_warning(self) -> None:
        """Default (auto_renew=True) suppresses the finite-lease warning."""
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            lock = _make_lock()
        try:
            matching = [
                r
                for r in records
                if issubclass(r.category, UserWarning)
                and "auto_renew=False" in str(r.message)
            ]
            assert not matching, (
                "Default (auto_renew=True) must not warn; got "
                f"{[str(r.message) for r in matching]}"
            )
        finally:
            lock.close()

    def test_finite_lease_with_auto_renew_false_warns(self) -> None:
        """auto_renew=False with finite lease emits UserWarning naming the values."""
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            lock = _make_lock(lease_duration=30, auto_renew=False)
        try:
            matching = [
                r
                for r in records
                if issubclass(r.category, UserWarning)
                and "lease_duration=30" in str(r.message)
                and "auto_renew=False" in str(r.message)
            ]
            assert matching, (
                "Expected UserWarning naming lease_duration=30 and "
                f"auto_renew=False; got {[str(r.message) for r in records]}"
            )
        finally:
            lock.close()

    def test_infinite_lease_no_warning_regardless_of_auto_renew(self) -> None:
        """Infinite lease never warns — auto-renewal is meaningless for -1."""
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            lock_a = _make_lock(lease_duration=-1)
            lock_b = _make_lock(lease_duration=-1, auto_renew=False)
        try:
            matching = [
                r
                for r in records
                if issubclass(r.category, UserWarning)
                and "auto_renew" in str(r.message)
            ]
            assert not matching, (
                "Infinite lease must not warn; got "
                f"{[str(r.message) for r in matching]}"
            )
        finally:
            lock_a.close()
            lock_b.close()


# ------------------------------------------------------------------
# Background auto-renewal
# ------------------------------------------------------------------


class TestAutoRenewal:
    """Background auto-renewal for AzureBlobLeaseThreadLock."""

    def test_renewal_thread_starts_by_default(self) -> None:
        """Default construction with finite lease spawns a daemon thread."""
        lock = _make_lock()
        try:
            assert lock._renewal_thread is not None
            assert lock._renewal_thread.is_alive()
            assert lock._renewal_thread.daemon
            assert lock._auto_renew is True
            assert lock._renewal_interval == 60 / 3
        finally:
            lock.close()

    def test_no_renewal_thread_for_infinite_lease(self) -> None:
        """Infinite lease_duration=-1 skips the renewal thread."""
        lock = _make_lock(lease_duration=-1)
        try:
            assert lock._renewal_thread is None
            assert lock._auto_renew is False
        finally:
            lock.close()

    def test_no_renewal_thread_when_auto_renew_false(self) -> None:
        """auto_renew=False skips the renewal thread and emits warning."""
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            lock = _make_lock(lease_duration=30, auto_renew=False)
        try:
            assert lock._renewal_thread is None
            assert lock._auto_renew is False
            assert any(
                issubclass(r.category, UserWarning)
                and "auto_renew=False" in str(r.message)
                for r in records
            )
        finally:
            lock.close()

    def test_renew_all_once_renews_every_active_lease(self) -> None:
        """_renew_all_once() calls .renew() on every tracked lease exactly once."""
        container = MockContainerClient()
        lock = _make_lock(container, auto_renew=False)  # no background thread
        try:
            lock.acquire("graph", "t1")
            lock.acquire("graph", "t2")
            lease1 = lock._active_leases[("graph", "t1")]
            lease2 = lock._active_leases[("graph", "t2")]
            lock._renew_all_once()
            assert lease1.renew_count == 1
            assert lease2.renew_count == 1
            lock._renew_all_once()
            assert lease1.renew_count == 2
            assert lease2.renew_count == 2
        finally:
            lock.close()

    def test_renew_all_once_drops_lease_on_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When renew() raises, the lease is removed from _active_leases."""
        container = MockContainerClient()
        lock = _make_lock(container, auto_renew=False)
        try:
            lock.acquire("graph", "t1")
            lease = lock._active_leases[("graph", "t1")]

            def _raise() -> None:
                raise RuntimeError("simulated renewal failure")

            lease.renew = _raise

            with caplog.at_level(
                "WARNING", logger="azure_functions_langgraph.locks.azure_blob"
            ):
                lock._renew_all_once()

            assert ("graph", "t1") not in lock._active_leases
            assert any(
                "Failed to renew Azure Blob lease" in rec.getMessage()
                for rec in caplog.records
            )
        finally:
            lock.close()

    def test_renew_all_once_skips_stale_lease_on_failure(self) -> None:
        """If lease is concurrently released after snapshot, no KeyError on cleanup."""
        container = MockContainerClient()
        lock = _make_lock(container, auto_renew=False)
        try:
            lock.acquire("graph", "t1")
            lease = lock._active_leases[("graph", "t1")]

            def _release_then_raise() -> None:
                # Simulate a race: another thread releases the lock while we
                # were mid-renew. When our failure handler re-locks, the dict
                # entry is gone and the identity check must skip the del.
                lock.release("graph", "t1")
                raise RuntimeError("simulated failure after concurrent release")

            lease.renew = _release_then_raise
            lock._renew_all_once()  # snapshot has entry; dict is empty after renew()
            assert ("graph", "t1") not in lock._active_leases
        finally:
            lock.close()

    def test_renewal_thread_actually_renews_in_background(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live daemon thread renews leases at the configured interval."""
        # Force sub-second cadence by inflating the renewal fraction so the
        # computed interval (lease_duration / fraction) is small.
        monkeypatch.setattr(
            "azure_functions_langgraph.locks.azure_blob._LEASE_RENEWAL_FRACTION",
            600,
        )
        container = MockContainerClient()
        lock = _make_lock(container, lease_duration=15)
        try:
            assert lock._renewal_interval == pytest.approx(15 / 600)
            lock.acquire("graph", "t1")
            lease = lock._active_leases[("graph", "t1")]
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if lease.renew_count >= 3:
                    break
                time.sleep(0.02)
            assert lease.renew_count >= 3, (
                f"Expected ≥3 renewals, got {lease.renew_count}"
            )
        finally:
            lock.close()

    def test_close_stops_renewal_thread(self) -> None:
        """close() sets shutdown event and joins the renewal thread."""
        lock = _make_lock()
        thread = lock._renewal_thread
        assert thread is not None
        assert thread.is_alive()
        lock.close()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert lock._closed is True

    def test_close_releases_active_leases(self) -> None:
        """close() releases every tracked lease."""
        container = MockContainerClient()
        lock = _make_lock(container)
        lock.acquire("graph", "t1")
        lock.acquire("graph", "t2")
        leases = list(lock._active_leases.values())
        assert len(leases) == 2
        lock.close()
        for lease in leases:
            assert lease.released
        assert not lock._active_leases

    def test_close_idempotent(self) -> None:
        """Repeated close() calls are safe no-ops."""
        lock = _make_lock()
        lock.close()
        lock.close()
        lock.close()  # third call must not raise
        assert lock._closed is True

    def test_close_swallows_release_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """close() swallows exceptions from individual lease.release() calls."""
        container = MockContainerClient()
        lock = _make_lock(container)
        lock.acquire("graph", "t1")
        lease = lock._active_leases[("graph", "t1")]

        def _raise() -> None:
            raise RuntimeError("simulated release failure during close")

        lease.release = _raise
        with caplog.at_level(
            "DEBUG", logger="azure_functions_langgraph.locks.azure_blob"
        ):
            lock.close()  # must not raise
        assert any(
            "during close" in rec.getMessage() for rec in caplog.records
        )