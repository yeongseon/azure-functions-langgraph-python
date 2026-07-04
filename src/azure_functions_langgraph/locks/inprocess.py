"""In-process ThreadLock backend using :class:`threading.Lock`."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class InProcessThreadLock:
    """:class:`threading.Lock`-based per-thread lock scoped to a single worker.

    This is the default backend when :attr:`LangGraphApp.thread_lock` is not
    supplied. It is **not distributed** — locks are held in this Python
    interpreter only and do not coordinate across:

    * Multiple Azure Functions App instances (scale-out)
    * Multiple worker processes on the same instance
    * Warm swaps / cold starts

    Multi-instance production deployments **must** supply a distributed
    backend such as
    :class:`~azure_functions_langgraph.locks.azure_blob.AzureBlobLeaseThreadLock`.

    Thread-safety:
        Safe for concurrent ``acquire`` / ``release`` calls from multiple
        threads. Internal state is guarded by a private
        :class:`threading.Lock`.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> bool:
        """Acquire the lock for ``(graph_name, thread_id)``.

        See :meth:`ThreadLock.acquire` for the general contract.
        """
        with self._guard:
            lock = self._locks.setdefault((graph_name, thread_id), threading.Lock())
        if timeout > 0.0:
            return lock.acquire(blocking=True, timeout=timeout)
        return lock.acquire(blocking=False)

    def release(self, graph_name: str, thread_id: str) -> None:
        """Release the lock for ``(graph_name, thread_id)``.

        See :meth:`ThreadLock.release` for the general contract. Also
        garbage-collects the underlying :class:`threading.Lock` when no
        other request currently holds it, so long-lived workers do not
        grow the internal dict unboundedly.
        """
        key = (graph_name, thread_id)
        with self._guard:
            lock = self._locks.get(key)
        if lock is None:
            logger.debug(
                "release() called for unknown lock key %s/%s; ignoring", graph_name, thread_id
            )
            return
        try:
            lock.release()
        except RuntimeError:
            # Not held (or not held by us). Log and continue so the caller's
            # `finally` block never masks the original exception.
            logger.debug(
                "release() called on unheld lock for %s/%s; ignoring",
                graph_name,
                thread_id,
            )
            return
        # Clean up to prevent unbounded growth in long-lived workers.
        # Re-check under guard: only remove if the lock is not currently held
        # (another request may have acquired it between release and this check).
        with self._guard:
            current = self._locks.get(key)
            if current is lock and not lock.locked():
                self._locks.pop(key, None)
