from threading import Event
from unittest.mock import Mock, patch

import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.platform import _runs, _store_renewal
from tests.conftest import FakeCompiledGraph
from tests.test_platform_routes import _get_fn, _post_request
from tests.test_stores_azure_table import _new_store


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("failure", [False, RuntimeError("private-renewal-canary"), True])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "superseded"])
def test_run_fails_when_renewal_loses_ownership(
    endpoint: str, failure: bool | Exception, outcome: str
) -> None:
    # Given a Table-backed run whose renewal fails while graph execution is active.
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
    active = Event()
    primary = RuntimeError("private-graph-canary")
    cancellation = KeyboardInterrupt()
    newer_owner: list[str | None] = []

    def renew(thread_id: str, owner_token: str) -> bool:
        assert active.wait(2)
        if isinstance(failure, Exception):
            raise failure
        return failure

    def execute(*args: object, **kwargs: object) -> dict[str, bool]:
        active.set()
        renewal = next(iter(_runs._STORE_RENEWALS.values()))
        assert renewal.worker is not None
        if failure is True:
            renewal.stop.set()
        renewal.worker.join(2)
        assert not renewal.worker.is_alive()
        if outcome == "superseded":
            store.update(thread.thread_id, status="idle")
            locked = store.try_acquire_run_lock(thread.thread_id)
            assert locked is not None
            newer_owner.append(locked.lock_token)
        if outcome == "error":
            raise primary
        if outcome == "cancel":
            raise cancellation
        return {"after": True}

    # When the actual daemon executes one renewal, without the 20-second delay.
    with (
        patch.object(_store_renewal, "RENEWAL_INTERVAL", 0),
        patch.object(store, "renew_run_lock", side_effect=renew),
        patch.object(graph, "invoke", side_effect=execute),
        patch.object(graph, "stream", side_effect=lambda *a, **kw: iter([execute()])),
    ):
        if outcome == "cancel":
            with pytest.raises(KeyboardInterrupt) as caught:
                handler(request)
            assert caught.value is cancellation
            response = None
        else:
            response = handler(request)

    # Then no exclusive success or untrusted final values are published.
    if response is not None:
        if outcome == "error" and failure is True and endpoint == "stream":
            assert b"event: error" in response.get_body()
        else:
            assert response.status_code == (200 if failure is True and outcome != "error" else 500)
        assert b"private-" not in response.get_body()
    if outcome == "cancel":
        observer.on_run_completed.assert_not_called()
        observer.on_run_failed.assert_not_called()
    elif failure is True and outcome != "error":
        observer.on_run_completed.assert_called_once()
        observer.on_run_failed.assert_not_called()
    else:
        observer.on_run_completed.assert_not_called()
        observer.on_run_failed.assert_called_once()
    if outcome == "error":
        assert observer.on_run_failed.call_args.args[1] is primary
    elif isinstance(failure, Exception) and outcome != "cancel":
        assert observer.on_run_failed.call_args.args[1] is failure
    stored = store.get(thread.thread_id)
    assert stored is not None
    if newer_owner:
        assert stored.status == "busy"
        assert table.entities[("thread", thread.thread_id)]["lock_token"] == newer_owner[0]
    else:
        assert stored.status == ("idle" if failure is True and outcome == "success" else "error")
    expected_values = (
        {"after": True}
        if failure is True and endpoint == "wait" and outcome == "success"
        else {"before": True}
    )
    assert stored.values == expected_values
    assert not _runs._STORE_RENEWALS
    token = lock.acquire("agent", thread.thread_id)
    assert token is not None
    lock.release("agent", thread.thread_id, token)
