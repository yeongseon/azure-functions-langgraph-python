from collections.abc import Callable
import json
from typing import Literal, TypedDict
from unittest.mock import DEFAULT, Mock, patch

import azure.functions as func
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.platform.contracts import ThreadStatus
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore
from tests.conftest import FakeStatefulGraph
from tests.test_platform_routes import _get_fn, _get_request, _post_request
from tests.test_stores_azure_table import FakeResourceModifiedError, _new_store


class CounterState(TypedDict):
    count: int


def test_committed_update_requires_read_before_owner_fenced_recovery() -> None:
    # Given a real checkpointed graph and Table adapter with a failing release boundary.
    builder = StateGraph(CounterState)
    builder.add_node("counter", lambda state: {"count": state["count"]})
    builder.add_edge(START, "counter")
    builder.add_edge("counter", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    store, table = _new_store()
    lock = InProcessThreadLock()
    app = LangGraphApp(platform_compat=True, thread_lock=lock)
    app._thread_store = store
    app.register(graph=graph, name="agent")
    thread = store.create()
    store.update(thread.thread_id, assistant_id="agent")
    handler = _get_fn(app.function_app, "aflg_platform_threads_state_update")
    request = _post_request(
        "/api/threads/state", {"values": {"count": 1}}, thread_id=thread.thread_id
    )
    with (
        patch.object(store, "release_run_lock", side_effect=OSError("private-store-canary")),
        patch.object(lock, "release", wraps=lock.release) as outer_release,
    ):
        # When the state commits but release fails.
        response = handler(request)

    # Then failure is sanitized, and the committed state is read rather than blindly rewritten.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == {
        "detail": "Internal error releasing thread state lock"
    }
    outer_release.assert_called_once()
    retry = handler(request)
    assert retry.status_code == 409
    reader = _get_fn(app.function_app, "aflg_platform_threads_state_get")
    state = json.loads(
        reader(_get_request("/api/threads/state", thread_id=thread.thread_id)).get_body()
    )
    assert state["values"] == {"count": 1}
    current = store.get(thread.thread_id)
    assert current is not None and current.status == "busy"
    store.release_run_lock(
        thread.thread_id,
        status="idle",
        owner_token=table.entities[("thread", thread.thread_id)]["lock_token"],
    )
    token = lock.acquire("agent", thread.thread_id)
    assert token is not None
    lock.release("agent", thread.thread_id, token)
    assert store.get(thread.thread_id).status == "idle"
    assert graph.get_state({"configurable": {"thread_id": thread.thread_id}}).values == {"count": 1}


