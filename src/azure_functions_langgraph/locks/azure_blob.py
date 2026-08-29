"""Azure Blob lease-backed distributed ThreadLock.

Uses the Azure Blob Storage lease API to coordinate a per-thread lock across
multiple Azure Functions instances. Each ``(graph_name, thread_id)`` maps to
a marker blob; acquiring the lock means holding an exclusive lease on that
blob. Releasing the lock releases the lease.

Lease semantics recap (see the Azure Blob REST reference for details):

* A blob can have at most one active lease at any time.
* Leases can be finite (15-60 seconds) or infinite (``-1``).
* Attempting to acquire a held lease returns ``409 LeaseAlreadyPresent``.
* This class renews leases in the background by default: a per-instance
  daemon thread renews every active lease at ``lease_duration / 3``
  intervals until :meth:`close` (or process exit). Pass
  ``auto_renew=False`` to opt out — a finite lease then behaves as it
  did before renewal support: it silently expires mid-execution and
  lets another instance acquire the same ``(graph_name, thread_id)``
  lock, and construction emits a :class:`UserWarning` to make the
  trade-off visible.
* Infinite leases (``lease_duration=-1``) never need renewal but must
  be broken manually if a host crashes.

Renewal resilience:

* A single *transient* renewal failure (network blip, timeout,
  throttling, 5xx) does **not** drop the lease from local tracking. The
  Azure lease is still valid for roughly two more renewal intervals, so
  the daemon retries on the next tick. Only after
  :data:`_MAX_CONSECUTIVE_RENEWAL_FAILURES` consecutive transient
  failures is the lease treated as lost.
* A *definitive* lease-loss error (the lease id no longer matches, the
  lease is gone, or a ``412`` precondition failure) marks the local
  entry ``lost`` immediately — but the entry is **kept** locally
  occupied until :meth:`release` runs, so the same process cannot
  reacquire the key while its original graph execution may still be
  running.

.. warning::
    This lock is best-effort under mid-run lease loss. If a lease is
    genuinely lost while a graph execution is still running, this lock
    alone cannot prevent another Function App instance from acquiring
    the same ``(graph_name, thread_id)`` and writing concurrently —
    preventing that would require cancelling the in-flight execution or
    fencing the checkpoint writes, neither of which this alpha lock does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import logging
import secrets
import threading
import time
from typing import Any, Protocol, cast
from urllib.parse import quote
import warnings

logger = logging.getLogger(__name__)


class _BlobLeaseClientProtocol(Protocol):
    def release(self) -> None: ...
    def renew(self) -> None: ...


class _BlobClientProtocol(Protocol):
    def acquire_lease(
        self, lease_duration: int, lease_id: str | None = ...
    ) -> _BlobLeaseClientProtocol: ...

    def upload_blob(self, data: bytes, overwrite: bool = ...) -> Any: ...


class _ContainerClientProtocol(Protocol):
    def get_blob_client(self, blob: str) -> _BlobClientProtocol: ...


# Lease duration constants (Azure Blob lease API limits).
_LEASE_DURATION_MIN = 15
_LEASE_DURATION_MAX = 60
_LEASE_DURATION_INFINITE = -1

# Polling backoff for blocking acquire when a lease is already held elsewhere.
_POLL_INTERVAL_MIN = 0.05
_POLL_INTERVAL_MAX = 0.5

# Renewal cadence: renew each active lease this fraction of lease_duration
# before expiry. 1/3 means the Azure lease stays valid for roughly two more
# renewal ticks after a single failed renewal call, so a transient failure can
# be retried on the next tick instead of abandoning the lease immediately.
_LEASE_RENEWAL_FRACTION = 3
# How many *consecutive* transient renewal failures may occur before the lease
# is marked ``lost``. With the 1/3 cadence above, the Azure lease has typically
# already expired by the time this many consecutive renewals have failed, so
# continuing to retry buys nothing. Note the key stays locally occupied even
# once marked ``lost`` — it is only removed from tracking by ``release()`` — so a
# lost lease never silently frees the slot for a concurrent in-process caller.
_MAX_CONSECUTIVE_RENEWAL_FAILURES = 3
# Deadline for join() when close() stops the renewal thread. Bounded so a
# stuck Azure call cannot indefinitely block interpreter shutdown.
_RENEWAL_SHUTDOWN_TIMEOUT = 5.0

# Azure error codes that mean the lease is *definitively* gone (as opposed to a
# transient network/service failure that should be retried on the next tick).
_DEFINITIVE_LEASE_LOSS_ERROR_CODES = frozenset(
    code.lower()
    for code in (
        "LeaseLost",
        "LeaseIdMismatchWithLeaseOperation",
        "LeaseNotPresentWithLeaseOperation",
        "LeaseIdMissing",
    )
)


@dataclass
class _LeaseState:
    """Mutable per-key tracking record for one held Azure Blob lease.

    Attributes:
        lease: The Azure ``BlobLeaseClient`` (or protocol-compatible stub).
        token: Opaque owner token returned by :meth:`acquire` and required by
            :meth:`release`, so a stale caller cannot free a lease that has
            since been re-acquired under the same key by a newer execution.
        consecutive_failures: Count of consecutive *transient* renewal
            failures since the last successful renewal. Reset to ``0`` on
            every successful renewal.
        last_successful_renewal: ``time.monotonic()`` timestamp of the last
            successful ``renew()`` (or of acquisition, before the first
            renewal).
        lost: ``True`` once the lease is known to be gone (definitive loss or
            too many consecutive transient failures). A lost entry is kept
            locally occupied so the same process does not reacquire the key
            until :meth:`release` clears it.
        last_error: The most recent renewal exception, for diagnostics.
    """

    lease: _BlobLeaseClientProtocol
    token: str = field(default_factory=lambda: secrets.token_hex(16))
    consecutive_failures: int = 0
    last_successful_renewal: float = field(default_factory=time.monotonic)
    lost: bool = False
    last_error: BaseException | None = None


class AzureBlobLeaseThreadLock:
    """Distributed per-thread lock backed by Azure Blob leases.

    Coordinates ``(graph_name, thread_id)`` locking across multiple Azure
    Functions instances by holding an exclusive lease on a marker blob per
    thread. Any :class:`~azure.storage.blob.ContainerClient` will do — the
    same container as the ``AzureBlobCheckpointSaver`` is a natural fit but
    a dedicated container is fine too.

    .. warning::
        By default this class renews Azure Blob leases in the background
        (a per-instance daemon thread renews every active lease at
        ``lease_duration / 3`` intervals). Pass ``auto_renew=False`` to
        opt out — construction then emits a :class:`UserWarning` because
        a finite ``lease_duration`` can silently expire mid-execution and
        let another instance acquire the same ``(graph_name, thread_id)``
        lock, allowing concurrent writes to single-writer checkpointers.
        Call :meth:`close` for graceful shutdown of the renewal thread
        when the lock instance is no longer needed; Azure Functions
        workers do not typically need this because the daemon thread
        dies when the interpreter exits.

    Example:
        >>> from azure.storage.blob import ContainerClient
        >>> from azure_functions_langgraph import LangGraphApp
        >>> from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock
        >>>
        >>> container = ContainerClient.from_connection_string(conn, "thread-locks")
        >>> if not container.exists():
        ...     container.create_container()
        >>> lock = AzureBlobLeaseThreadLock(container_client=container)
        >>> app = LangGraphApp(thread_lock=lock)

    Args:
        container_client: An ``azure.storage.blob.ContainerClient`` bound to
            the container where marker blobs will live. The container must
            already exist — this class never creates it (that decision
            belongs to app-level infrastructure code).
        lease_duration: Lease length in seconds. Must be 15-60 (finite) or
            ``-1`` (infinite). Defaults to 60. With ``auto_renew=True``
            (the default), a background daemon thread renews every active
            lease at ``lease_duration / 3`` intervals, so execution time
            is no longer bounded by the lease. Set ``auto_renew=False``
            to disable renewal — a finite lease then silently expires
            mid-execution and lets another instance acquire the same
            lock. Finite leases also auto-expire on the service if
            :meth:`release` never runs (host crash, scale-in), giving you
            a crash-recovery mechanism. Infinite leases require an
            operator to break them manually when a host crashes.
        blob_prefix: Prefix applied to every marker blob so lock blobs are
            visually grouped inside the container. Defaults to
            ``"thread-locks/"``.
        auto_renew: If ``True`` (default), start a per-instance daemon
            thread that renews every active lease at
            ``lease_duration / 3`` intervals until :meth:`close` (or
            process exit). If ``False``, no renewal happens and
            construction emits a :class:`UserWarning` when
            ``lease_duration`` is finite, since finite leases will
            silently expire mid-execution. Ignored for
            ``lease_duration=-1`` (infinite leases are not renewable).

    Thread-safety:
        Safe for concurrent ``acquire`` / ``release`` calls from multiple
        threads. Only one thread in this process can hold a given lease at
        a time (the Azure API enforces this globally); this class enforces
        it locally by returning ``False`` from :meth:`acquire` when a lease
        is already tracked for the key.
    """

    def __init__(
        self,
        *,
        container_client: _ContainerClientProtocol,
        lease_duration: int = _LEASE_DURATION_MAX,
        blob_prefix: str = "thread-locks/",
        auto_renew: bool = True,
    ) -> None:
        if lease_duration != _LEASE_DURATION_INFINITE and not (
            _LEASE_DURATION_MIN <= lease_duration <= _LEASE_DURATION_MAX
        ):
            raise ValueError(
                f"lease_duration must be -1 (infinite) or between "
                f"{_LEASE_DURATION_MIN} and {_LEASE_DURATION_MAX} seconds; got {lease_duration}"
            )

        try:
            azure_blob_module = importlib.import_module("azure.storage.blob")
        except ImportError as exc:
            raise ImportError(
                "AzureBlobLeaseThreadLock requires optional dependency "
                "'azure-storage-blob'. Install with: "
                "pip install azure-functions-langgraph[azure-blob]"
            ) from exc

        azure_container_client = getattr(azure_blob_module, "ContainerClient", None)
        if azure_container_client is None or not isinstance(
            container_client, azure_container_client
        ):
            raise TypeError(
                "container_client must be an instance of azure.storage.blob.ContainerClient"
            )

        try:
            azure_core_exceptions = importlib.import_module("azure.core.exceptions")
        except (
            ImportError
        ) as exc:  # pragma: no cover - defensive; installed with azure-storage-blob
            raise ImportError(
                "AzureBlobLeaseThreadLock requires 'azure-core'. "
                "Install with: pip install azure-functions-langgraph[azure-blob]"
            ) from exc
        resource_exists_error = getattr(azure_core_exceptions, "ResourceExistsError", None)
        http_response_error = getattr(azure_core_exceptions, "HttpResponseError", None)
        if resource_exists_error is None or http_response_error is None:
            raise ImportError(  # pragma: no cover - defensive
                "azure.core.exceptions is missing ResourceExistsError or HttpResponseError; "
                "azure-core installation may be corrupt."
            )

        self._container_client: _ContainerClientProtocol = cast(
            _ContainerClientProtocol, container_client
        )
        self._lease_duration = lease_duration
        self._prefix = blob_prefix
        self._resource_exists_error: type[BaseException] = cast(
            type[BaseException], resource_exists_error
        )
        self._http_response_error: type[BaseException] = cast(
            type[BaseException], http_response_error
        )
        self._active_leases: dict[tuple[str, str], _LeaseState] = {}
        self._active_leases_guard = threading.Lock()

        self._auto_renew: bool = auto_renew and lease_duration != _LEASE_DURATION_INFINITE
        self._closed: bool = False
        self._shutdown_event: threading.Event = threading.Event()
        self._renewal_thread: threading.Thread | None = None
        self._renewal_interval: float = 0.0

        if lease_duration != _LEASE_DURATION_INFINITE and not auto_renew:
            warnings.warn(
                f"AzureBlobLeaseThreadLock(lease_duration={lease_duration}, "
                "auto_renew=False) is finite and auto-renewal is disabled. "
                "If a graph execution exceeds lease_duration seconds, the "
                "lease will silently expire mid-execution and another "
                "instance may acquire the same (graph_name, thread_id) "
                "lock, allowing concurrent writes to single-writer "
                "checkpointers. Enable auto_renew=True (the default) or "
                "pass lease_duration=-1 (infinite) whenever graph "
                "execution can exceed 60 seconds.",
                UserWarning,
                stacklevel=2,
            )

        if self._auto_renew:
            self._renewal_interval = lease_duration / _LEASE_RENEWAL_FRACTION
            self._renewal_thread = threading.Thread(
                target=self._renewal_worker,
                name=f"azblob-lease-renew-{id(self):x}",
                daemon=True,
            )
            self._renewal_thread.start()

    def _blob_name(self, graph_name: str, thread_id: str) -> str:
        """Return the URL-safe blob path for ``(graph_name, thread_id)``."""
        # ``safe=""`` percent-encodes every reserved char, so graph names or
        # thread IDs containing ``/``, ``?``, ``#`` etc. cannot escape the
        # marker prefix and clash with unrelated lock blobs.
        return f"{self._prefix}{quote(graph_name, safe='')}/{quote(thread_id, safe='')}"

    def _ensure_marker(self, blob_client: _BlobClientProtocol) -> None:
        """Idempotently create the marker blob so it can be leased."""
        try:
            blob_client.upload_blob(b"", overwrite=False)
        except self._resource_exists_error:
            # Expected on every acquire after the first — the marker blob is
            # created once and reused for every subsequent lease attempt.
            return

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        """Attempt to hold an Azure Blob lease for ``(graph_name, thread_id)``.

        Semantics match :meth:`ThreadLock.acquire`:

        * ``timeout=0.0`` — non-blocking. Returns immediately.
        * ``timeout>0.0`` — polls the Azure API with jittered backoff until
          the lease is acquired or the deadline expires.

        A key that is already tracked locally — **including** one whose lease
        has been marked ``lost`` but not yet released — returns ``None``
        immediately without hitting Azure, so the original execution retains
        exclusive local ownership until it releases.
        """
        key = (graph_name, thread_id)
        # Fast local check — do not hammer Azure if we already track a lease
        # (a lost-but-unreleased entry still counts as occupied).
        with self._active_leases_guard:
            if key in self._active_leases:
                return None

        blob_client = self._container_client.get_blob_client(self._blob_name(graph_name, thread_id))
        self._ensure_marker(blob_client)

        deadline = time.monotonic() + timeout if timeout > 0.0 else 0.0
        while True:
            try:
                lease = blob_client.acquire_lease(lease_duration=self._lease_duration)
            except self._http_response_error as exc:
                if not self._is_lease_conflict(exc):
                    raise
                if timeout <= 0.0 or time.monotonic() >= deadline:
                    return None
                remaining = deadline - time.monotonic()
                time.sleep(min(_POLL_INTERVAL_MAX, max(_POLL_INTERVAL_MIN, remaining / 2)))
                continue

            with self._active_leases_guard:
                # Concurrent local acquire may have won the race — release the
                # lease we just took and report failure so callers stay
                # consistent with the fast-path check above.
                if key in self._active_leases:
                    try:
                        lease.release()
                    except Exception:  # pragma: no cover - defensive
                        logger.debug(
                            "Failed to release race-loser lease for %s/%s",
                            graph_name,
                            thread_id,
                            exc_info=True,
                        )
                    return None
                state = _LeaseState(lease=lease)
                self._active_leases[key] = state
            return state.token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        """Release the Azure Blob lease for ``(graph_name, thread_id)``.

        Best-effort — never raises. If ``token`` does not match the owner
        token of the currently-tracked lease (e.g. the key was dropped and
        re-acquired by a newer execution), release is a no-op logged at DEBUG
        so the newer owner is preserved. If the tracked lease was already
        marked ``lost`` (definitive loss or exhausted renewals), the local
        entry is simply cleared without calling ``lease.release()`` because
        the lease is gone. Otherwise the lease is released best-effort;
        failures during release are logged at DEBUG and left for lease expiry
        (or manual break) to recover.
        """
        key = (graph_name, thread_id)
        with self._active_leases_guard:
            state = self._active_leases.get(key)
            if state is None:
                logger.debug(
                    "release() called for unknown lease key %s/%s; ignoring",
                    graph_name,
                    thread_id,
                )
                return
            if state.token != token:
                logger.debug(
                    "release() token mismatch for %s/%s; lease is held by a "
                    "newer owner, ignoring stale release",
                    graph_name,
                    thread_id,
                )
                return
            self._active_leases.pop(key, None)
        if state.lost:
            # Lease already gone — nothing to release on the service side.
            logger.debug(
                "release() clearing lost lease entry for %s/%s (no service release)",
                graph_name,
                thread_id,
            )
            return
        try:
            state.lease.release()
        except Exception:
            logger.debug(
                "Failed to release blob lease for %s/%s; will expire naturally",
                graph_name,
                thread_id,
                exc_info=True,
            )

    def _renew_all_once(self) -> None:
        """Renew every currently tracked lease exactly once.

        Safe to call from any thread. Renewal failures are classified:

        * **Definitive lease-loss** (see :meth:`_is_definitive_lease_loss`):
          the entry is marked ``lost`` but kept locally occupied so the same
          process cannot reacquire the key until :meth:`release` runs.
        * **Transient failure** (anything else): the entry is kept and its
          ``consecutive_failures`` counter is incremented so the next tick
          retries. Only after
          :data:`_MAX_CONSECUTIVE_RENEWAL_FAILURES` consecutive transient
          failures is the entry marked ``lost``.

        A successful renewal resets ``consecutive_failures`` to ``0``.
        """
        with self._active_leases_guard:
            snapshot = list(self._active_leases.items())
        for key, state in snapshot:
            if state.lost:
                # Already known gone; leave it for release() to clear.
                continue
            try:
                state.lease.renew()
            except Exception as exc:  # noqa: BLE001 - classified below
                self._handle_renew_failure(key, state, exc)
            else:
                with self._active_leases_guard:
                    if self._active_leases.get(key) is state:
                        state.consecutive_failures = 0
                        state.last_successful_renewal = time.monotonic()
                        state.last_error = None

    def _handle_renew_failure(
        self, key: tuple[str, str], state: _LeaseState, exc: BaseException
    ) -> None:
        """Apply the transient-vs-definitive policy for one failed renewal."""
        definitive = self._is_definitive_lease_loss(exc)
        with self._active_leases_guard:
            # A concurrent release() may have swapped/removed the entry; only
            # act if this is still the same state object.
            if self._active_leases.get(key) is not state:
                return
            state.last_error = exc
            if definitive:
                state.lost = True
                logger.warning(
                    "Azure Blob lease for %s/%s is lost (definitive lease-loss "
                    "on renew); keeping the key locally occupied until release. "
                    "Another instance may now acquire this lock.",
                    key[0],
                    key[1],
                    exc_info=True,
                )
                return
            state.consecutive_failures += 1
            if state.consecutive_failures >= _MAX_CONSECUTIVE_RENEWAL_FAILURES:
                state.lost = True
                logger.warning(
                    "Azure Blob lease for %s/%s failed renewal %d consecutive "
                    "times; treating as lost and keeping the key locally "
                    "occupied until release. Another instance may now acquire "
                    "this lock.",
                    key[0],
                    key[1],
                    state.consecutive_failures,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "Transient failure renewing Azure Blob lease for %s/%s "
                    "(%d/%d consecutive); will retry on the next renewal tick.",
                    key[0],
                    key[1],
                    state.consecutive_failures,
                    _MAX_CONSECUTIVE_RENEWAL_FAILURES,
                    exc_info=True,
                )

    def _renewal_worker(self) -> None:
        """Daemon loop: call :meth:`_renew_all_once` every renewal interval."""
        while not self._shutdown_event.wait(timeout=self._renewal_interval):
            self._renew_all_once()

    def close(self) -> None:
        """Stop the renewal thread and release every active lease.

        Idempotent and safe to call from any thread. After :meth:`close`,
        further :meth:`acquire` calls still work but will not be
        auto-renewed even if ``auto_renew=True`` was passed to the
        constructor. Entries already marked ``lost`` are dropped without a
        service-side release (their lease is gone).
        """
        if self._closed:
            return
        self._closed = True
        self._shutdown_event.set()
        thread = self._renewal_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_RENEWAL_SHUTDOWN_TIMEOUT)
        with self._active_leases_guard:
            remaining = list(self._active_leases.items())
            self._active_leases.clear()
        for key, state in remaining:
            if state.lost:
                continue
            try:
                state.lease.release()
            except Exception:
                logger.debug(
                    "Failed to release blob lease for %s/%s during close",
                    key[0],
                    key[1],
                    exc_info=True,
                )

    def __del__(self) -> None:  # pragma: no cover - GC timing not deterministic
        try:
            self.close()
        except Exception:  # nosec B110 - GC cleanup; nothing actionable if close() fails during interpreter shutdown
            pass

    def _is_lease_conflict(self, exc: BaseException) -> bool:
        """Return True if *exc* is a lease-already-present conflict."""
        # Azure returns 409 for lease conflicts, with error_code=LeaseAlreadyPresent
        # or LeaseIdMissing. Prefer error_code when populated; fall back to status.
        error_code = getattr(exc, "error_code", None)
        if isinstance(error_code, str) and error_code.lower().startswith("lease"):
            return True
        status_code = getattr(exc, "status_code", None)
        return status_code == 409

    def _is_definitive_lease_loss(self, exc: BaseException) -> bool:
        """Return True if *exc* means the lease is *definitively* gone.

        Distinguishes a genuine lease-loss (the lease id no longer matches,
        the lease is not present, or a ``412`` precondition failure) from a
        transient service/network error that should be retried on the next
        renewal tick. Prefers the Azure ``error_code`` allowlist; falls back
        to a ``412 Precondition Failed`` status, which the Blob lease API
        returns when the lease id is no longer valid.
        """
        error_code = getattr(exc, "error_code", None)
        if isinstance(error_code, str) and error_code.lower() in _DEFINITIVE_LEASE_LOSS_ERROR_CODES:
            return True
        # 412 Precondition Failed on a renew means the lease id is no longer
        # valid (lease broken/changed). 409/5xx/timeouts are treated as
        # transient and retried.
        status_code = getattr(exc, "status_code", None)
        return status_code == 412
