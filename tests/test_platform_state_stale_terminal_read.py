"""A state update keeps the terminal status a run left after its first status read (#849).

``POST /threads/{id}/state`` reads the thread, takes the shared ``ThreadLock`` and the store run
lock, runs ``graph.update_state`` and releases to the status it read, so an ``idle``,
``interrupted`` or ``error`` thread goes back to where it was. A run that started and finished
between that first read and the lock acquisition leaves a different terminal status; the
acquisition succeeded, the update committed, and the release wrote the status read earlier, so
``error`` or ``interrupted`` became ``idle`` behind a normal ``200``.

The handler now reads the thread again once it holds the shared lock, which is before the store
lock overwrites the status with ``busy``. Under a distributed ``ThreadLock`` a run holds that lock
for its whole duration, so that read is accurate. With the process-local default a run in another
instance does not hold it, and the window between the second read and the store acquisition
remains; the production guide says so.

Fake-backed: every read before the shared lock is taken reports ``reported`` while the stored row
holds a different terminal status. The scenarios are plain ``check_*`` functions over the
``StateHarness`` of ``test_platform_state_release_ownership`` so that the Azurite lane can run the
very same assertions through a real Table service.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any

import pytest

from azure_functions_langgraph.platform.contracts import Thread, ThreadStatus
from tests.test_platform_state_release_ownership import StateHarness

ROOT = Path(__file__).resolve().parents[1]
UPDATE_FAILURE = {"detail": "Internal error updating thread state"}
# (status the first read reports, status the run actually left in the row)
STALE_PAIRS: list[tuple[ThreadStatus, ThreadStatus]] = [
    ("idle", "error"),
    ("idle", "interrupted"),
    ("error", "idle"),
    ("interrupted", "error"),
]


def reads_differ_once_the_shared_lock_is_taken(
    h: StateHarness, read: Callable[[Callable[[str], Thread | None], str, bool], Thread | None]
) -> Callable[[str], Thread | None]:
    """Route the store's ``get`` through ``read(real_get, thread_id, locked)``.

    ``locked`` turns true once the shared thread lock has been acquired, which separates the
    handler's first read from the one it makes under the lock. Returns the real ``get`` so a test
    can read the stored row.
    """
    real_get: Callable[[str], Thread | None] = h.store.get
    real_acquire = h.lock.acquire
    state = {"locked": False}

    def get(thread_id: str) -> Thread | None:
        return read(real_get, thread_id, state["locked"])

    def acquire(graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        token = real_acquire(graph_name, thread_id, timeout)
        state["locked"] = token is not None
        return token

    h.store.get = get
    setattr(h.lock, "acquire", acquire)  # noqa: B010 - replace the recording method on one lock
    return real_get


def check_a_stale_terminal_read_does_not_replace_the_status_the_run_left(
    h: StateHarness, reported: ThreadStatus
) -> None:
    # Given a thread whose row holds what a finished run left, while the first read says otherwise.
    assert reported != h.prior_status

    def read(real_get: Callable[[str], Thread | None], thread_id: str, locked: bool) -> Any:
        thread = real_get(thread_id)
        if thread is not None and not locked:
            return thread.model_copy(update={"status": reported})
        return thread

    real_get = reads_differ_once_the_shared_lock_is_taken(h, read)
    left = real_get(h.thread_id)
    assert left is not None and left.status == h.prior_status

    # When the state update arrives.
    response = h.update()

    # Then the update is applied and answers normally.
    assert response.status_code == 200
    assert h.graph.update_calls == 1
    # And the status the run left is kept, with no lock token, not the status the first read saw.
    after = real_get(h.thread_id)
    assert after is not None and (after.status, after.lock_token) == (h.prior_status, None)
    # And the shared lock is taken and released once.
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


def check_a_thread_deleted_after_the_first_read_answers_404_and_frees_the_lock(
    h: StateHarness,
) -> None:
    # Given a thread that disappears after the handler's first read and before its second.
    def read(real_get: Callable[[str], Thread | None], thread_id: str, locked: bool) -> Any:
        return None if locked else real_get(thread_id)

    reads_differ_once_the_shared_lock_is_taken(h, read)

    # When the state update arrives.
    response = h.update()

    # Then it is the existing 404, nothing ran, and the shared lock was released.
    assert response.status_code == 404
    assert h.graph.update_calls == 0
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


def check_a_run_that_started_after_the_first_read_is_refused_and_keeps_its_owner(
    h: StateHarness,
) -> None:
    # Given a run that takes the thread after the handler's first read and before its second.
    owner_token: list[str | None] = []

    def read(real_get: Callable[[str], Thread | None], thread_id: str, locked: bool) -> Any:
        if locked and not owner_token:
            owner = h.store.try_acquire_run_lock(thread_id)
            assert owner is not None
            owner_token.append(owner.lock_token)
        return real_get(thread_id)

    real_get = reads_differ_once_the_shared_lock_is_taken(h, read)

    # When the state update arrives.
    response = h.update()

    # Then it is the busy answer, nothing ran, and the shared lock was released.
    assert response.status_code == 409
    assert (
        "State updates are not supported while a run is active."
        in (json.loads(response.get_body())["detail"])
    )
    assert h.graph.update_calls == 0
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired
    # And the running owner's row is untouched.
    after = real_get(h.thread_id)
    assert after is not None and (after.status, after.lock_token) == ("busy", owner_token[0])


def check_a_failed_second_read_answers_a_sanitized_500_and_frees_the_lock(
    h: StateHarness,
) -> None:
    # Given a store whose read fails after the shared lock was taken.
    def read(real_get: Callable[[str], Thread | None], thread_id: str, locked: bool) -> Any:
        if locked:
            raise RuntimeError("storage-secret-canary")
        return real_get(thread_id)

    reads_differ_once_the_shared_lock_is_taken(h, read)

    # When the state update arrives.
    response = h.update()

    # Then the caller gets the sanitized failure, nothing ran, and the lock is released.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == UPDATE_FAILURE
    assert b"storage-secret-canary" not in response.get_body()
    assert h.graph.update_calls == 0
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize(("reported", "left"), STALE_PAIRS)
def test_a_stale_terminal_read_does_not_replace_the_status_the_run_left(
    store_kind: str, reported: ThreadStatus, left: ThreadStatus
) -> None:
    check_a_stale_terminal_read_does_not_replace_the_status_the_run_left(
        StateHarness(store_kind, prior_status=left), reported
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_a_thread_deleted_after_the_first_read_answers_404_and_frees_the_lock(
    store_kind: str,
) -> None:
    check_a_thread_deleted_after_the_first_read_answers_404_and_frees_the_lock(
        StateHarness(store_kind)
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_a_failed_second_read_answers_a_sanitized_500_and_frees_the_lock(
    store_kind: str,
) -> None:
    check_a_failed_second_read_answers_a_sanitized_500_and_frees_the_lock(StateHarness(store_kind))


@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_a_run_that_started_after_the_first_read_is_refused_and_keeps_its_owner(
    store_kind: str,
) -> None:
    check_a_run_that_started_after_the_first_read_is_refused_and_keeps_its_owner(
        StateHarness(store_kind)
    )
