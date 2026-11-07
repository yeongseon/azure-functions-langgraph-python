"""POST /threads/{id}/state must release the outer thread lock when store acquisition raises (#807).

``threads_state_update`` takes the shared ``ThreadLock`` first and the store run lock second. Only
``KeyError`` and ``ValueError`` used to release the outer lock; any other storage error (a timeout,
a transport fault, a bug) left the per-thread lock held, blocking later requests on that thread and
escaping as a raw exception instead of a sanitized 5xx.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import patch

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from tests.conftest import FakeStatefulGraph
from tests.test_platform_routes import _get_fn, _post_request
from tests.test_stores_azure_table import _new_store

SECRET = "private-storage-canary"


class RecordingLock(InProcessThreadLock):
    """A real in-process lock that records every acquisition and release call."""

    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.fail_release = False

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        token = super().acquire(graph_name, thread_id, timeout)
        if token is not None:
            self.acquired.append(token)
        return token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.released.append(token)
        super().release(graph_name, thread_id, token)
        if self.fail_release:
            raise OSError("private-outer-canary")


class Scenario:
    """A Platform app over a real Table-model store and a real in-process lock."""

    def __init__(self) -> None:
        self.store, self.table = _new_store()
        self.lock = RecordingLock()
        self.graph = FakeStatefulGraph()
        self.app = LangGraphApp(platform_compat=True, thread_lock=self.lock)
        self.app._thread_store = self.store
        self.app.register(graph=self.graph, name="agent")
        thread = self.store.create()
        self.store.update(thread.thread_id, assistant_id="agent")
        self.thread_id = thread.thread_id
        self.handler = _get_fn(self.app.function_app, "aflg_platform_threads_state_update")
        self.update_calls: list[Any] = []
        original = self.graph.update_state

        def recording_update(*args: Any, **kwargs: Any) -> Any:
            self.update_calls.append(args)
            return original(*args, **kwargs)

        self.graph.update_state = recording_update  # type: ignore[method-assign]

    def post(self) -> func.HttpResponse:
        request = _post_request(
            "/api/threads/state", {"values": {"k": 1}}, thread_id=self.thread_id
        )
        return cast(func.HttpResponse, self.handler(request))


@pytest.mark.parametrize("error", [TimeoutError, OSError, RuntimeError, ConnectionError])
def test_unexpected_store_acquisition_error_releases_the_outer_lock(
    error: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    # Given the store run-lock acquisition raising an unexpected storage error.
    s = Scenario()
    with patch.object(s.store, "try_acquire_run_lock", side_effect=error(SECRET)):
        # When a state update takes the outer lock and then hits that error.
        response = s.post()

    # Then it is a sanitized 5xx, the outer lock was released exactly once, and the graph
    # was never touched.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == {"detail": "Internal error updating thread state"}
    assert SECRET.encode() not in response.get_body()
    assert len(s.lock.acquired) == 1 and s.lock.released == s.lock.acquired
    assert s.update_calls == []
    # And the original error stays available to operators as the diagnostic cause.
    failures = [r for r in caplog.records if r.levelname == "ERROR" and r.exc_info is not None]
    assert any(r.exc_info and SECRET in str(r.exc_info[1]) for r in failures)


def test_the_thread_is_usable_again_right_after_a_failed_acquisition() -> None:
    # Given a failed state update whose store acquisition raised.
    s = Scenario()
    with patch.object(s.store, "try_acquire_run_lock", side_effect=TimeoutError(SECRET)):
        assert s.post().status_code == 500

    # When the next request on the same thread arrives with a healthy store.
    retry = s.post()

    # Then it is not blocked by a leaked outer lock, and it completes and cleans up.
    assert retry.status_code == 200
    assert len(s.lock.acquired) == 2 and s.lock.released == s.lock.acquired
    assert len(s.update_calls) == 1
    current = s.store.get(s.thread_id)
    assert current is not None and current.status == "idle"
    token = s.lock.acquire("agent", s.thread_id)
    assert token is not None


def test_a_failing_outer_release_does_not_escape_or_mask_the_storage_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a store error AND an outer lock whose release itself fails.
    s = Scenario()
    s.lock.fail_release = True
    with patch.object(s.store, "try_acquire_run_lock", side_effect=TimeoutError(SECRET)):
        # When the handler cleans up.
        response = s.post()

    # Then the handler still answers with the sanitized 5xx, the release was attempted once,
    # and both failures are logged for operators.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == {"detail": "Internal error updating thread state"}
    assert b"private-" not in response.get_body()
    assert s.lock.released == s.lock.acquired and len(s.lock.acquired) == 1
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "Unexpected error releasing thread lock" in messages
    assert any(r.exc_info and SECRET in str(r.exc_info[1]) for r in caplog.records)


@pytest.mark.parametrize(
    ("outcome", "status"),
    [(KeyError("gone"), 404), (ValueError("bound elsewhere"), 409), (None, 409)],
    ids=["missing-thread", "binding-mismatch", "contended"],
)
@pytest.mark.parametrize("release_fails", [False, True])
def test_expected_acquisition_outcomes_keep_their_status_and_always_release_once(
    outcome: Exception | None, status: int, release_fails: bool
) -> None:
    # Given each expected store outcome: not found, binding mismatch, or a contended run lock.
    s = Scenario()
    s.lock.fail_release = release_fails
    patched = (
        patch.object(s.store, "try_acquire_run_lock", side_effect=outcome)
        if outcome is not None
        else patch.object(s.store, "try_acquire_run_lock", return_value=None)
    )
    with patched:
        # When the state update runs.
        response = s.post()

    # Then the existing status code is preserved even if the outer release fails, the outer
    # lock is released exactly once, and no graph update happened.
    assert response.status_code == status
    assert len(s.lock.acquired) == 1 and s.lock.released == s.lock.acquired
    assert s.update_calls == []


def test_a_normal_state_update_is_unchanged() -> None:
    # Given a healthy store.
    s = Scenario()

    # When a state update runs end to end.
    response = s.post()

    # Then it succeeds, updates the graph once and releases both locks once.
    assert response.status_code == 200
    assert len(s.update_calls) == 1
    assert len(s.lock.acquired) == 1 and s.lock.released == s.lock.acquired
    current = s.store.get(s.thread_id)
    assert current is not None and current.status == "idle" and current.lock_token is None
