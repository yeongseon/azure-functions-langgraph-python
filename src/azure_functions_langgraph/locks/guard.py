"""Opt-in ownership guard: lets a checkpoint saver reject writes after lock loss.

A thread lock that can *detect* that it no longer owns a ``(graph_name,
thread_id)`` may hand a small :class:`OwnershipGuard` to the graph run that
holds its token. A guard-aware checkpoint saver (for example
:class:`~azure_functions_langgraph.checkpointers.azure_blob.AzureBlobCheckpointSaver`)
calls :meth:`OwnershipGuard.check` before every write and refuses to write once
the guard reports loss.

This is **not** fencing. It reduces writes after *locally detected* loss and
nothing more:

* Detection lags. Loss is noticed when the lock itself notices it (a failed
  lease renewal, an explicit release, a closed lock), not when it happens.
* A check can pass an instant before the write it protects, and the write can
  still be accepted afterwards.
* Nothing stops the stale graph from continuing to run, and nothing reverts
  its side effects outside the checkpoint store.
* :class:`ThreadLeaseLostError` reports a locally detected condition. It is not
  proof that a stale write happened, and it does not mean earlier writes were
  rolled back.

Locks without the optional :class:`OwnershipGuardProvider` capability (the
in-process lock, third-party locks) behave exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final, Literal, Protocol, overload, runtime_checkable

__all__ = [
    "LeaseLossReason",
    "OwnershipGuard",
    "OwnershipGuardProvider",
    "ThreadLeaseLostError",
]
# Everything else in this module (``OWNERSHIP_GUARD_CONFIG_KEY``,
# ``guard_run_config``, ``config_with_ownership_guard``, ``check_ownership_guard``,
# ``ownership_guard_for``, ``resolve_ownership_guard``, ``bind_ownership_guard``)
# is adapter plumbing shared by the transports and the checkpoint saver. It is
# deliberately outside ``__all__``: not public API, and free to change.

#: Private run-config key (under ``configurable``) that carries the guard from
#: a transport boundary to the checkpoint saver. It is internal: it is not part
#: of the public API, it is never persisted, and any client-supplied value is
#: discarded. The ``__pregel_`` prefix makes LangGraph itself strip the key
#: from ``debug`` / ``checkpoints`` stream payloads.
OWNERSHIP_GUARD_CONFIG_KEY: Final = "__pregel_azfunc_ownership_guard"

#: Why a guard rejected a write.
#:
#: * ``"lost"`` -- the lock detected that it lost the lease (definitive loss
#:   or exhausted renewals).
#: * ``"released"`` -- the token was already released, or the lock was closed.
#: * ``"superseded"`` -- the key is now held under a different (newer) token.
#: * ``"missing"`` -- a saver that requires a guard received a write without one.
LeaseLossReason = Literal["lost", "released", "superseded", "missing"]

# One fixed sentence per reason. Messages never contain thread ids, graph names
# or tokens: ``str(exc)`` can reach a Durable client through ``serialize_error``.
# The same error is raised both when a checkpoint write is about to happen and
# when a finished run is checked, so the wording must hold for either.
_LOCALLY_DETECTED: Final = (
    "This reports a locally detected condition: it is not proof that a stale write "
    "occurred, and it does not mean that earlier checkpoint writes were rolled back."
)
_MESSAGES: Final[dict[LeaseLossReason, str]] = {
    "lost": f"Thread lock ownership was detected as lost. {_LOCALLY_DETECTED}",
    "released": (
        f"Thread lock ownership was already released, or the lock was closed. {_LOCALLY_DETECTED}"
    ),
    "superseded": (f"Thread lock ownership now belongs to a newer execution. {_LOCALLY_DETECTED}"),
    "missing": (
        "The checkpoint saver requires a thread lock ownership guard but this write "
        "carried none, so it was rejected."
    ),
}


class ThreadLeaseLostError(RuntimeError):
    """Locally detected loss of thread-lock ownership.

    Raised by an :class:`OwnershipGuard` when ownership of a ``(graph_name,
    thread_id)`` is no longer held: by a guard-aware checkpoint saver instead of
    writing a checkpoint, and once more by the adapter when a run finishes
    (so a run that ended after detected loss is not reported as a success). A
    saver configured to require a guard raises it with ``reason="missing"`` for
    a write that carries none.

    .. warning::
        This is not proof that a stale write happened, and it does not mean
        that checkpoints committed earlier in the same run were rolled back:
        the run may already have persisted most or all of its state. Do not
        blindly retry non-idempotent work after this error (for example a
        redelivered Service Bus message). It is also not fencing: see the
        production guide for the exact contract.

    The message is a fixed sentence that never contains thread ids, graph names
    or tokens, so it is safe to return to clients. Use the attributes for
    diagnostics.

    Attributes:
        reason: Why the guard rejected the write (``"lost"``, ``"released"``,
            ``"superseded"`` or ``"missing"``).
        graph_name: The graph the guard was created for, when known.
        thread_id: The thread the guard was created for, when known.
    """

    def __init__(
        self,
        *,
        reason: LeaseLossReason,
        graph_name: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        super().__init__(_MESSAGES[reason])
        self.reason: LeaseLossReason = reason
        self.graph_name: str | None = graph_name
        self.thread_id: str | None = thread_id

    def __reduce__(self) -> tuple[Any, ...]:
        # The constructor is keyword-only, so the default ``BaseException``
        # reduction (positional ``args``) cannot rebuild the error for
        # ``copy`` / ``pickle``.
        return (_rebuild_thread_lease_lost_error, (self.reason, self.graph_name, self.thread_id))


def _rebuild_thread_lease_lost_error(
    reason: LeaseLossReason, graph_name: str | None, thread_id: str | None
) -> ThreadLeaseLostError:
    return ThreadLeaseLostError(reason=reason, graph_name=graph_name, thread_id=thread_id)


@runtime_checkable
class OwnershipGuard(Protocol):
    """Answers, for one specific lock acquisition, "is ownership still held?"."""

    def check(self) -> None:
        """Raise :class:`ThreadLeaseLostError` if ownership is no longer held.

        The guard is bound to the **original** lock token of one acquisition,
        never to "whoever currently owns this thread".

        Implementations must be cheap and must not block or perform network
        I/O: ``check`` runs inside checkpoint writes, including on event-loop
        threads.
        """
        ...


@runtime_checkable
class OwnershipGuardProvider(Protocol):
    """Optional lock capability: hand out an :class:`OwnershipGuard` per acquisition.

    A :class:`~azure_functions_langgraph.locks.ThreadLock` that also implements
    this protocol takes part in guarded checkpoint writes. ``acquire`` /
    ``release`` and the opaque owner token are unchanged.
    """

    def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> OwnershipGuard | None:
        """Return a guard bound to ``token``, or ``None`` when guarding is off.

        Like :meth:`OwnershipGuard.check`, this must be cheap and must not
        block or perform network I/O: the adapter calls it on event-loop
        threads while it holds the owner token.

        Args:
            graph_name: Registered graph name the token was acquired for.
            thread_id: Thread ID the token was acquired for.
            token: The owner token returned by the matching ``acquire`` call.
        """
        ...


def ownership_guard_for(
    thread_lock: object, graph_name: str, thread_id: str | None, token: str | None
) -> OwnershipGuard | None:
    """Return the guard for one acquisition, or ``None`` when there is none.

    ``None`` unless the lock actually returned a token for a thread *and*
    implements :class:`OwnershipGuardProvider`. A provider that returns
    something other than a guard or ``None`` is a bug and raises
    :class:`TypeError` instead of silently running unguarded.
    """
    if not token or thread_id is None:
        return None
    if not isinstance(thread_lock, OwnershipGuardProvider):
        return None
    guard = thread_lock.ownership_guard(graph_name, thread_id, token)
    if guard is None:
        return None
    if not isinstance(guard, OwnershipGuard):
        raise TypeError(
            f"{type(thread_lock).__name__}.ownership_guard() must return an OwnershipGuard "
            f"or None, not {type(guard).__name__}"
        )
    return guard


@overload
def config_with_ownership_guard(
    config: dict[str, Any], guard: OwnershipGuard | None
) -> dict[str, Any]: ...


@overload
def config_with_ownership_guard(
    config: None, guard: OwnershipGuard | None
) -> dict[str, Any] | None: ...


def config_with_ownership_guard(
    config: dict[str, Any] | None, guard: OwnershipGuard | None
) -> dict[str, Any] | None:
    """Return ``config`` carrying ``guard`` under the private key.

    Any pre-existing value under :data:`OWNERSHIP_GUARD_CONFIG_KEY` is removed
    first, at the top level *and* under ``configurable``: LangGraph moves
    unknown top-level keys into ``configurable``, and some transports copy
    client-supplied top-level keys, so a client value must never survive.

    The caller's ``config`` is never mutated. When there is nothing to remove
    and nothing to inject (``guard is None`` and no stray key), the **same**
    object is returned so locks without the capability see an unchanged config.
    """
    configurable = config.get("configurable") if config is not None else None
    configurable_map = configurable if isinstance(configurable, Mapping) else None
    has_stray_key = config is not None and (
        OWNERSHIP_GUARD_CONFIG_KEY in config
        or (configurable_map is not None and OWNERSHIP_GUARD_CONFIG_KEY in configurable_map)
    )
    if guard is None and not has_stray_key:
        return config

    result: dict[str, Any] = (
        {}
        if config is None
        else {k: v for k, v in config.items() if k != OWNERSHIP_GUARD_CONFIG_KEY}
    )
    cleaned: dict[str, Any] = (
        {}
        if configurable_map is None
        else {k: v for k, v in configurable_map.items() if k != OWNERSHIP_GUARD_CONFIG_KEY}
    )
    if guard is not None:
        cleaned[OWNERSHIP_GUARD_CONFIG_KEY] = guard
        result["configurable"] = cleaned
    elif configurable_map is not None:
        result["configurable"] = cleaned
    return result


@overload
def guard_run_config(
    thread_lock: object,
    graph_name: str,
    thread_id: str | None,
    token: str | None,
    config: dict[str, Any],
) -> tuple[dict[str, Any], OwnershipGuard | None]: ...


@overload
def guard_run_config(
    thread_lock: object,
    graph_name: str,
    thread_id: str | None,
    token: str | None,
    config: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, OwnershipGuard | None]: ...


def guard_run_config(
    thread_lock: object,
    graph_name: str,
    thread_id: str | None,
    token: str | None,
    config: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, OwnershipGuard | None]:
    """The one helper every lock-holding boundary calls before running the graph.

    Returns ``(run_config, guard)``: ``run_config`` is the config to hand to the
    graph, ``guard`` is the guard to :func:`check_ownership_guard` after the
    run completes (``None`` when the lock has no guard). Call it **inside** the
    scope that releases ``token`` so a failing provider cannot leak the token.
    """
    guard = ownership_guard_for(thread_lock, graph_name, thread_id, token)
    return config_with_ownership_guard(config, guard), guard


def check_ownership_guard(guard: OwnershipGuard | None) -> None:
    """Raise :class:`ThreadLeaseLostError` if ``guard`` reports loss; ``None`` is a no-op."""
    if guard is not None:
        guard.check()


_scoped_guard: ContextVar[OwnershipGuard | None] = ContextVar(
    "azure_functions_langgraph_ownership_guard", default=None
)


def resolve_ownership_guard(config: Mapping[str, Any] | None) -> OwnershipGuard | None:
    """Find the guard that applies to a checkpoint write.

    A guard bound with :func:`bind_ownership_guard` wins. Otherwise the value
    under :data:`OWNERSHIP_GUARD_CONFIG_KEY` in ``config["configurable"]`` is
    used **only if it really is a guard**: a client-controlled JSON string or
    dict under that key is ignored, never trusted.
    """
    scoped = _scoped_guard.get()
    if scoped is not None:
        return scoped
    if config is None:
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    candidate = configurable.get(OWNERSHIP_GUARD_CONFIG_KEY)
    if isinstance(candidate, OwnershipGuard):
        return candidate
    return None


@contextmanager
def bind_ownership_guard(guard: OwnershipGuard | None) -> Iterator[None]:
    """Make ``guard`` apply to every checkpoint write made by the current context.

    LangGraph's ``update_state`` can write through configs the saver itself
    returned (and, for ``as_node="__copy__"``, through a stored config), none
    of which carry the guard. The adapter binds the guard around its
    **synchronous** ``update_state`` call so those writes are checked too.

    The binding is a :class:`~contextvars.ContextVar`: it is restored on every
    exit path, it is isolated per thread / task, and it is copied into worker
    threads by :func:`asyncio.to_thread`. Never hold it across a ``yield`` of a
    generator. ``None`` is a no-op.
    """
    if guard is None:
        yield
        return
    reset_token = _scoped_guard.set(guard)
    try:
        yield
    finally:
        _scoped_guard.reset(reset_token)
