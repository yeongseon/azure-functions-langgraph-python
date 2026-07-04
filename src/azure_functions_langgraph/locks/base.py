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

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> bool:
        """Attempt to acquire an exclusive lock for ``(graph_name, thread_id)``.

        Args:
            graph_name: Registered graph name.
            thread_id: Thread ID drawn from ``config.configurable.thread_id``.
            timeout: Maximum seconds to wait for the lock. ``0.0`` (default)
                is non-blocking — matches the pre-existing native-endpoint
                behavior. Positive values block up to ``timeout`` seconds.

        Returns:
            ``True`` if the lock was acquired, ``False`` if it is held
            elsewhere (in the same process or, for distributed backends, on
            another Function App instance).
        """
        ...

    def release(self, graph_name: str, thread_id: str) -> None:
        """Release a previously acquired lock.

        Must be safe to call even if the lock is not currently held by the
        caller — implementations should log at DEBUG level for any
        inconsistency rather than raising, so that the handler ``finally``
        block never masks the underlying request failure.
        """
        ...