@pytest.mark.parametrize("prior_status", ["idle", "interrupted", "error"])
@pytest.mark.parametrize(
    "graph_error", [None, ValueError("missing checkpoint"), RuntimeError("private-graph-canary")]
)
@pytest.mark.parametrize("store_fails", [False, True])
@pytest.mark.parametrize("outer_fails", [False, True])
def test_cleanup_preserves_primary_response_and_releases_outer_once(
    prior_status: ThreadStatus,
    graph_error: ValueError | RuntimeError | None,
    store_fails: bool,
    outer_fails: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given failing update/store/outer boundaries; state updates do not emit run events.
    store, table = _new_store()
    lock = InProcessThreadLock()
    observer = Mock()
    graph = FakeStatefulGraph()
    app = LangGraphApp(platform_compat=True, thread_lock=lock, observer=observer)
    app._thread_store = store
    app.register(graph=graph, name="agent")
    thread = store.create()
    store.update(thread.thread_id, assistant_id="agent", status=prior_status)
    handler = _get_fn(app.function_app, "aflg_platform_threads_state_update")
    release = lock.release

    def release_outer(graph_name: str, thread_id: str, token: str) -> None:
        release(graph_name, thread_id, token)
        if outer_fails:
            raise OSError("private-outer-canary")

    with (
        patch.object(graph, "update_state", side_effect=graph_error or graph.update_state),
        patch.object(
            store,
            "release_run_lock",
            side_effect=OSError("private-store-canary") if store_fails else store.release_run_lock,
        ),
        patch.object(lock, "release", side_effect=release_outer) as outer_release,
    ):
        # When the registered HTTP handler updates and cleans up.
        response = handler(
            _post_request("/api/threads/state", {"values": {}}, thread_id=thread.thread_id)
        )

    # Then cleanup never overrides a primary update error or claims unqualified success.
    match graph_error:
        case ValueError():
            assert response.status_code == 404
            assert "missing checkpoint" in json.loads(response.get_body())["detail"]
        case RuntimeError():
            assert response.status_code == 500
            assert (
                json.loads(response.get_body())["detail"] == "Internal error updating thread state"
            )
        case None:
            assert response.status_code == (500 if store_fails or outer_fails else 200)
    assert b"private-" not in response.get_body()
    outer_release.assert_called_once()
    assert observer.mock_calls == []
    current = store.get(thread.thread_id)
    assert current.status == ("busy" if store_fails else "error" if graph_error else prior_status)
    assert ("lock_token" in table.entities[("thread", thread.thread_id)]) == store_fails
    failures = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(failures) == int(store_fails) + int(outer_fails) + int(
        isinstance(graph_error, RuntimeError)
    )


@pytest.mark.parametrize("boundary", ["deleted", "superseded", "cas_superseded", "cas_exhausted"])
@pytest.mark.parametrize("graph_fails", [False, True])
def test_cleanup_reports_owner_uncertainty_without_mutating_newer_owner(
    boundary: Literal["deleted", "superseded", "cas_superseded", "cas_exhausted"],
    graph_fails: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given the actual Table adapter over a deterministic fake service (not Azure certification).
    store: AzureTableThreadStore
    store, table = _new_store()
    lock = InProcessThreadLock()
    graph = FakeStatefulGraph()
    app = LangGraphApp(platform_compat=True, thread_lock=lock)
    app._thread_store = store
    app.register(graph=graph, name="agent")
    thread = store.create()
    store.update(thread.thread_id, assistant_id="agent")
    key = ("thread", thread.thread_id)
    newer: dict[str, object] = {}
    update = table.update_entity

    def replace_owner() -> None:
        store.update(thread.thread_id, status="idle", metadata={"newer": True})
        store.try_acquire_run_lock(thread.thread_id, assistant_id="agent")
        newer.update(table.entities[key])

    def update_state(*args: object, **kwargs: object) -> dict[str, object]:
        match boundary:
            case "deleted":
                store.delete(thread.thread_id)
            case "superseded":
                replace_owner()
            case "cas_superseded" | "cas_exhausted":
                pass
        if graph_fails:
            raise RuntimeError("private-graph-canary")
        return {"configurable": {"checkpoint_id": "committed"}}

    def conflict(*args: object, **kwargs: object) -> object:
        if table.entities[key]["status"] != "busy":
            return DEFAULT
        match boundary:
            case "cas_superseded":
                with patch.object(table, "update_entity", update):
                    replace_owner()
                raise FakeResourceModifiedError("private-cas-canary")
            case "cas_exhausted":
                raise FakeResourceModifiedError("private-cas-canary")
            case "deleted" | "superseded":
                return DEFAULT

    handler: Callable[[func.HttpRequest], func.HttpResponse] = _get_fn(
        app.function_app, "aflg_platform_threads_state_update"
    )
    with (
        patch.object(graph, "update_state", side_effect=update_state),
        patch.object(table, "update_entity", side_effect=conflict, wraps=update),
        patch.object(lock, "release", wraps=lock.release) as outer_release,
    ):
        # When cleanup observes deletion, stale ownership or repeated CAS conflicts.
        response = handler(
            _post_request("/api/threads/state", {"values": {}}, thread_id=thread.thread_id)
        )

    # Then only sanitized failure is returned and the newer owner remains byte-for-byte unchanged.
    assert response.status_code == 500
    assert json.loads(response.get_body())["detail"] == (
        "Internal error updating thread state"
        if graph_fails
        else "Internal error releasing thread state lock"
    )
    outer_release.assert_called_once()
    if newer:
        assert table.entities[key] == newer
    if boundary == "deleted":
        assert key not in table.entities
        assert any(record.levelname == "WARNING" for record in caplog.records)
