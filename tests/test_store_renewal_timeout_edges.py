"""Edge cases for the bounded Platform renewal-worker shutdown (#766)."""

import copy
import json
import threading
from threading import Event, Thread
from typing import Any
from unittest.mock import Mock, patch

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.platform import _common, _runs, _store_renewal
from azure_functions_langgraph.platform._common import PlatformRouteDeps
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from tests.conftest import FakeCompiledGraph
from tests.test_platform_routes import _get_fn, _post_request
from tests.test_stores_azure_table import _new_store


def _deps(store: object) -> PlatformRouteDeps:
    return PlatformRouteDeps(
        registrations={},
        thread_store=store,  # type: ignore[arg-type]
        thread_lock=InProcessThreadLock(),
        auth_level=func.AuthLevel.FUNCTION,
        max_stream_response_bytes=1024,
    )


def _store_renewal_threads() -> list[Thread]:
    # Renewal workers are the only threads whose target is the renewal closure.
    return [
        t
        for t in threading.enumerate()
        if t.is_alive()
        and getattr(t, "_target", None) is not None
        and getattr(t._target, "__qualname__", "").startswith("_start_store_renewal")  # type: ignore[attr-defined]
    ]


def _route_setup(endpoint: str) -> tuple[Any, ...]:
    store, table = _new_store()
    lock = InProcessThreadLock()
    observer = Mock()
    graph = FakeCompiledGraph()
    app = LangGraphApp(platform_compat=True, thread_lock=lock, observer=observer)
    app._thread_store = store
    app.register(graph=graph, name="agent")
    thread = store.create()
    store.update(thread.thread_id, values={"before": True})
    handler = _get_fn(app.function_app, f"aflg_platform_runs_{endpoint}")
    request = _post_request(
        f"/api/threads/{thread.thread_id}/runs/{endpoint}",
        {"assistant_id": "agent", "input": {}},
        thread_id=thread.thread_id,
    )
    return store, table, lock, observer, graph, thread, handler, request


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_slow_successful_renewal_is_terminal_error_and_values_dropped(endpoint: str) -> None:
    # Given a renewal call that exceeds the stop bound but would later succeed.
    store, _, lock, observer, graph, thread, handler, request = _route_setup(endpoint)
    entered, unblock = Event(), Event()

    def renew(thread_id: str, owner_token: str) -> bool:
        entered.set()
        unblock.wait(5)
        return True

    def execute(*args: object, **kwargs: object) -> dict[str, bool]:
        assert entered.wait(2)
        return {"after": True}

    with (
        patch.object(_store_renewal, "RENEWAL_INTERVAL", 0),
        patch.object(_store_renewal, "RENEWAL_STOP_TIMEOUT", 0.05),
        patch.object(store, "renew_run_lock", side_effect=renew),
        patch.object(graph, "invoke", side_effect=execute),
        patch.object(graph, "stream", side_effect=lambda *a, **kw: iter([execute()])),
    ):
        # When cleanup gives up on the blocked worker.
        response = handler(request)
        unblock.set()

    # Then the run fails with a sanitized 500, final values are not persisted,
    # the observer fires, and the outer lock is free.
    assert response.status_code == 500
    body = response.get_body().decode()
    assert thread.thread_id not in body
    observer.on_run_failed.assert_called_once()
    assert isinstance(
        observer.on_run_failed.call_args.args[1], _store_renewal.RunLockRenewalStopTimeoutError
    )
    observer.on_run_completed.assert_not_called()
    stored = store.get(thread.thread_id)
    assert stored is not None
    assert stored.status == "error"
    assert stored.values == {"before": True}
    assert not _runs._STORE_RENEWALS
    token = lock.acquire("agent", thread.thread_id)
    assert token is not None
    lock.release("agent", thread.thread_id, token)


def test_release_failure_after_stop_timeout_is_reported_and_outer_lock_released() -> None:
    # Given a stop timeout followed by a failing store release.
    store, _, lock, observer, graph, thread, handler, request = _route_setup("wait")
    entered, unblock = Event(), Event()

    def renew(thread_id: str, owner_token: str) -> bool:
        entered.set()
        unblock.wait(5)
        return True

    def execute(*args: object, **kwargs: object) -> dict[str, bool]:
        assert entered.wait(2)
        return {"after": True}

    with (
        patch.object(_store_renewal, "RENEWAL_INTERVAL", 0),
        patch.object(_store_renewal, "RENEWAL_STOP_TIMEOUT", 0.05),
        patch.object(store, "renew_run_lock", side_effect=renew),
        patch.object(store, "release_run_lock", side_effect=OSError("private-storage-canary")),
        patch.object(graph, "invoke", side_effect=execute),
    ):
        # When the handler cleans up.
        response = handler(request)
        unblock.set()

    # Then a sanitized failure is reported and local exclusion still ends.
    assert response.status_code == 500
    assert b"private-" not in response.get_body()
    observer.on_run_failed.assert_called_once()
    token = lock.acquire("agent", thread.thread_id)
    assert token is not None
    lock.release("agent", thread.thread_id, token)


