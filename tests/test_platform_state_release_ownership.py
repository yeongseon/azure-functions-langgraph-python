"""A state update must not report success when its own run-lock release was refused as stale (#836).

``POST /threads/{id}/state`` takes the shared ``ThreadLock``, then the store run lock, runs
``graph.update_state`` and releases both. ``thread_store.release_run_lock`` deliberately does not
raise for a stale owner token: it leaves the row alone and returns the unchanged current thread.
``_release_thread_state_lock`` only rejected a thread that was still ``busy`` or still carried a
lock token, so an update whose lock was reclaimed by ``reset_stale_locks`` (to a terminal status,
with the token removed since #808), or that another run acquired and finished first, still
answered 200 with a checkpoint although the store had ignored its release. #804 and #815 closed
the same gap for runs.

Fake-backed: ownership is stolen from inside ``update_state``, which runs after the lock was taken
and before the handler's cleanup. A state update starts no renewal worker, so nothing refreshes the
row while it runs.

The scenarios are plain ``check_*`` functions over a ``StateHarness`` so that the Azurite lane
(``tests/integration/test_table_store_integration.py``) can run the very same assertions through a
real Table service; the tests below run them over the in-process stores.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any, Literal, cast

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.platform.contracts import Thread, ThreadStatus
from azure_functions_langgraph.platform.stores import InMemoryThreadStore, ReleaseOutcomeProvider
from tests.conftest import FakeStatefulGraph
from tests.ownership_guard_support import RecordingObserver
from tests.test_platform_routes import _get_fn, _post_request
from tests.test_platform_run_release_ownership import (
    PlainStore,
    RecordingLock,
    _ScriptedOutcomeStore,
)
from tests.test_stores_azure_table import _new_store

SECRET = "private-graph-canary"
RELEASE_FAILURE = {"detail": "Internal error releasing thread state lock"}
UPDATE_FAILURE = {"detail": "Internal error updating thread state"}
ResetTo = Literal["idle", "error"]


class HookedStateGraph(FakeStatefulGraph):
    """A stateful graph whose ``update_state`` runs ``hook`` first, then behaves like the fake."""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Callable[[], None] | None = None
        self.error: Exception | None = None
        self.update_calls = 0

    def update_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any] | list[dict[str, Any]] | None,
        *,
        as_node: str | None = None,
    ) -> dict[str, Any]:
        self.update_calls += 1
        if self.hook is not None:
            self.hook()
        if self.error is not None:
            raise self.error
        return super().update_state(config, values, as_node=as_node)


class StateHarness:
    """A Platform app over a chosen store, with ownership stolen from inside ``update_state``."""

    def __init__(
        self,
        kind: str,
        store: Any = None,
        table: Any = None,
        prior_status: ThreadStatus = "idle",
    ) -> None:
        """``kind``: ``"memory"`` or ``"table"``; ``store``/``table`` plug in a real service."""
        self.store: Any
        self.table: Any
        if store is not None:
            self.store, self.table = store, table
        elif kind == "memory":
            self.store, self.table = InMemoryThreadStore(), None
        else:
            self.store, self.table = _new_store()
        self.prior_status = prior_status
        self.lock = RecordingLock()
        self.observer = RecordingObserver()
        self.graph = HookedStateGraph()
        self.app = LangGraphApp(platform_compat=True, thread_lock=self.lock, observer=self.observer)
        self.app._thread_store = self.store
        self.app.register(graph=self.graph, name="agent")
        self.thread_id = self.store.create().thread_id
        self.store.update(self.thread_id, assistant_id="agent", status=prior_status)
        self.new_owner_token: str | None = None
        self.steals = 0
        self.before_steal: Thread | None = None
        self.after_steal: Thread | None = None

    def steal(
        self,
        *,
        new_owner: bool,
        reset_to: ResetTo = "error",
        new_owner_finishes: dict[str, Any] | None = None,
    ) -> None:
        """Ownership is lost while ``update_state`` runs: reclaimed, optionally re-acquired.

        With ``new_owner_finishes`` the new owner also runs to completion (releases to ``idle``
        with those values) before the stale update gets to release. The row just before and just
        after is kept, so a test can prove that the loss happened and what it left behind.
        """
        self.steals += 1
        self.before_steal = self.store.get(self.thread_id)
        if self.table is None:
            self.store.release_run_lock(self.thread_id, status=reset_to)
        else:
            # A plain merge works on the in-process Table model and on a real service alike.
            self.table.update_entity(
                {
                    "PartitionKey": "thread",
                    "RowKey": self.thread_id,
                    "updated_at": datetime.now(UTC) - timedelta(seconds=900),
                },
                mode="merge",
            )
            assert self.store.reset_stale_locks(older_than_seconds=300, status=reset_to) == 1
        if new_owner:
            owner = self.store.try_acquire_run_lock(self.thread_id)
            assert owner is not None and owner.lock_token is not None
            self.new_owner_token = owner.lock_token
            if new_owner_finishes is not None:
                finished = self.store.release_run_lock(
                    self.thread_id,
                    status="idle",
                    values=new_owner_finishes,
                    owner_token=owner.lock_token,
                )
                assert (finished.status, finished.lock_token) == ("idle", None)
        self.after_steal = self.store.get(self.thread_id)

    def update(self) -> func.HttpResponse:
        handler = _get_fn(self.app.function_app, "aflg_platform_threads_state_update")
        request = _post_request(
            f"/api/threads/{self.thread_id}/state",
            {"values": {"count": 1}},
            thread_id=self.thread_id,
        )
        return cast(func.HttpResponse, handler(request))


def assert_the_loss_happened_and_the_cleanup_changed_nothing(h: StateHarness) -> Thread:
    """The row is exactly as the ownership loss left it, so the update's cleanup wrote nothing.

    It also proves that the loss really happened inside ``update_state``, while the update held
    the lock: a hook that never fires cannot let a test pass for the wrong reason.
    """
    assert h.steals == 1
    assert h.before_steal is not None
    assert h.before_steal.status == "busy" and h.before_steal.lock_token is not None
    after: Thread | None = h.store.get(h.thread_id)
    assert after is not None and after == h.after_steal
    return after


def check_a_stale_release_fails_the_state_update(
    h: StateHarness, new_owner: bool, reset_to: ResetTo = "error"
) -> None:
    # Given an update that loses the thread after it took the lock and before its cleanup.
    h.graph.hook = lambda: h.steal(new_owner=new_owner, reset_to=reset_to)

    # When update_state returns and the handler releases with its old owner token.
    response = h.update()

    # Then the caller gets the sanitized release failure, not a 200 with a checkpoint.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == RELEASE_FAILURE
    # And the update itself ran once: the failure is about ownership, not a rollback.
    assert h.graph.update_calls == 1
    # And the other party's state is preserved: the reclaimed disposition or the new owner.
    after = assert_the_loss_happened_and_the_cleanup_changed_nothing(h)
    if new_owner:
        assert (after.status, after.lock_token) == ("busy", h.new_owner_token)
    else:
        assert (after.status, after.lock_token) == (reset_to, None)
    # And the outer thread lock is released exactly once, and no run event is emitted.
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired
    assert h.observer.events == []


def check_another_owner_finishing_first_does_not_hide_a_stale_state_update_release(
    h: StateHarness,
) -> None:
    # Given an update that loses the thread, and a newer owner that acquires it and runs to
    # completion before the stale update releases.
    h.graph.hook = lambda: h.steal(new_owner=True, new_owner_finishes={"newer": "values"})

    # When the stale update finally releases with its old owner token. The thread the store hands
    # back now looks like a normally finished one: idle, no lock token.
    response = h.update()

    # Then the release was still not applied, so the update is a failure, not a success.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == RELEASE_FAILURE
    # And the newer owner's result stands.
    after = assert_the_loss_happened_and_the_cleanup_changed_nothing(h)
    assert (after.status, after.lock_token, after.values) == ("idle", None, {"newer": "values"})
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


def check_the_new_owner_can_still_finish_after_a_stale_state_update_release_was_refused(
    h: StateHarness,
) -> None:
    # Given a stale release that was refused while a newer owner holds the thread.
    h.graph.hook = lambda: h.steal(new_owner=True)
    assert h.update().status_code == 500
    assert_the_loss_happened_and_the_cleanup_changed_nothing(h)
    assert h.new_owner_token is not None

    # When the newer owner releases with its own token and a result.
    released = h.store.release_run_lock(
        h.thread_id, status="idle", values={"new": 1}, owner_token=h.new_owner_token
    )

    # Then its lock, status and values were never disturbed by the stale update.
    assert (released.status, released.values, released.lock_token) == ("idle", {"new": 1}, None)


def check_an_ordinary_state_update_still_completes(h: StateHarness) -> None:
    # Given a healthy update.

    # When it commits and releases normally.
    response = h.update()

    # Then it is a success with its checkpoint, and the thread is back at its prior status.
    assert response.status_code == 200
    assert json.loads(response.get_body())["checkpoint"]["checkpoint_id"] == "new-checkpoint-id"
    after = h.store.get(h.thread_id)
    assert after is not None and (after.status, after.lock_token) == (h.prior_status, None)
    assert h.graph.update_calls == 1
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired
    assert h.observer.events == []


def check_a_state_update_graph_failure_stays_the_primary_error_when_the_release_is_also_stale(
    h: StateHarness, new_owner: bool
) -> None:
    # Given an update whose graph fails after it already lost the thread.
    h.graph.hook = lambda: h.steal(new_owner=new_owner)
    h.graph.error = RuntimeError(SECRET)

    # When the handler cleans up. Its release is refused too, but only the update error is
    # reported to the caller.
    response = h.update()

    # Then the graph error is the one failure, sanitized, and nothing of the other party changed.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == UPDATE_FAILURE
    assert SECRET.encode() not in response.get_body()
    after = assert_the_loss_happened_and_the_cleanup_changed_nothing(h)
    if new_owner:
        assert (after.status, after.lock_token) == ("busy", h.new_owner_token)
    else:
        assert (after.status, after.lock_token) == ("error", None)
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


PRIOR_STATUSES = ["idle", "interrupted", "error"]
RESET_TARGETS = ["error", "idle"]


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("prior_status", PRIOR_STATUSES)
@pytest.mark.parametrize("reset_to", RESET_TARGETS)
def test_a_reclaimed_state_update_is_a_failure_not_a_success(
    store_kind: str, prior_status: ThreadStatus, reset_to: ResetTo
) -> None:
    # The reclaim leaves a thread that is neither busy nor tokened, at "error" or at "idle": the
    # pairs (idle, idle) and (error, error) are exactly what an applied release would return.
    check_a_stale_release_fails_the_state_update(
        StateHarness(store_kind, prior_status=prior_status), new_owner=False, reset_to=reset_to
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("prior_status", PRIOR_STATUSES)
def test_a_state_update_that_lost_the_thread_to_a_new_owner_is_a_failure(
    store_kind: str, prior_status: ThreadStatus
) -> None:
    check_a_stale_release_fails_the_state_update(
        StateHarness(store_kind, prior_status=prior_status), new_owner=True
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("prior_status", PRIOR_STATUSES)
def test_another_owner_finishing_first_does_not_hide_a_stale_release(
    store_kind: str, prior_status: ThreadStatus
) -> None:
    check_another_owner_finishing_first_does_not_hide_a_stale_state_update_release(
        StateHarness(store_kind, prior_status=prior_status)
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
def test_the_new_owner_can_still_finish_after_the_stale_release_was_refused(
    store_kind: str,
) -> None:
    check_the_new_owner_can_still_finish_after_a_stale_state_update_release_was_refused(
        StateHarness(store_kind)
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("prior_status", PRIOR_STATUSES)
def test_an_ordinary_state_update_still_completes(
    store_kind: str, prior_status: ThreadStatus
) -> None:
    check_an_ordinary_state_update_still_completes(
        StateHarness(store_kind, prior_status=prior_status)
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("new_owner", [False, True], ids=["reclaimed", "reacquired"])
def test_a_graph_failure_stays_the_primary_error_when_the_release_is_also_stale(
    store_kind: str, new_owner: bool
) -> None:
    check_a_state_update_graph_failure_stays_the_primary_error_when_the_release_is_also_stale(
        StateHarness(store_kind), new_owner
    )


def test_a_refused_release_is_logged_with_its_reason_and_nothing_private(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given an update that lost the thread to a reclaim.
    h = StateHarness("memory")
    h.graph.hook = lambda: h.steal(new_owner=False)

    # When its release is refused.
    with caplog.at_level(logging.WARNING):
        response = h.update()

    # Then the operator can read why in the log, and the caller gets nothing but the sanitized 500.
    assert response.status_code == 500
    assert_the_loss_happened_and_the_cleanup_changed_nothing(h)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(h.thread_id in m and "ignored" in m and "no longer owned" in m for m in messages)
    assert b"ignored" not in response.get_body()


def _plain_harness(kind: str, prior_status: ThreadStatus = "idle") -> StateHarness:
    """A custom store that implements only the ``ThreadStore`` protocol: no release outcome."""
    inner: Any
    table: Any = None
    if kind == "memory":
        inner = InMemoryThreadStore()
    else:
        inner, table = _new_store()
    plain = PlainStore(inner)
    assert not isinstance(plain, ReleaseOutcomeProvider)
    return StateHarness(kind, plain, table, prior_status)


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize(
    ("prior_status", "reset_to", "new_owner"),
    [
        pytest.param("idle", "error", False, id="reclaimed-to-another-status"),
        pytest.param("interrupted", "error", False, id="reclaimed-from-interrupted"),
        pytest.param("error", "idle", False, id="reclaimed-to-idle-from-error"),
        pytest.param("idle", "error", True, id="reacquired"),
    ],
)
def test_a_store_without_the_outcome_still_rejects_what_the_returned_thread_exposes(
    store_kind: str, prior_status: ThreadStatus, reset_to: ResetTo, new_owner: bool
) -> None:
    # A reclaim that left a status other than the requested one, and a thread busy under a new
    # owner, both differ from what the update asked for, so the returned thread still exposes them.
    check_a_stale_release_fails_the_state_update(
        _plain_harness(store_kind, prior_status), new_owner, reset_to
    )


@pytest.mark.parametrize("store_kind", ["memory", "table"])
@pytest.mark.parametrize("prior_status", PRIOR_STATUSES)
def test_a_store_without_the_outcome_still_completes_ordinary_updates(
    store_kind: str, prior_status: ThreadStatus
) -> None:
    check_an_ordinary_state_update_still_completes(_plain_harness(store_kind, prior_status))


def test_a_store_without_the_outcome_cannot_see_a_same_status_reclaim_documented_limit() -> None:
    # Given an update on a thread at "error" that is reclaimed to "error" while it runs, on a store
    # that cannot say whether its release was applied.
    h = _plain_harness("memory", "error")
    h.graph.hook = lambda: h.steal(new_owner=False, reset_to="error")

    # When the update releases: the returned thread is "error" with no token, exactly what an
    # applied release returns, and nothing else is known.
    response = h.update()

    # Then it is reported as a success although the store ignored the release: the row is exactly
    # as the reclaim left it. This is the documented limit of the fallback; a store that
    # implements the outcome closes it (see the reclaimed test above).
    assert response.status_code == 200
    assert_the_loss_happened_and_the_cleanup_changed_nothing(h)


def test_a_store_without_the_outcome_cannot_see_a_finished_new_owner_documented_limit() -> None:
    # Given the finished-first ordering on a store that cannot say whether its release was applied.
    h = _plain_harness("memory", "idle")
    h.graph.hook = lambda: h.steal(new_owner=True, new_owner_finishes={"newer": "values"})

    # When the stale update releases: idle with no token, the status it asked for.
    response = h.update()

    # Then it is reported as a success, the documented limit of the fallback, although the store
    # ignored the release and the newer owner's result stands.
    assert response.status_code == 200
    after = assert_the_loss_happened_and_the_cleanup_changed_nothing(h)
    assert after.values == {"newer": "values"}


@pytest.mark.parametrize(
    ("applied", "override"),
    [
        (False, None),
        (True, {"status": "busy"}),
        (True, {"lock_token": "left-behind"}),
        (True, {"status": "error"}),
    ],
    ids=[
        "reported-not-applied",
        "applied-but-busy",
        "applied-but-token-left",
        "applied-but-status",
    ],
)
def test_the_reported_outcome_and_the_returned_thread_must_both_say_success(
    applied: bool, override: dict[str, Any] | None
) -> None:
    # Given a store that reports its own outcome: a refusal beside a clean-looking thread, or an
    # "applied" beside a thread that contradicts it.
    scripted = _ScriptedOutcomeStore(
        InMemoryThreadStore(), applied=applied, thread_override=override
    )
    assert isinstance(scripted, ReleaseOutcomeProvider)
    h = StateHarness("memory", scripted)

    # When the update commits and releases.
    response = h.update()

    # Then either signal alone is enough to fail it, and the outer lock is still released once.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == RELEASE_FAILURE
    assert len(h.lock.acquired) == 1 and h.lock.released == h.lock.acquired


class AuditingStore(InMemoryThreadStore):
    """A bundled-store subclass that audits every release through the overridden method."""

    def __init__(self) -> None:
        super().__init__()
        self.releases: list[tuple[str, str | None]] = []

    def release_run_lock(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, Any] | None = None,
        owner_token: str | None = None,
    ) -> Thread:
        self.releases.append((status, owner_token))
        return super().release_run_lock(
            thread_id, status=status, values=values, owner_token=owner_token
        )


def test_an_overridden_release_run_lock_is_still_the_call_the_handler_reaches() -> None:
    # Given a store subclass that overrides release_run_lock (the pattern the platform tests use
    # for fault injection), and an update on an interrupted thread.
    store = AuditingStore()
    h = StateHarness("memory", store, prior_status="interrupted")

    # When the update commits and releases.
    response = h.update()

    # Then the override saw exactly one release, to the prior status, with a store owner token.
    assert response.status_code == 200
    assert [status for status, _ in store.releases] == ["interrupted"]
    assert store.releases[0][1] is not None


def test_a_release_that_leaves_the_lock_token_in_place_is_not_a_success() -> None:
    # Given a store whose release reports the requested terminal status but did not clear the
    # owner token (the condition the helper has always rejected).
    h = StateHarness("memory")
    real_release = h.store.release_run_lock

    def release_without_clearing(*args: Any, **kwargs: Any) -> Any:
        released = real_release(*args, **kwargs)
        return released.model_copy(update={"lock_token": "left-behind"})

    h.store.release_run_lock = release_without_clearing

    # When the update commits and the handler releases.
    response = h.update()

    # Then it is a failure, not a success.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == RELEASE_FAILURE
