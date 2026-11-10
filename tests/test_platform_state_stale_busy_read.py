"""A state update whose prior-status read saw ``busy`` is refused, not run and left busy (#845).

``POST /threads/{id}/state`` reads the thread before it takes any lock and releases to the status
it read, so an ``idle``, ``interrupted`` or ``error`` thread goes back to where it was. A run that
was in flight at that read and finished before the store run lock was taken leaves the row
terminal: the acquisition succeeded, ``update_state`` committed, and the handler then asked the
store to release to ``busy``. Both bundled stores refuse that (``release_run_lock cannot set status
to 'busy'``), so the release was never sent: the update answered ``500`` although it had been
applied, and the thread stayed ``busy`` under the update's own lock token until
``reset_stale_locks`` reclaimed it.

The terminal status that the finished run left cannot be recovered once the lock is taken (the
acquisition overwrites it with ``busy``), so there is no correct status to release to. The handler
refuses before it takes any lock or runs anything, with the ``409`` that a busy thread already gets:
nothing changes and the client may retry. A thread that is genuinely busy keeps the same answer.

Fake-backed, like the other state-update ownership tests: the reads that happen before the store
lock is taken are made to report ``busy`` while the stored row is terminal. The scenarios are plain
``check_*`` functions over the ``StateHarness`` of ``test_platform_state_release_ownership`` so that
the Azurite lane can run the very same assertions through a real Table service.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from azure_functions_langgraph.platform.contracts import Thread, ThreadStatus
from tests.test_platform_state_release_ownership import StateHarness

BUSY_DETAIL = "State updates are not supported while a run is active."
TERMINAL_STATUSES = ["idle", "interrupted", "error"]


def report_busy_until_the_store_lock_is_taken(h: StateHarness) -> Any:
    """Every read before the store lock is taken reports ``busy``; the stored row is untouched.

    This is a run that was in flight at the handler's reads and finished before its acquisition.
    Returns the store's own ``get`` so a test can read the real row.
    """
    real_get = h.store.get
    real_acquire = h.store.try_acquire_run_lock
    acquiring = {"started": False}

    def get(thread_id: str) -> Thread | None:
        thread: Thread | None = real_get(thread_id)
        if thread is not None and not acquiring["started"]:
            return thread.model_copy(update={"status": "busy"})
        return thread

    def acquire(*args: Any, **kwargs: Any) -> Any:
        acquiring["started"] = True
        return real_acquire(*args, **kwargs)

    h.store.get = get
    h.store.try_acquire_run_lock = acquire
    return real_get


def check_a_stale_busy_read_is_refused_before_anything_changes(h: StateHarness) -> None:
    # Given a terminal thread whose pre-lock reads report busy, because a run finished after them.
    real_get = report_busy_until_the_store_lock_is_taken(h)
    before = real_get(h.thread_id)
    assert before is not None and before.status == h.prior_status

    # When the state update arrives.
    response = h.update()

    # Then it is the existing busy answer, and the update never ran.
    assert response.status_code == 409
    assert BUSY_DETAIL in json.loads(response.get_body())["detail"]
    assert h.graph.update_calls == 0
    # And the row is exactly as the finished run left it: its terminal status, no lock token.
    after = real_get(h.thread_id)
    assert after is not None
    assert (after.status, after.lock_token, after.lock_expires_at) == (h.prior_status, None, None)
    assert after.updated_at == before.updated_at
    # And the shared thread lock was never taken, so nothing is left to release.
    assert h.lock.acquired == [] and h.lock.released == []
    assert h.observer.events == []


def check_the_thread_can_be_updated_once_the_stale_read_is_gone(h: StateHarness) -> None:
    # Given a refused update, because its read saw a run that had already finished.
    report_busy_until_the_store_lock_is_taken(h)
    assert h.update().status_code == 409

    # When the client retries and the reads are accurate again.
    del h.store.get, h.store.try_acquire_run_lock
    response = h.update()

    # Then the retry is an ordinary update that goes back to the prior status.
    assert response.status_code == 200
    after = h.store.get(h.thread_id)
    assert after is not None and (after.status, after.lock_token) == (h.prior_status, None)
    assert h.graph.update_calls == 1
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


def check_a_genuinely_busy_thread_still_answers_409_and_keeps_its_owner(h: StateHarness) -> None:
    # Given a thread that another run really holds.
    owner = h.store.try_acquire_run_lock(h.thread_id)
    assert owner is not None and owner.lock_token is not None

    # When the state update arrives.
    response = h.update()

    # Then it is the busy answer, nothing ran, and the owner's row is untouched.
    assert response.status_code == 409
    assert BUSY_DETAIL in json.loads(response.get_body())["detail"]
    assert h.graph.update_calls == 0
    after = h.store.get(h.thread_id)
    assert after is not None and (after.status, after.lock_token) == ("busy", owner.lock_token)
    assert h.lock.released == h.lock.acquired


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("prior_status", TERMINAL_STATUSES)
def test_a_stale_busy_read_is_refused_before_anything_changes(
    store_kind: str, prior_status: ThreadStatus
) -> None:
    check_a_stale_busy_read_is_refused_before_anything_changes(
        StateHarness(store_kind, prior_status=prior_status)
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("prior_status", TERMINAL_STATUSES)
def test_the_thread_can_be_updated_once_the_stale_read_is_gone(
    store_kind: str, prior_status: ThreadStatus
) -> None:
    check_the_thread_can_be_updated_once_the_stale_read_is_gone(
        StateHarness(store_kind, prior_status=prior_status)
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_a_genuinely_busy_thread_still_answers_409_and_keeps_its_owner(store_kind: str) -> None:
    check_a_genuinely_busy_thread_still_answers_409_and_keeps_its_owner(StateHarness(store_kind))
