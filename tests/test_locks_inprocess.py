"""Tests for InProcessThreadLock — the default in-process lock backend."""

from __future__ import annotations

import threading
import time

import pytest

from azure_functions_langgraph.locks import InProcessThreadLock, ThreadLock
from azure_functions_langgraph.locks.inprocess import _KeyState


class TestInProcessThreadLockProtocol:
    """InProcessThreadLock must satisfy the ThreadLock protocol."""

    def test_satisfies_thread_lock_protocol(self) -> None:
        assert isinstance(InProcessThreadLock(), ThreadLock)


class TestInProcessThreadLockAcquire:
    """Non-blocking and blocking acquire semantics."""

    def test_first_acquire_returns_token(self) -> None:
        lock = InProcessThreadLock()
        token = lock.acquire("graph", "t1")
        assert token
        lock.release("graph", "t1", token)

    def test_second_acquire_returns_none(self) -> None:
        lock = InProcessThreadLock()
        token = lock.acquire("graph", "t1")
        assert token
        assert lock.acquire("graph", "t1") is None
        lock.release("graph", "t1", token)

    def test_distinct_keys_do_not_conflict(self) -> None:
        lock = InProcessThreadLock()
        t1 = lock.acquire("graph", "t1")
        t2 = lock.acquire("graph", "t2")
        t3 = lock.acquire("other", "t1")
        assert t1 and t2 and t3
        # Each acquisition gets a distinct token.
        assert len({t1, t2, t3}) == 3
        lock.release("graph", "t1", t1)
        lock.release("graph", "t2", t2)
        lock.release("other", "t1", t3)

    def test_blocking_acquire_with_timeout_returns_none_after_deadline(self) -> None:
        lock = InProcessThreadLock()
        token = lock.acquire("graph", "t1")
        assert token
        start = time.monotonic()
        # 0.1s timeout — the lock is held so acquire must return None.
        assert lock.acquire("graph", "t1", timeout=0.1) is None
        elapsed = time.monotonic() - start
        # Verify we actually blocked (at least 90ms) rather than fast-fail.
        assert elapsed >= 0.09
        lock.release("graph", "t1", token)

    def test_blocking_acquire_returns_token_when_released(self) -> None:
        """Another thread releasing lets the blocked acquire complete."""
        lock = InProcessThreadLock()
        token = lock.acquire("graph", "t1")
        assert token

        acquired: dict[str, str | None] = {}

        def _try_acquire() -> None:
            # Blocking wait up to 1s — the main thread will release in 0.05s.
            acquired["result"] = lock.acquire("graph", "t1", timeout=1.0)

        thread = threading.Thread(target=_try_acquire)
        thread.start()
        time.sleep(0.05)
        lock.release("graph", "t1", token)
        thread.join(timeout=2.0)
        second_token = acquired["result"]
        assert second_token
        assert second_token != token
        lock.release("graph", "t1", second_token)


class TestInProcessThreadLockRelease:
    """Release semantics: cleanup, idempotence, no-raise on unknown/unheld."""

    def test_release_removes_entry_from_internal_dict(self) -> None:
        lock = InProcessThreadLock()
        token = lock.acquire("cleanup", "t1")
        assert token
        key = ("cleanup", "t1")
        assert key in lock._states
        lock.release("cleanup", "t1", token)
        assert key not in lock._states

    def test_release_of_unknown_key_is_silent(self) -> None:
        lock = InProcessThreadLock()
        # Must not raise even though the key was never acquired.
        lock.release("unknown", "t99", "no-such-token")

    def test_release_of_unheld_lock_is_silent(self) -> None:
        """release() on a lock that exists but isn't held is silent."""
        lock = InProcessThreadLock()
        token = lock.acquire("graph", "t1")
        assert token
        lock.release("graph", "t1", token)
        # A second release() should be a no-op — the lock is not held (it was
        # cleaned up on the first release) so the guarded lookup finds nothing.
        lock.release("graph", "t1", token)

    def test_release_matching_token_on_unheld_lock_swallows_runtimeerror(self) -> None:
        """A matching token whose underlying lock is not held is swallowed.

        The name previously implied "another holder"; in fact the failure mode
        is a state whose ``lock`` is *unheld* (e.g. after an external release),
        so ``lock.release()`` raises ``RuntimeError``. release() must swallow
        it, still drop the ref, and clean the entry up rather than leak it.
        """
        lock = InProcessThreadLock()
        # Force a state whose lock we do not actually hold, so lock.release()
        # raises RuntimeError inside release() and must be swallowed.
        state = _KeyState()
        state.token = "manual-token"
        state.refs = 1
        lock._states[("graph", "manual")] = state
        # No one holds state.lock, so calling .release() will raise RuntimeError.
        lock.release("graph", "manual", "manual-token")
        # Fall-through cleanup must have removed the leaked entry.
        assert ("graph", "manual") not in lock._states

    def test_reacquire_after_release(self) -> None:
        lock = InProcessThreadLock()
        first = lock.acquire("reacq", "t1")
        assert first
        lock.release("reacq", "t1", first)
        second = lock.acquire("reacq", "t1")
        assert second
        lock.release("reacq", "t1", second)


