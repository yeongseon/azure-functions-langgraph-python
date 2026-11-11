"""Lock scope resolution: graphs on the same checkpoint storage share one lock.

Thread locks are keyed by ``(scope, thread_id)``. Two registered graphs that
write the same checkpoint storage collide on a ``thread_id`` (and checkpoint
namespace), so locking them under different graph names would let both writers
run at once.

The scope of a graph is derived **only from that graph's own checkpointer**:

* A checkpointer that exposes an optional ``lock_scope_identity`` string (the
  bundled :class:`~azure_functions_langgraph.checkpointers.azure_blob.AzureBlobCheckpointSaver`
  reports ``azure-blob:<account>/<container>``) names its physical storage.
  Every graph on that storage gets the scope ``storage-<sha256(identity)[:32]>``,
  no matter which graphs, apps or processes register it. Nothing about the
  registration inventory feeds into the result, so it cannot change when
  another graph is registered later or when a different app (or a fleet member
  with a different set of graphs) computes it.
* Every other graph (no checkpointer, the ``True``/``False`` LangGraph
  sentinels, or a saver that exposes no identity) keeps its registered graph
  name as the scope. Sharing between such savers **cannot be detected**.

Cross-surface name overlap (an HTTP graph and a Service Bus trigger both named
``worker``) resolves by name, so :func:`ensure_unambiguous_scope` rejects an
overlap whose two registrations would resolve to different scopes.

:class:`ScopedThreadLock` binds each acquired token to the scope it was
acquired under, so release and ownership guards always address the acquiring
key. The lock is per thread, so it also covers every checkpoint namespace. It
is not fencing.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import threading
from typing import Any

from azure_functions_langgraph.locks.base import ThreadLock
from azure_functions_langgraph.locks.guard import OwnershipGuard, OwnershipGuardProvider

__all__ = [
    "ScopedThreadLock",
    "ensure_unambiguous_scope",
    "graph_lock_scope",
    "scope_thread_lock",
]


def storage_identity(graph: object) -> str | None:
    """Return the checkpointer's optional storage identity, if it exposes one."""
    checkpointer: object = getattr(graph, "checkpointer", None)
    # ``True`` / ``False`` are LangGraph's inherit / disable sentinels, not savers.
    if checkpointer is None or isinstance(checkpointer, bool):
        return None
    identity: object = getattr(checkpointer, "lock_scope_identity", None)
    if isinstance(identity, str) and identity:
        return identity
    return None


def graph_lock_scope(name: str, graph: object) -> str:
    """Return the lock scope for graph ``name`` (a pure function of its own saver)."""
    identity = storage_identity(graph)
    if identity is None:
        return name
    return "storage-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def ensure_unambiguous_scope(name: str, graph: object, existing: Any | None) -> None:
    """Reject registering ``name`` when ``existing`` (same name, other surface) differs.

    ``existing`` is the registration already holding ``name`` on another
    surface, or ``None``. Both would be locked by name, so they must agree.
    """
    if existing is None:
        return
    other = graph_lock_scope(name, getattr(existing, "graph", None))
    if other != graph_lock_scope(name, graph):
        raise ValueError(
            f"{name!r} is already registered on another surface with a different "
            "checkpoint storage; use distinct names so each resolves to one lock scope"
        )


class ScopedThreadLock:
    """Adapter translating registered graph names to storage lock scopes."""

    def __init__(self, inner: ThreadLock, scope_of: Callable[[str], str]) -> None:
        self._inner = inner
        self._scope_of = scope_of
        self._bound: dict[tuple[str, str], str] = {}
        self._bound_guard = threading.Lock()

    @property
    def inner(self) -> ThreadLock:
        return self._inner

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        scope = self._scope_of(graph_name)
        if timeout == 0.0:
            token = self._inner.acquire(scope, thread_id)
        else:
            token = self._inner.acquire(scope, thread_id, timeout=timeout)
        if token is not None:
            with self._bound_guard:
                self._bound[(thread_id, token)] = scope
        return token

    def _scope_for(self, graph_name: str, thread_id: str, token: str) -> str:
        with self._bound_guard:
            scope = self._bound.get((thread_id, token))
        return scope if scope is not None else self._scope_of(graph_name)

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        scope = self._scope_for(graph_name, thread_id, token)
        self._inner.release(scope, thread_id, token)
        # Drop the binding only for the token's owner: a stale/foreign token
        # was never bound, so this is a no-op for it.
        with self._bound_guard:
            self._bound.pop((thread_id, token), None)

    def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> OwnershipGuard | None:
        if not isinstance(self._inner, OwnershipGuardProvider):
            return None
        scope = self._scope_for(graph_name, thread_id, token)
        return self._inner.ownership_guard(scope, thread_id, token)


def scope_thread_lock(inner: ThreadLock, scope_of: Callable[[str], str]) -> ThreadLock:
    """Wrap ``inner`` so same-storage graphs compete on one lock key.

    The wrapper class is named after ``type(inner)`` so the ``lock_backend``
    observability label (``type(thread_lock).__name__``) is unchanged.
    """
    wrapper_cls = type(type(inner).__name__, (ScopedThreadLock,), {})
    wrapper: ScopedThreadLock = wrapper_cls(inner, scope_of)
    return wrapper