def test_repeated_timeouts_leave_at_most_one_stuck_worker_per_run() -> None:
    # Given several runs whose renewal call never returns until released.
    store = InMemoryThreadStore()
    deps = _deps(store)
    unblock = Event()
    entered = [Event() for _ in range(3)]
    for stale in _store_renewal_threads():  # workers abandoned by earlier tests
        stale.join(2)

    def renew(thread_id: str, owner_token: str) -> bool:
        entered[int(thread_id)].set()
        unblock.wait(5)
        return True

    workers: list[Thread] = []
    with (
        patch.object(_store_renewal, "RENEWAL_INTERVAL", 0),
        patch.object(_store_renewal, "RENEWAL_STOP_TIMEOUT", 0.02),
        patch.object(store, "renew_run_lock", side_effect=renew),
    ):
        for i in range(3):
            # When each run is started and then cleaned up.
            _store_renewal._start_store_renewal(deps, str(i), f"owner{i}")
            assert entered[i].wait(2)
            worker = _store_renewal._STORE_RENEWALS[f"owner{i}"].worker
            assert worker is not None
            workers.append(worker)
            result = _store_renewal.stop_store_renewal(f"owner{i}")
            assert isinstance(result, _store_renewal.RunLockRenewalStopTimeoutError)
            # Then exactly one stuck worker per run exists; no replacements spawn.
            assert sum(w.is_alive() for w in workers) == i + 1
            assert len(_store_renewal_threads()) == i + 1
        unblock.set()
        for worker in workers:
            worker.join(2)

    # And all abandoned workers exit once their calls return, leaving no registry entries.
    assert not _store_renewal_threads()
    assert not _store_renewal._STORE_RENEWALS


def test_late_renew_cannot_mutate_newer_owner_in_table_store() -> None:
    # Given a newer owner that acquired the thread after the first run was abandoned.
    store, table = _new_store()
    thread = store.create()
    first = store.try_acquire_run_lock(thread.thread_id)
    assert first is not None and first.lock_token is not None
    store.release_run_lock(thread.thread_id, status="idle", owner_token=first.lock_token)
    newer = store.try_acquire_run_lock(thread.thread_id)
    assert newer is not None and newer.lock_token is not None
    before = copy.deepcopy(table.entities[("thread", thread.thread_id)])

    # When the abandoned worker's late renew arrives.
    renewed = store.renew_run_lock(thread.thread_id, first.lock_token)

    # Then it is rejected and the newer owner's record is unchanged.
    assert renewed is False
    assert table.entities[("thread", thread.thread_id)] == before
    assert before["lock_token"] == newer.lock_token


def test_late_renew_cannot_mutate_newer_owner_in_memory_store() -> None:
    # Given the same ownership hand-over on the in-memory store.
    store = InMemoryThreadStore()
    thread = store.create()
    first = store.try_acquire_run_lock(thread.thread_id)
    assert first is not None and first.lock_token is not None
    store.release_run_lock(thread.thread_id, status="idle", owner_token=first.lock_token)
    newer = store.try_acquire_run_lock(thread.thread_id)
    assert newer is not None
    before = store.get(thread.thread_id)

    # When the abandoned worker's late renew arrives.
    renewed = store.renew_run_lock(thread.thread_id, first.lock_token)

    # Then it is rejected and the newer owner's record is unchanged.
    assert renewed is False
    assert store.get(thread.thread_id) == before


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
def test_never_returning_renewal_yields_sanitized_500(endpoint: str) -> None:
    # Given a renewal call that never returns during the whole request.
    store, _, _, observer, graph, thread, handler, request = _route_setup(endpoint)
    entered, unblock = Event(), Event()

    def renew(thread_id: str, owner_token: str) -> bool:
        entered.set()
        unblock.wait(5)
        raise OSError("private-late-canary")

    def execute(*args: object, **kwargs: object) -> dict[str, bool]:
        assert entered.wait(2)
        return {"after": True}

    with (
        patch.object(_store_renewal, "RENEWAL_INTERVAL", 0),
        patch.object(_store_renewal, "RENEWAL_STOP_TIMEOUT", 0.05),
        patch.object(store, "renew_run_lock", side_effect=renew),
        patch.object(graph, "invoke", side_effect=execute),
        patch.object(graph, "stream", side_effect=lambda *a, **kw: iter([execute()])),
        patch.object(_common.logger, "error") as log_error,
    ):
        # When the handler finishes.
        response = handler(request)
        unblock.set()

    # Then the body and logs carry no exception text or owner/thread identifiers.
    assert response.status_code == 500
    text = response.get_body().decode()
    assert "private-" not in text and thread.thread_id not in text
    assert json.loads(text)
    stored = store.get(thread.thread_id)
    assert stored is not None
    owner_tokens = [c.args for c in log_error.call_args_list]
    assert all("private-" not in str(a) for a in owner_tokens)
    observer.on_run_failed.assert_called_once()


def test_adapter_reference_documents_bounded_renewal_stop() -> None:
    # Given the Platform run-lock documentation.
    from pathlib import Path

    text = " ".join(Path("docs/adapter-reference.md").read_text().split())

    # When read for the renewal shutdown contract.
    # Then it names the fixed bound and terminal error, not an unbounded wait.
    assert "RENEWAL_STOP_TIMEOUT" in text
    assert "RunLockRenewalStopTimeoutError" in text
    assert "must wait for any in-flight renewal I/O" not in text
    assert "Cleanup stops and joins the renewal worker" not in text