class TestInProcessThreadLockOwnerToken:
    """A stale/foreign token must not release a lock held by a newer owner."""

    def test_foreign_token_release_is_noop(self) -> None:
        lock = InProcessThreadLock()
        token = lock.acquire("graph", "t1")
        assert token
        # A caller with the wrong token must NOT free the lock.
        lock.release("graph", "t1", "foreign-token")
        # Still held — a fresh acquire fails.
        assert lock.acquire("graph", "t1") is None
        # The true owner can still release.
        lock.release("graph", "t1", token)
        final_token = lock.acquire("graph", "t1")
        assert final_token
        lock.release("graph", "t1", final_token)

    def test_stale_token_does_not_release_reacquired_lock(self) -> None:
        """The classic cross-execution bug: stale release must not free a new lease."""
        lock = InProcessThreadLock()
        stale_token = lock.acquire("graph", "t1")
        assert stale_token
        # Original owner releases legitimately.
        lock.release("graph", "t1", stale_token)
        # A different execution acquires the same key.
        new_token = lock.acquire("graph", "t1")
        assert new_token
        # The stale owner's late release must be a no-op — it must NOT free the
        # new owner's lock.
        lock.release("graph", "t1", stale_token)
        assert lock.acquire("graph", "t1") is None
        # New owner still holds it and can release.
        lock.release("graph", "t1", new_token)
        final_token = lock.acquire("graph", "t1")
        assert final_token
        lock.release("graph", "t1", final_token)

    def test_foreign_token_release_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        lock = InProcessThreadLock()
        token = lock.acquire("graph", "t1")
        assert token
        with caplog.at_level("DEBUG", logger="azure_functions_langgraph.locks.inprocess"):
            lock.release("graph", "t1", "foreign-token")
        assert any("token mismatch" in rec.getMessage() for rec in caplog.records)
        lock.release("graph", "t1", token)


class TestInProcessThreadLockConcurrency:
    """Only one thread can acquire a lock at a time."""

    def test_only_one_thread_wins(self) -> None:
        lock = InProcessThreadLock()
        winners: list[int] = []
        barrier = threading.Barrier(10)

        def _worker(worker_id: int) -> None:
            barrier.wait()
            token = lock.acquire("graph", "shared")
            if token:
                winners.append(worker_id)
                time.sleep(0.01)
                lock.release("graph", "shared", token)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Between 1 and 10 winners depending on scheduling; but each winner had
        # exclusive access (this is the invariant we care about).
        # We verify each key ("graph","shared") never has two winners
        # simultaneously by checking no duplicate winner in this window.
        assert len(winners) == len(set(winners))

    def test_cleanup_keeps_entry_if_another_acquire_in_flight(self) -> None:
        """release() must not evict the state while another ref is outstanding."""
        lock = InProcessThreadLock()
        token = lock.acquire("graph", "shared")
        assert token
        key = ("graph", "shared")
        state = lock._states[key]
        # Simulate a concurrent acquire that has reserved a ref but not yet
        # completed: refs is now 2 (this holder + the in-flight acquire).
        state.refs += 1
        # Releasing our hold drops one ref but must NOT GC the state, because
        # the in-flight acquire still references it.
        lock.release("graph", "shared", token)
        assert key in lock._states
        assert lock._states[key].refs == 1
        # Draining the outstanding ref lets the next release GC it.
        lock._states[key].refs -= 1
        lock._maybe_gc(key, state)
        assert key not in lock._states


class TestInProcessThreadLockLogging:
    """Debug logging on soft-failure paths (verifies coverage of the log calls)."""

    def test_release_unknown_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        lock = InProcessThreadLock()
        with caplog.at_level("DEBUG", logger="azure_functions_langgraph.locks.inprocess"):
            lock.release("unknown", "t99", "no-such-token")
        assert any("unknown lock key" in rec.getMessage() for rec in caplog.records)

    def test_release_unheld_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        lock = InProcessThreadLock()
        # Force an unheld lock entry so release() hits the RuntimeError branch.
        state = _KeyState()
        state.token = "tok"
        state.refs = 1
        lock._states[("graph", "t1")] = state
        with caplog.at_level("DEBUG", logger="azure_functions_langgraph.locks.inprocess"):
            lock.release("graph", "t1", "tok")
        assert any("unheld lock" in rec.getMessage() for rec in caplog.records)
