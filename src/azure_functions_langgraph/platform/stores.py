"""Thread storage protocol and in-memory implementation.

The ``ThreadStore`` protocol defines CRUD + search for Platform API
thread metadata plus atomic run-lock transitions for threaded run
execution. Threads are independent of LangGraph checkpoint state and can
exist before any graph execution.

``InMemoryThreadStore`` is the default implementation for development and
single-process deployments.  For production scale-out, implement
``ThreadStore`` against a durable backend (e.g. Azure Table Storage).

.. versionadded:: 0.3.0
"""

from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable
import uuid

from azure_functions_langgraph.platform.contracts import (
    Interrupt,
    Thread,
    ThreadStatus,
)

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ThreadStore(Protocol):
    """Protocol for thread metadata persistence.

    Implementations must be safe for concurrent use from multiple request
    handlers, including atomic run-lock acquisition and release.

    * ``get`` returns ``None`` when the thread does not exist.
    * ``update`` and ``delete`` raise ``KeyError`` for missing threads.
    * ``try_acquire_run_lock`` atomically transitions runnable threads to
      ``busy``.
    * ``release_run_lock`` transitions a held run lock back to a
      terminal thread status.
    * ``search`` filters by exact top-level metadata subset match.
    """

    def create(self, *, metadata: Mapping[str, Any] | None = None) -> Thread:
        """Create a new thread and return it."""
        ...

    def get(self, thread_id: str) -> Thread | None:
        """Return the thread or ``None`` if not found."""
        ...

    def update(
        self,
        thread_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
        values: dict[str, Any] | None = None,
        interrupts: dict[str, list[Interrupt]] | None = None,
        assistant_id: str | None = None,
    ) -> Thread:
        """Partially update a thread.

        Only provided (non-``None``) fields are changed.  Raises
        ``KeyError`` if the thread does not exist.
        """
        ...

    def delete(self, thread_id: str) -> None:
        """Delete a thread.

        Raises ``KeyError`` if the thread does not exist.
        """
        ...

    def try_acquire_run_lock(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
    ) -> Thread | None:
        """Atomically transition status idle/interrupted/error → busy.

        Behavior (in order):
        1. If thread does not exist: raise ``KeyError(thread_id)``.
        2. If thread.assistant_id is set AND assistant_id is provided AND
           they differ: raise ``ValueError`` with a message containing
           both ids.
        3. If thread.status == ``"busy"``: return ``None``.
        4. Otherwise atomically set status=``"busy"``, bind
           assistant_id if the thread had none and one is provided,
           update updated_at, and return the resulting ``Thread``.

        Concurrent calls from multiple processes/instances must be safe:
        exactly one call returns a ``Thread`` and all others return
        ``None`` or raise.
        """
        ...

    def release_run_lock(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, Any] | None = None,
    ) -> Thread:
        """Release a held lock by transitioning to a terminal status.

        ``status`` must be one of ``"idle"``, ``"interrupted"``, or
        ``"error"``. If status == ``"busy"`` this method raises
        ``ValueError``.

        Raises ``KeyError(thread_id)`` if the thread does not exist.
        Implementations may treat this as a best-effort release when used
        by callers.
        """
        ...

    def search(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Thread]:
        """Search threads with optional filters.

        Filters:
        * ``metadata`` — exact top-level key/value subset match.
        * ``status`` — exact status match.

        Results are ordered by ``created_at`` descending (newest first).
        """
        ...

    def count(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
    ) -> int:
        """Count threads matching filters.

        Uses the same filter semantics as ``search``.
        """
        ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryThreadStore:
    """Thread store backed by an in-process dictionary.

    All public methods are thread-safe (guarded by ``threading.RLock``)
    and return deep copies so callers cannot mutate persisted state,
    including atomic run-lock acquire/release operations.

    Parameters
    ----------
    id_factory:
        Callable that returns a new unique thread ID string.
        Defaults to ``lambda: str(uuid.uuid4())``.
    """

    def __init__(
        self,
        *,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._threads: dict[str, Thread] = {}
        self._lock = threading.RLock()
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _deep_copy(self, thread: Thread) -> Thread:
        """Return a deep copy of a Thread to prevent reference leaks."""
        return thread.model_copy(deep=True)

    # -- public API ----------------------------------------------------------

    def create(self, *, metadata: Mapping[str, Any] | None = None) -> Thread:
        """Create a new thread with status ``idle``."""
        with self._lock:
            now = self._now()
            thread_id = self._id_factory()
            if thread_id in self._threads:
                raise ValueError(
                    f"Duplicate thread ID generated: {thread_id!r}"
                )
            thread = Thread(
                thread_id=thread_id,
                created_at=now,
                updated_at=now,
                metadata=dict(metadata) if metadata is not None else None,
                status="idle",
            )
            self._threads[thread_id] = thread
        return self._deep_copy(thread)

    def get(self, thread_id: str) -> Thread | None:
        """Return the thread or ``None`` if not found."""
        with self._lock:
            thread = self._threads.get(thread_id)
            if thread is None:
                return None
            return self._deep_copy(thread)

    def update(
        self,
        thread_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
        values: dict[str, Any] | None = None,
        interrupts: dict[str, list[Interrupt]] | None = None,
        assistant_id: str | None = None,
    ) -> Thread:
        """Partially update a thread.  Raises ``KeyError`` if not found."""
        with self._lock:
            if thread_id not in self._threads:
                raise KeyError(thread_id)

            existing = self._threads[thread_id]
            data = existing.model_dump()
            data["updated_at"] = self._now()

            if metadata is not None:
                data["metadata"] = dict(metadata)
            if status is not None:
                data["status"] = status
            if values is not None:
                data["values"] = values
            if interrupts is not None:
                data["interrupts"] = interrupts
            if assistant_id is not None:
                data["assistant_id"] = assistant_id

            updated = Thread.model_validate(data)
            self._threads[thread_id] = updated
            return self._deep_copy(updated)

    def delete(self, thread_id: str) -> None:
        """Delete a thread.  Raises ``KeyError`` if not found."""
        with self._lock:
            if thread_id not in self._threads:
                raise KeyError(thread_id)
            del self._threads[thread_id]

    def try_acquire_run_lock(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
    ) -> Thread | None:
        """Atomically acquire the per-thread run lock."""
        with self._lock:
            if thread_id not in self._threads:
                raise KeyError(thread_id)
            existing = self._threads[thread_id]
            if (
                existing.assistant_id is not None
                and assistant_id is not None
                and existing.assistant_id != assistant_id
            ):
                raise ValueError(
                    f"Thread {thread_id!r} is bound to assistant "
                    f"{existing.assistant_id!r}, cannot run with {assistant_id!r}"
                )
            if existing.status == "busy":
                return None
            data = existing.model_dump()
            data["status"] = "busy"
            data["updated_at"] = self._now()
            if existing.assistant_id is None and assistant_id is not None:
                data["assistant_id"] = assistant_id
            updated = Thread.model_validate(data)
            self._threads[thread_id] = updated
            return self._deep_copy(updated)

    def release_run_lock(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, Any] | None = None,
    ) -> Thread:
        """Release the per-thread run lock to a terminal status."""
        if status == "busy":
            raise ValueError("release_run_lock cannot set status to 'busy'")
        with self._lock:
            if thread_id not in self._threads:
                raise KeyError(thread_id)
            existing = self._threads[thread_id]
            data = existing.model_dump()
            data["status"] = status
            data["updated_at"] = self._now()
            if values is not None:
                data["values"] = values
            updated = Thread.model_validate(data)
            self._threads[thread_id] = updated
            return self._deep_copy(updated)

    def _filtered_threads(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
    ) -> list[Thread]:
        """Return threads matching filters (unsorted, caller holds lock)."""
        results: list[Thread] = []
        for thread in self._threads.values():
            # Status filter
            if status is not None and thread.status != status:
                continue
            # Metadata subset match
            if metadata is not None:
                if thread.metadata is None:
                    continue
                if not all(
                    k in thread.metadata and thread.metadata[k] == v
                    for k, v in metadata.items()
                ):
                    continue
            results.append(thread)
        return results

    def search(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Thread]:
        """Search threads with optional filters, newest first."""
        if limit < 0:
            raise ValueError(f"limit must be non-negative, got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        with self._lock:
            results = self._filtered_threads(metadata=metadata, status=status)

            # Sort by created_at descending (newest first)
            results.sort(key=lambda t: t.created_at, reverse=True)

            # Apply offset/limit
            page = results[offset : offset + limit]
            return [self._deep_copy(t) for t in page]

    def count(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus | None = None,
    ) -> int:
        """Count threads matching filters.

        Uses the same filter semantics as ``search``.
        """
        with self._lock:
            return len(self._filtered_threads(metadata=metadata, status=status))


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "ThreadStore",
    "InMemoryThreadStore",
]
