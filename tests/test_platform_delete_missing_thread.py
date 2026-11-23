"""DELETE /threads/{id} answers 404 for a missing thread with either shipped store (#934)."""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

import pytest

from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from tests.conftest import FakeCompiledGraph
from tests.test_platform_routes import _build_platform_app, _delete_request, _get_fn
from tests.test_stores_azure_table import _new_store


def _in_memory() -> Any:
    return InMemoryThreadStore()


def _azure_table() -> Any:
    store, _ = _new_store()
    return store


@pytest.fixture(params=[_in_memory, _azure_table], ids=["in-memory", "azure-table"])
def delete_handler_and_store(request: pytest.FixtureRequest) -> tuple[Callable[..., Any], Any]:
    make_store: Callable[[], Any] = request.param
    store = make_store()
    app = _build_platform_app(graphs={"agent": FakeCompiledGraph()}, store=store)
    return _get_fn(app.function_app, "aflg_platform_threads_delete"), store


def test_a_missing_thread_answers_404(
    delete_handler_and_store: tuple[Callable[..., Any], Any],
) -> None:
    # Given: a Platform app on top of the store and a thread id that never existed
    handler, _ = delete_handler_and_store

    # When: the thread is deleted
    response = handler(_delete_request("/api/threads/never-existed", thread_id="never-existed"))

    # Then: the answer is the standard 404, not a success
    assert response.status_code == 404
    assert "not found" in json.loads(response.get_body())["detail"]


def test_an_existing_thread_is_deleted_once_and_then_answers_404(
    delete_handler_and_store: tuple[Callable[..., Any], Any],
) -> None:
    # Given: an existing thread
    handler, store = delete_handler_and_store
    thread = store.create()
    request = _delete_request(f"/api/threads/{thread.thread_id}", thread_id=thread.thread_id)

    # When: it is deleted twice
    first = handler(request)
    second = handler(request)

    # Then: the first delete succeeds, the thread is gone, and the second answers 404
    assert first.status_code == 204
    assert store.get(thread.thread_id) is None
    assert second.status_code == 404
