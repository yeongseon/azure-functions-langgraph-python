"""Characterize observable stage ordering through real registered sync handlers.

Spies wrap real in-memory storage/locks; this is not Azure certification. Process
control cancellation intentionally propagates, with the current run cleanup.
"""

from collections.abc import Iterator
import json
from unittest.mock import DEFAULT, Mock

import azure.functions as func
import pytest

from azure_functions_langgraph.authorization import AuthorizationContext
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from tests.conftest import FakeCompiledGraph
from tests.test_platform_routes import _build_platform_app, _get_fn, _post_request


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("threaded", [True, False])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_run_stage_order_and_cleanup(
    endpoint: str, threaded: bool, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given real ownership with spies only at externally observable boundaries.
    events: list[str] = []
    store = InMemoryThreadStore(id_factory=lambda: "thread-0")
    store.create()
    lock = InProcessThreadLock()

    def authorize(context: AuthorizationContext) -> bool:
        events.append(f"auth:{context.graph_name}")
        return True

    graph = FakeCompiledGraph(invoke_result={"answer": 42})

    def invoke(*args: object, **kwargs: object) -> dict[str, int]:
        events.append("graph")
        if outcome == "cancel":
            raise KeyboardInterrupt
        if outcome == "error":
            raise RuntimeError("private-canary")
        return {"answer": 42}

    def stream(*args: object, **kwargs: object) -> Iterator[dict[str, int]]:
        try:
            yield invoke()
        finally:
            events.append("iterator_closed")

    monkeypatch.setattr(graph, "invoke", invoke)
    monkeypatch.setattr(graph, "stream", stream)
    app = _build_platform_app(graphs={"agent": graph}, store=store, authorize=authorize)
    app.thread_lock = lock
    handler = _get_fn(
        app.function_app, f"aflg_platform_runs_{endpoint}{'' if threaded else '_threadless'}"
    )
    calls = Mock()
    for target, attribute, name in [
        (store, "get", "lookup"),
        (lock, "acquire", "outer_acquire"),
        (store, "try_acquire_run_lock", "store_acquire"),
        (store, "release_run_lock_outcome", "store_release"),
        (lock, "release", "outer_release"),
    ]:
        original = getattr(target, attribute)

        def record(*args: object, _name: str = name, **kwargs: object) -> None:
            events.append(_name)

        spy = Mock(wraps=original, side_effect=None)
        spy.side_effect = lambda *a, _record=record, **kw: (_record(), DEFAULT)[1]
        monkeypatch.setattr(target, attribute, spy)
        calls.attach_mock(spy, name)
    request = func.HttpRequest(
        method="POST",
        url="/runs",
        body=b'{"assistant_id":"agent","input":{}}',
        headers={"X-Request-ID": "stage-request"},
        route_params={"thread_id": "thread-0"} if threaded else {},
    )

    # When the registered handler succeeds, fails, or receives process cancellation.
    if outcome == "cancel":
        with pytest.raises(KeyboardInterrupt):
            handler(request)
    else:
        response = handler(request)
        assert response.status_code == (500 if outcome == "error" and endpoint == "wait" else 200)
        if endpoint == "wait":
            assert json.loads(response.get_body()) == (
                {"answer": 42}
                if outcome == "success"
                else {"detail": "Graph execution failed", "request_id": "stage-request"}
            )
            assert response.mimetype == "application/json"
            if outcome == "error":
                assert response.headers["X-Request-ID"] == "stage-request"
        else:
            assert response.headers["Cache-Control"] == "no-cache"
            assert response.headers["X-Accel-Buffering"] == "no"
            assert response.mimetype == "text/event-stream"
            body = response.get_body()
            assert b"event: metadata" in body and b"event: end" in body
            data = [
                json.loads(line[6:]) for line in body.splitlines() if line.startswith(b"data: ")
            ]
            assert ({"answer": 42} in data) == (outcome == "success")
            assert (b"event: error" in body) == (outcome == "error")
            assert b"private-canary" not in body

    # Then graph-aware auth precedes lookup/acquisition and cleanup is inside-out.
    expected = ["auth:None", "auth:agent"]
    if threaded:
        expected += ["lookup", "outer_acquire", "store_acquire"]
    expected += ["graph"]
    if endpoint == "stream":
        expected += ["iterator_closed"]
    if threaded:
        expected += ["store_release", "outer_release"]
    assert events == expected
    if threaded:
        assert calls.store_release.call_args.kwargs["status"] == (
            "idle" if outcome == "success" else "error"
        )
        assert calls.store_release.call_args.kwargs["owner_token"]


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("assistant", ["agent", "unknown"])
def test_unknown_ids_preserve_authorization_and_error_precedence(
    endpoint: str, assistant: str
) -> None:
    # Given both an unknown thread and optionally an unknown assistant.
    seen: list[str | None] = []

    def authorize(context: AuthorizationContext) -> bool:
        seen.append(context.graph_name)
        return True

    app = _build_platform_app(graphs={"agent": FakeCompiledGraph()}, authorize=authorize)
    handler = _get_fn(app.function_app, f"aflg_platform_runs_{endpoint}")
    # When the request crosses the route's actual parsing and lookup stages.
    response = handler(_post_request("/runs", {"assistant_id": assistant}, thread_id="missing"))
    # Then assistant lookup wins over thread lookup, with no error-shape drift.
    assert response.status_code == 404
    assert json.loads(response.get_body()) == {
        "detail": "Thread 'missing' not found"
        if assistant == "agent"
        else "Assistant 'unknown' not found"
    }
    assert seen == ([None, "agent"] if assistant == "agent" else [None])


@pytest.mark.parametrize(
    "route", ["get", "update", "delete", "state_get", "state_update", "history"]
)
@pytest.mark.parametrize("allowed", [True, False])
def test_thread_preamble_precedes_parsing_and_lookup(route: str, allowed: bool) -> None:
    # Given an unknown thread, invalid JSON, and a recording ownership policy.
    seen: list[str] = []

    def authorize(context: AuthorizationContext) -> bool:
        seen.append(context.action.value)
        return allowed

    app = _build_platform_app(authorize=authorize)
    handler = _get_fn(app.function_app, f"aflg_platform_threads_{route}")
    request = func.HttpRequest(
        method="POST", url="/threads/missing", body=b"{", route_params={"thread_id": "missing"}
    )
    # When the registered thread route processes conflicting failures.
    response = handler(request)
    # Then denial wins, otherwise body parsing precedes lookup only on body routes.
    parses = route in {"update", "state_update", "history"}
    assert response.status_code == (403 if not allowed else 400 if parses else 404)
    detail = (
        "Access denied"
        if not allowed
        else "Invalid JSON body"
        if parses
        else "Thread 'missing' not found"
    )
    assert json.loads(response.get_body()) == {"detail": detail}
    assert len(seen) == 1
