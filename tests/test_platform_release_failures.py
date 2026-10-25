import json
from unittest.mock import Mock, patch

import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from tests.conftest import FakeCompiledGraph
from tests.test_platform_routes import _get_fn, _post_request
from tests.test_stores_azure_table import _new_store


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("graph_fails", [False, True])
def test_release_failure_retains_primary_error_and_releases_outer_lock(
    endpoint: str, graph_fails: bool
) -> None:
    # Given an execution with a failing persistence boundary and optional graph failure.
    store, _ = _new_store()
    lock = InProcessThreadLock()
    observer = Mock()
    graph = FakeCompiledGraph()
    app = LangGraphApp(platform_compat=True, thread_lock=lock, observer=observer)
    app._thread_store = store
    app.register(graph=graph, name="agent")
    thread = store.create()
    handler = _get_fn(app.function_app, f"aflg_platform_runs_{endpoint}")
    request = _post_request(
        f"/api/threads/{thread.thread_id}/runs/{endpoint}",
        {"assistant_id": "agent", "input": {}},
        thread_id=thread.thread_id,
    )
    primary = RuntimeError("private-graph-canary")
    persistence = OSError("private-storage-canary")
    method = "invoke" if endpoint == "wait" else "stream"
    original = getattr(graph, method)
    with (
        patch.object(graph, method, side_effect=primary if graph_fails else original),
        patch.object(store, "release_run_lock", side_effect=persistence),
    ):
        # When the handler completes its cleanup.
        response = handler(request)

    # Then only failure is observable, the actual primary exception is retained, and exclusion ends.
    assert response.status_code == 500
    assert b"private-" not in response.get_body()
    expected = primary if graph_fails else persistence
    observer.on_run_failed.assert_called_once()
    assert observer.on_run_failed.call_args.args[1] is expected
    observer.on_run_completed.assert_not_called()
    if graph_fails:
        assert json.loads(response.get_body())["detail"] == "Graph execution failed"
    token = lock.acquire("agent", thread.thread_id)
    assert token is not None
    lock.release("agent", thread.thread_id, token)
