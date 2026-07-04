"""Azure Blob lease-backed distributed ThreadLock.

Uses the Azure Blob Storage lease API to coordinate a per-thread lock across
multiple Azure Functions instances. Each ``(graph_name, thread_id)`` maps to
a marker blob; acquiring the lock means holding an exclusive lease on that
blob. Releasing the lock releases the lease.

Lease semantics recap (see the Azure Blob REST reference for details):

* A blob can have at most one active lease at any time.
* Leases can be finite (15-60 seconds) or infinite (``-1``).
* Attempting to acquire a held lease returns ``409 LeaseAlreadyPresent``.
* **This class never renews leases** — there is no background renewal
  task. A finite lease that outlives its ``lease_duration`` silently
  releases mid-execution and lets another instance acquire the same
  lock, so pick ``lease_duration`` comfortably above your longest
  expected graph execution time, or use ``-1`` (infinite) and rely on
  :meth:`release` running in the request ``finally`` block.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any, Protocol, cast
from urllib.parse import quote
import warnings

logger = logging.getLogger(__name__)


class _BlobLeaseClientProtocol(Protocol):
    def release(self) -> None: ...


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


class AzureBlobLeaseThreadLock:
    """Distributed per-thread lock backed by Azure Blob leases.

    Coordinates ``(graph_name, thread_id)`` locking across multiple Azure
    Functions instances by holding an exclusive lease on a marker blob per
    thread. Any :class:`~azure.storage.blob.ContainerClient` will do — the
    same container as the ``AzureBlobCheckpointSaver`` is a natural fit but
    a dedicated container is fine too.

    .. warning::
        This class does **not** renew Azure Blob leases in the background.
        If a graph execution exceeds ``lease_duration`` seconds (default 60,
        the maximum finite value Azure allows), the lease silently expires
        and another instance can acquire the same ``(graph_name, thread_id)``
        lock, allowing concurrent writes to single-writer checkpointers.
        Pass ``lease_duration=-1`` (infinite) whenever graph execution can
        exceed 60 seconds. Construction emits a :class:`UserWarning` for
        finite ``lease_duration`` to make the trade-off visible in test and
        CI output. Auto-renewal is tracked as a future enhancement.

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
            ``-1`` (infinite). Defaults to 60. **Because this class does
            not renew leases in the background**, a finite ``lease_duration``
            bounds the maximum safe graph execution time — pick a value
            comfortably above your longest expected execution or use ``-1``
            (infinite). Finite leases also auto-expire on the service if
            :meth:`release` never runs (host crash, scale-in), giving you a
            crash-recovery mechanism at the cost of the mid-execution race
            above. Infinite leases require an operator to break them
            manually when a host crashes. Construction emits a
            :class:`UserWarning` for finite ``lease_duration`` to make the
            trade-off visible in test and CI output.
        blob_prefix: Prefix applied to every marker blob so lock blobs are
            visually grouped inside the container. Defaults to
            ``"thread-locks/"``.

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
        self._active_leases: dict[tuple[str, str], _BlobLeaseClientProtocol] = {}
        self._active_leases_guard = threading.Lock()

        if lease_duration != _LEASE_DURATION_INFINITE:
            warnings.warn(
                f"AzureBlobLeaseThreadLock(lease_duration={lease_duration}) is "
                "finite and this class does not renew leases in the background. "
                "If a graph execution exceeds lease_duration seconds, the lease "
                "will silently expire mid-execution and another instance may "
                "acquire the same (graph_name, thread_id) lock, allowing "
                "concurrent writes to single-writer checkpointers. Pass "
                "lease_duration=-1 (infinite) whenever graph execution can "
                "exceed 60 seconds.",
                UserWarning,
                stacklevel=2,
            )

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

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> bool:
        """Attempt to hold an Azure Blob lease for ``(graph_name, thread_id)``.

        Semantics match :meth:`ThreadLock.acquire`:

        * ``timeout=0.0`` — non-blocking. Returns immediately.
        * ``timeout>0.0`` — polls the Azure API with jittered backoff until
          the lease is acquired or the deadline expires.
        """
        key = (graph_name, thread_id)
        # Fast local check — do not hammer Azure if we already track a lease.
        with self._active_leases_guard:
            if key in self._active_leases:
                return False

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
                    return False
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
                    return False
                self._active_leases[key] = lease
            return True

    def release(self, graph_name: str, thread_id: str) -> None:
        """Release the Azure Blob lease for ``(graph_name, thread_id)``.

        Best-effort — never raises. Failures during release are logged at
        DEBUG and left for lease expiry (or manual break) to recover.
        """
        key = (graph_name, thread_id)
        with self._active_leases_guard:
            lease = self._active_leases.pop(key, None)
        if lease is None:
            logger.debug(
                "release() called for unknown lease key %s/%s; ignoring", graph_name, thread_id
            )
            return
        try:
            lease.release()
        except Exception:
            logger.debug(
                "Failed to release blob lease for %s/%s; will expire naturally",
                graph_name,
                thread_id,
                exc_info=True,
            )

    def _is_lease_conflict(self, exc: BaseException) -> bool:
        """Return True if *exc* is a lease-already-present conflict."""
        # Azure returns 409 for lease conflicts, with error_code=LeaseAlreadyPresent
        # or LeaseIdMissing. Prefer error_code when populated; fall back to status.
        error_code = getattr(exc, "error_code", None)
        if isinstance(error_code, str) and error_code.lower().startswith("lease"):
            return True
        status_code = getattr(exc, "status_code", None)
        return status_code == 409
