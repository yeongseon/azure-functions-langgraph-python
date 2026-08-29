"""ThreadLock protocol — pluggable per-thread lock backend contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ThreadLock(Protocol):
    """Contract for pluggable per-thread lock backends.

    Implementations coordinate concurrent access to a native invoke/stream
    request that targets a specific ``(graph_name, thread_id)`` so that
    single-writer checkpointers (for example
    :class:`~azure_functions_langgraph.checkpointers.azure_blob.AzureBlobCheckpointSaver`)
    never see racing writes for one thread.

    The two shipped implementations are
    :class:`~azure_functions_langgraph.locks.inprocess.InProcessThreadLock`
    (the default; single-process only) and
    :class:`~azure_functions_langgraph.locks.azure_blob.AzureBlobLeaseThreadLock`
    (distributed via Azure Blob lease CAS).

    Third-party backends satisfying this protocol (Redis, Cosmos DB, etc.)
    can be plugged in via :attr:`LangGraphApp.thread_lock`.
    """

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        """Attempt to acquire an exclusive lock for ``(graph_name, thread_id)``.

        Args:
            graph_name: Registered graph name.
            thread_id: Thread ID drawn from ``config.configurable.thread_id``.
            timeout: Maximum seconds to wait for the lock. ``0.0`` (default)
                is non-blocking — matches the pre-existing native-endpoint
                behavior. Positive values block up to ``timeout`` seconds.

        Returns:
            An opaque, non-empty **owner token** if the lock was acquired, or
            ``None`` if it is held elsewhere (in the same process or, for
            distributed backends, on another Function App instance). The token
            is truthy on success and ``None`` on failure, so callers may still
            branch on truthiness. Pass the returned token back to
            :meth:`release` so a stale caller cannot free a lock that has since
            been re-acquired by a different execution.
        """
        ...

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        """Release a lock previously acquired with :meth:`acquire`.

        Args:
            graph_name: Registered graph name.
            thread_id: Thread ID that was locked.
            token: The owner token returned by the matching :meth:`acquire`
                call. If it does not match the currently-held owner (e.g. the
                lock was dropped and re-acquired by a newer execution), release
                is a **no-op** logged at DEBUG — the newer owner is preserved.

        Must be safe to call even if the lock is not currently held by the
        caller — implementations should log at DEBUG level for any
        inconsistency rather than raising, so that the handler ``finally``
        block never masks the underlying request failure.
        """
        ...
