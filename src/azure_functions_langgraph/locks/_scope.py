"""Lock scope resolution: graphs that share a checkpointer share one lock identity.

Thread locks are keyed by ``(graph_name, thread_id)``. Two registered graphs
that were compiled with the *same* checkpointer object read and write the same
checkpoint records for a given ``thread_id`` (and checkpoint namespace), so
locking them under different graph names would let both writers run at once.

:class:`ScopedThreadLock` is an internal adapter placed between the transports
and the configured :class:`~azure_functions_langgraph.locks.base.ThreadLock`.
It rewrites the ``graph_name`` argument to a *lock scope*:

* A graph without a checkpointer keeps its own name (key unchanged).
* Graphs holding the **same checkpointer object** (``is``) share one scope: the
  lexicographically smallest registered name in the group. Choosing the
  minimum makes the scope independent of registration order, so every instance
  of a fleet that registers the same graphs computes the same lock key.

Limits (documented in the production guide): this is object identity, not a
check of physical storage. Two distinct saver objects pointed at the same
storage are not detected. The lock is per thread, so it also covers every
checkpoint namespace of that thread (a superset of the physical collision
scope). It is not fencing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from azure_functions_langgraph.locks.base import ThreadLock
from azure_functions_langgraph.locks.guard import OwnershipGuard, OwnershipGuardProvider

__all__ = ["ScopedThreadLock", "resolve_lock_scope", "scope_thread_lock"]


def _checkpointer_of(reg: Any) -> object | None:
    checkpointer: object = getattr(getattr(reg, "graph", None), "checkpointer", None)
    # ``True`` / ``False`` are LangGraph's inherit / disable sentinels, not savers.
    if checkpointer is None or isinstance(checkpointer, bool):
        return None
    return checkpointer


def resolve_lock_scope(name: str, registrations: Iterable[Any]) -> str:
    """Return the lock scope for graph ``name`` given all registrations."""
    regs = list(registrations)
    checkpointer: object | None = None
    for reg in regs:
        if getattr(reg, "name", None) == name:
            checkpointer = _checkpointer_of(reg)
            if checkpointer is not None:
                break
    if checkpointer is None:
        return name
    names: list[str] = [
        str(reg.name)
        for reg in regs
        if _checkpointer_of(reg) is checkpointer and hasattr(reg, "name")
    ]
    return min([name, *names])


class ScopedThreadLock:
    """Adapter translating registered graph names to shared lock scopes."""

    def __init__(self, inner: ThreadLock, scope_of: Callable[[str], str]) -> None:
        self._inner = inner
        self._scope_of = scope_of

    @property
    def inner(self) -> ThreadLock:
        return self._inner

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        scope = self._scope_of(graph_name)
        if timeout == 0.0:
            return self._inner.acquire(scope, thread_id)
        return self._inner.acquire(scope, thread_id, timeout=timeout)

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self._inner.release(self._scope_of(graph_name), thread_id, token)

    def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> OwnershipGuard | None:
        if not isinstance(self._inner, OwnershipGuardProvider):
            return None
        return self._inner.ownership_guard(self._scope_of(graph_name), thread_id, token)


def scope_thread_lock(inner: ThreadLock, scope_of: Callable[[str], str]) -> ThreadLock:
    """Wrap ``inner`` so shared-checkpointer graphs compete on one lock key.

    The wrapper class is named after ``type(inner)`` so the ``lock_backend``
    observability label (``type(thread_lock).__name__``) is unchanged.
    """
    wrapper_cls = type(type(inner).__name__, (ScopedThreadLock,), {})
    wrapper: ScopedThreadLock = wrapper_cls(inner, scope_of)
    return wrapper
