"""``release_run_lock_outcome``: an exact "was this release applied" answer beside the thread.

``release_run_lock`` answers a stale owner token with the unchanged current thread and no error.
That thread cannot always be told apart from the result of an applied release: when another owner
has already finished and left the thread at the requested status it looks identical. The bundled
stores therefore also report ``applied`` through the optional ``ReleaseOutcomeProvider``
capability (#804). Fake-backed (the in-process store and the Table model); the Azurite lane
asks the same questions of the real service in
``tests/integration/test_table_store_integration.py``.
"""

from __future__ import annotations

from threading import Barrier
from threading import Thread as WorkerThread
from typing import Any

import pytest

from azure_functions_langgraph.platform.contracts import Thread
from azure_functions_langgraph.platform.stores import (
    InMemoryThreadStore,
    ReleaseOutcomeProvider,
    RunLockReleaseOutcome,
)
from tests.test_stores_azure_table import _new_store


@pytest.fixture(params=["memory", "table"])
def store(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":
        return InMemoryThreadStore()
    return _new_store()[0]


def _held(store: Any) -> tuple[str, str]:
    """A thread under a live run lock: ``(thread_id, owner_token)``."""
    thread = store.create()
    locked = store.try_acquire_run_lock(thread.thread_id)
    assert locked is not None and locked.lock_token is not None
    return thread.thread_id, locked.lock_token


def test_both_bundled_stores_provide_the_capability(store: Any) -> None:
    assert isinstance(store, ReleaseOutcomeProvider)


def test_an_applied_release_is_reported_and_matches_release_run_lock(store: Any) -> None:
    # Given a held lock.
    thread_id, token = _held(store)

    # When it is released with its owner token and a result.
    outcome = store.release_run_lock_outcome(
        thread_id, status="idle", values={"a": 1}, owner_token=token
    )

    # Then the release was applied, and the thread is the one release_run_lock would return.
    assert isinstance(outcome, RunLockReleaseOutcome)
    assert outcome.applied is True
    assert (outcome.thread.status, outcome.thread.lock_token, outcome.thread.values) == (
        "idle",
        None,
        {"a": 1},
    )
    assert store.get(thread_id) == outcome.thread


def test_a_release_without_an_owner_token_is_always_applied(store: Any) -> None:
    thread_id, _ = _held(store)

    outcome = store.release_run_lock_outcome(thread_id, status="error")

    assert outcome.applied is True and outcome.thread.status == "error"


def test_a_release_with_the_wrong_token_is_not_applied_and_changes_nothing(store: Any) -> None:
    thread_id, _ = _held(store)
    before = store.get(thread_id)

    outcome = store.release_run_lock_outcome(
        thread_id, status="idle", values={"stale": True}, owner_token="not-the-owner"
    )

    assert outcome.applied is False
    assert outcome.thread == before
    assert store.get(thread_id) == before


def test_a_release_on_a_thread_that_is_no_longer_busy_is_not_applied(store: Any) -> None:
    # Given an owner whose thread was taken away (reclaimed behind its back).
    thread_id, token = _held(store)
    store.update(thread_id, status="error")

    outcome = store.release_run_lock_outcome(
        thread_id, status="idle", values={"stale": True}, owner_token=token
    )

    assert outcome.applied is False
    assert (outcome.thread.status, outcome.thread.values) == ("error", None)


def test_a_finished_new_owner_looks_like_an_applied_release_only_to_the_thread(store: Any) -> None:
    # Given the ordering behind the review finding: A loses the thread (reclaimed to "error"),
    # B acquires it and runs to completion, and only then does A release with its old token.
    thread_id, token_a = _held(store)
    store.release_run_lock(thread_id, status="error")
    locked_b = store.try_acquire_run_lock(thread_id)
    assert locked_b is not None and locked_b.lock_token is not None
    store.release_run_lock(
        thread_id, status="idle", values={"b": 1}, owner_token=locked_b.lock_token
    )

    # When A's stale release arrives.
    outcome = store.release_run_lock_outcome(
        thread_id, status="idle", values={"a": 1}, owner_token=token_a
    )

    # Then the thread is exactly what an applied release would have returned (idle, no token),
    # which is why the thread alone cannot decide, and the outcome says it was not applied.
    assert (outcome.thread.status, outcome.thread.lock_token) == ("idle", None)
    assert outcome.thread.values == {"b": 1}
    assert outcome.applied is False
    assert store.get(thread_id) == outcome.thread


def test_the_outcome_method_keeps_the_same_errors_as_release_run_lock(store: Any) -> None:
    thread_id, token = _held(store)

    with pytest.raises(ValueError, match="busy"):
        store.release_run_lock_outcome(thread_id, status="busy", owner_token=token)
    with pytest.raises(KeyError):
        store.release_run_lock_outcome("missing-thread", status="idle", owner_token=token)


def test_an_overridden_release_run_lock_stays_in_play(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a store whose release_run_lock is wrapped or overridden (a subclass that logs or
    # audits, a test double): the outcome method must call it, not bypass it.
    thread_id, token = _held(store)
    original = store.release_run_lock
    calls: list[dict[str, Any]] = []

    def wrapped(*args: Any, **kwargs: Any) -> Thread:
        calls.append(kwargs)
        return original(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(store, "release_run_lock", wrapped)

    # When the outcome is asked for.
    outcome = store.release_run_lock_outcome(thread_id, status="idle", owner_token=token)

    # Then the wrapper ran exactly once and the store's own answer still came through.
    assert len(calls) == 1 and calls[0]["owner_token"] == token
    assert outcome.applied is True


def test_an_override_that_bypasses_the_store_leaves_the_answer_unknown(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a release_run_lock replaced by something that never reaches the store's own code.
    thread_id, token = _held(store)
    canned = store.get(thread_id)
    monkeypatch.setattr(store, "release_run_lock", lambda *a, **k: canned)

    # When the outcome is asked for.
    outcome = store.release_run_lock_outcome(thread_id, status="idle", owner_token="whatever")

    # Then there is no recorded answer, so it is not reported as a refusal: callers fall back to
    # judging by the returned thread, exactly as for a store without the capability.
    assert outcome.applied is True and outcome.thread == canned


def test_a_stale_answer_never_leaks_into_the_next_call(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a stale release made through the plain method: the store records its answer (not
    # applied) but nobody asks for it, so it is left in the slot.
    thread_id, token = _held(store)
    store.release_run_lock(thread_id, status="idle", owner_token="not-the-owner")

    # When a later release goes through a bypassing override.
    canned = store.get(thread_id)
    monkeypatch.setattr(store, "release_run_lock", lambda *a, **k: canned)
    outcome = store.release_run_lock_outcome(thread_id, status="idle", owner_token=token)

    # Then the leftover was cleared before the call: the unknown case reports applied, not the
    # old refusal.
    assert outcome.applied is True


def test_concurrent_releases_do_not_see_each_others_answer(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given two threads that release at the same time, one stale and one applied, and a wrapper
    # that holds each of them at a barrier AFTER the store recorded its answer and BEFORE the
    # outcome method reads it. A single shared slot would hand both the same answer.
    stale_id, _ = _held(store)
    fresh_id, fresh_token = _held(store)
    original = store.release_run_lock
    barrier = Barrier(2, timeout=5)

    def wrapped(*args: Any, **kwargs: Any) -> Thread:
        thread = original(*args, **kwargs)
        barrier.wait()
        return thread  # type: ignore[no-any-return]

    monkeypatch.setattr(store, "release_run_lock", wrapped)
    results: dict[str, RunLockReleaseOutcome] = {}

    def release(label: str, thread_id: str, token: str) -> None:
        results[label] = store.release_run_lock_outcome(thread_id, status="idle", owner_token=token)

    workers = [
        WorkerThread(target=release, args=("stale", stale_id, "not-the-owner")),
        WorkerThread(target=release, args=("fresh", fresh_id, fresh_token)),
    ]

    # When both run.
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)

    # Then each got its own answer.
    assert results["stale"].applied is False
    assert results["fresh"].applied is True
