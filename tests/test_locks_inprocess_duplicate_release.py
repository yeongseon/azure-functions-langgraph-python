"""Deterministic duplicate-release interleavings for the in-process backend."""

from concurrent.futures import ThreadPoolExecutor
import threading
from types import TracebackType

import pytest

from azure_functions_langgraph.locks import InProcessThreadLock


class _PauseFirstGuardExit:
    """Pause the first caller after it drops the real guard, not while holding it."""

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.exited = threading.Event()
        self.resume = threading.Event()
        self.first = True

    def __enter__(self) -> None:
        self.guard.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pause = self.first
        self.first = False
        self.guard.release()
        if pause:
            self.exited.set()
            assert self.resume.wait(5), "first release was not resumed"


@pytest.mark.parametrize("handoff", [False, True], ids=["duplicate", "new-owner"])
def test_duplicate_release_is_atomic_when_first_release_is_in_flight(
    monkeypatch: pytest.MonkeyPatch, handoff: bool
) -> None:
    # Given an owner and a release paused just after its first critical section.
    lock = InProcessThreadLock()
    token = lock.acquire("graph", "thread")
    assert token
    key = ("graph", "thread")
    state = lock._states[key]
    guard = _PauseFirstGuardExit()
    monkeypatch.setattr(lock, "_guard", guard)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_release = pool.submit(lock.release, "graph", "thread", token)
        try:
            assert guard.exited.wait(5), "first release did not reach the handoff"
            # When a duplicate release arrives before the first call returns.
            lock.release("graph", "thread", token)
            next_token = lock.acquire("graph", "thread") if handoff else None
        finally:
            guard.resume.set()
        first_release.result(timeout=5)

    # Then exactly one owner reference was dropped, including on the old state.
    assert state.refs == 0
    if handoff:
        assert next_token
        assert lock._states[key].refs == 1
        assert lock.acquire("graph", "thread") is None
        lock.release("graph", "thread", next_token)
    assert key not in lock._states


def test_duplicate_release_preserves_reserved_acquisition_during_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an acquisition paused after reserving the current owner's state.
    lock = InProcessThreadLock()
    first = lock.acquire("graph", "thread")
    assert first
    key = ("graph", "thread")
    state = lock._states[key]
    guard = _PauseFirstGuardExit()
    monkeypatch.setattr(lock, "_guard", guard)

    with ThreadPoolExecutor(max_workers=1) as pool:
        acquisition = pool.submit(lock.acquire, "graph", "thread")
        try:
            assert guard.exited.wait(5), "acquisition did not reserve its reference"
            # When the owner releases twice before the reserved acquire completes.
            lock.release("graph", "thread", first)
            lock.release("graph", "thread", first)
            assert lock._states[key] is state
            assert state.refs == 1
        finally:
            guard.resume.set()
        second = acquisition.result(timeout=5)

    # Then the same primitive has a new owner that stale releases cannot disturb.
    assert second and second != first
    lock.release("graph", "thread", first)
    assert state.token == second
    assert state.refs == 1
    assert lock.acquire("graph", "thread") is None
    lock.release("graph", "thread", second)
    assert state.refs == 0
    assert key not in lock._states
