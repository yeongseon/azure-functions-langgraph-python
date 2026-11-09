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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import threading
from typing import Any, Protocol, runtime_checkable
import uuid

from azure_functions_langgraph.platform.contracts import (
    Interrupt,
    Thread,
    ThreadStatus,
)

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunLockReleaseOutcome:
    """What a release did: the thread as it stands afterwards, and whether this call applied it."""

    thread: Thread
    applied: bool


@runtime_checkable
class ReleaseOutcomeProvider(Protocol):
    """Optional :class:`ThreadStore` capability: report whether a release was applied.

    With an owner token, ``release_run_lock`` treats a stale release (the token no longer
    owns a ``busy`` thread) as a no-op and returns the unchanged current thread. A caller
    can compare that thread with what it asked for, but the comparison cannot tell an
    applied release from a stale no-op when another owner has already left the thread at
    the requested status, for example because it finished first and went ``idle``. A store
    that also implements this protocol answers exactly: ``applied`` is true only when this
    call performed the release.

    Platform run handlers use the answer when the store provides it and fall back to the
    comparison otherwise, so stores without this capability keep working. Implementations
    must keep it consistent with ``release_run_lock``: that method's return value is this
    method's ``thread``.
    """

    def release_run_lock_outcome(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> RunLockReleaseOutcome:
        """Release exactly like ``release_run_lock`` and report whether it was applied.

        ``applied`` is false for a stale release (a token that no longer owns a ``busy``
        thread), in which case ``thread`` is the unchanged current thread. A release
        without an owner token is always applied. Raises ``KeyError(thread_id)`` if the
        thread does not exist and ``ValueError`` for status ``"busy"``.
        """
        ...


class _ReleaseSignal:
    """Carries a store's own "was this release applied" answer back to its outcome method.

    ``release_run_lock`` keeps its public contract (it returns only the thread) and a
    subclass or a test double may override or wrap it, so the bundled stores keep calling it
    and the answer travels beside the return value, in a slot of the calling thread. The
    store's own release code records it; the outcome method clears the slot before the call
    and reads it right after. An override that bypasses the store's release code leaves the
    slot empty, which the caller treats as "unknown" and judges by the returned thread alone.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def clear(self) -> None:
        self._local.applied = None

    def record(self, *, applied: bool) -> None:
        self._local.applied = applied

    def read(self) -> bool | None:
        applied: bool | None = getattr(self._local, "applied", None)
        self._local.applied = None
        return applied


def _release_with_outcome(
    release: Callable[[], Thread], signal: _ReleaseSignal
) -> RunLockReleaseOutcome:
    """Run ``release`` and pair its thread with the answer the store recorded while doing so."""
    signal.clear()
    thread = release()
    applied = signal.read()
    return RunLockReleaseOutcome(thread, applied=True if applied is None else applied)


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
        owner_token: str | None = None,
    ) -> Thread:
        """Release a held lock by transitioning to a terminal status.

        ``status`` must be one of ``"idle"``, ``"interrupted"``, or
        ``"error"``. If status == ``"busy"`` this method raises
        ``ValueError``.

        When ``owner_token`` is supplied, the release is applied only while
        that token still owns a ``busy`` thread. A token that no longer
        matches, or a thread that is no longer ``busy`` (for example one a
        maintenance sweep reclaimed), is a **stale release**: it must not
        change the thread, and the unchanged current thread is returned.

        Return the thread **as it stands after the call**. After an applied
        release that is the requested ``status`` with ``lock_token`` cleared.
        Platform handlers compare the returned thread with what they asked
        for: a thread that is still ``busy``, still carries a ``lock_token`` or
        has a different status counts as a refused release and fails the run, so
        a store that returns a snapshot taken before the write, or keeps the
        token on the released row, makes every run fail. That comparison cannot
        tell an applied release from a stale no-op when another owner already
        left the thread at the requested status; a store that also implements
        :class:`ReleaseOutcomeProvider` closes that gap and handlers then use
        its answer instead.

        Raises ``KeyError(thread_id)`` if the thread does not exist.
        Implementations may treat this as a best-effort release when used
        by callers.
        """
        ...

    def renew_run_lock(self, thread_id: str, owner_token: str) -> bool:
        """Extend a held run lock, returning false after ownership changes.

        Ownership has changed when the token no longer matches or the thread
        is no longer ``busy``. Return ``True`` only after the lock was
        extended and ``False`` only for lost ownership: an operational error,
        including contention that could not be resolved within a bounded
        number of attempts, must be raised, not reported as ``False``. The
        Platform renewal worker fails the run in both cases.
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
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._threads: dict[str, Thread] = {}
        self._lock = threading.RLock()
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._release_signal = _ReleaseSignal()

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

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
                raise ValueError(f"Duplicate thread ID generated: {thread_id!r}")
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
            update: dict[str, Any] = {"updated_at": self._now()}

            if metadata is not None:
                update["metadata"] = dict(metadata)
            if status is not None:
                update["status"] = status
            if values is not None:
                update["values"] = values
            if interrupts is not None:
                update["interrupts"] = interrupts
            if assistant_id is not None:
                update["assistant_id"] = assistant_id

            updated = existing.model_copy(update=update)
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
            now = self._now()
            update: dict[str, Any] = {
                "status": "busy",
                "updated_at": now,
                "lock_token": uuid.uuid4().hex,
                "lock_expires_at": now + timedelta(seconds=60),
            }
            if existing.assistant_id is None and assistant_id is not None:
                update["assistant_id"] = assistant_id
            updated = existing.model_copy(update=update)
            self._threads[thread_id] = updated
            return self._deep_copy(updated)

    def release_run_lock(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> Thread:
        """Release the per-thread run lock to a terminal status."""
        if status == "busy":
            raise ValueError("release_run_lock cannot set status to 'busy'")
        with self._lock:
            if thread_id not in self._threads:
                raise KeyError(thread_id)
            existing = self._threads[thread_id]
            if owner_token is not None and (
                existing.lock_token != owner_token or existing.status != "busy"
            ):
                self._release_signal.record(applied=False)
                return self._deep_copy(existing)
            update: dict[str, Any] = {
                "status": status,
                "updated_at": self._now(),
                "lock_token": None,  # nosec B105 - clearing a lease token, not a password
                "lock_expires_at": None,
            }
            if values is not None:
                update["values"] = values
            updated = existing.model_copy(update=update)
            self._threads[thread_id] = updated
            self._release_signal.record(applied=True)
            return self._deep_copy(updated)

    def release_run_lock_outcome(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> RunLockReleaseOutcome:
        """Release exactly like :meth:`release_run_lock` and report whether it was applied."""
        return _release_with_outcome(
            lambda: self.release_run_lock(
                thread_id, status=status, values=values, owner_token=owner_token
            ),
            self._release_signal,
        )

    def renew_run_lock(self, thread_id: str, owner_token: str) -> bool:
        with self._lock:
            existing = self._threads.get(thread_id)
            if existing is None or existing.lock_token != owner_token or existing.status != "busy":
                return False
            now = self._now()
            updated = existing.model_copy(
                update={"updated_at": now, "lock_expires_at": now + timedelta(seconds=60)}
            )
            self._threads[thread_id] = updated
            return True

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
                    k in thread.metadata and thread.metadata[k] == v for k, v in metadata.items()
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
