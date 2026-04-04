"""Thread storage protocol and in-memory implementation.

The ``ThreadStore`` protocol defines CRUD + search for Platform API
thread metadata.  Threads are independent of LangGraph checkpoint state
and can exist before any graph execution.

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
    handlers.

    * ``get`` returns ``None`` when the thread does not exist.
    * ``update`` and ``delete`` raise ``KeyError`` for missing threads.
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


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryThreadStore:
    """Thread store backed by an in-process dictionary.

    All public methods are thread-safe (guarded by ``threading.RLock``)
    and return deep copies so callers cannot mutate persisted state.

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

            updated = Thread.model_validate(data)
            self._threads[thread_id] = updated
            return self._deep_copy(updated)

    def delete(self, thread_id: str) -> None:
        """Delete a thread.  Raises ``KeyError`` if not found."""
        with self._lock:
            if thread_id not in self._threads:
                raise KeyError(thread_id)
            del self._threads[thread_id]

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

            # Sort by created_at descending (newest first)
            results.sort(key=lambda t: t.created_at, reverse=True)

            # Apply offset/limit
            page = results[offset : offset + limit]
            return [self._deep_copy(t) for t in page]


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "ThreadStore",
    "InMemoryThreadStore",
]
