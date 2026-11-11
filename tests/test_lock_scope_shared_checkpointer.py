"""Graphs that share a checkpointer share one thread-lock identity (#858).

Hold the lock through the app's own scoped lock under one graph name, then drive
another registration through every surface that takes the lock. A shared
checkpointer must reject (contend); separate checkpointers must stay independent.
No Azure access and no sleeps: all contention is deterministic.
"""

from __future__ import annotations

import json
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.locks._scope import resolve_lock_scope
from azure_functions_langgraph.locks.azure_blob import AzureBlobLeaseThreadLock
from azure_functions_langgraph.locks.guard import OwnershipGuardProvider
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from azure_functions_langgraph.triggers.service_bus import ThreadContentionError
from tests.ownership_guard_support import ProbeGraph
from tests.test_service_bus import FakeServiceBusMessage

THREAD_ID = "thread-1"


class KeyRecordingLock(InProcessThreadLock):
    """Real mutual exclusion that records the keys it was asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.keys: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.keys.append(graph_name)
        return super().acquire(graph_name, thread_id, timeout)


def _app(shared: bool, **kwargs: Any) -> tuple[LangGraphApp, KeyRecordingLock]:
    lock = KeyRecordingLock()
    saver_a, saver_b = object(), (None if shared else object())
    app = LangGraphApp(thread_lock=lock, **kwargs)
    app.register(graph=ProbeGraph(checkpointer=saver_a), name="alpha")
    app.register(graph=ProbeGraph(checkpointer=saver_a if shared else saver_b), name="beta")
    return app, lock


def _hold(app: LangGraphApp, graph: str) -> str:
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    token = scoped.acquire(graph, THREAD_ID)
    assert token is not None
    return token


def _invoke_request(name: str, endpoint: str = "invoke") -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url=f"https://example.test/api/graphs/{name}/{endpoint}",
        body=json.dumps(
            {"input": {"v": 1}, "config": {"configurable": {"thread_id": THREAD_ID}}}
        ).encode(),
        headers={},
    )


@pytest.mark.parametrize("surface", ["invoke", "stream", "invoke_async", "stream_async"])
async def test_native_surfaces_contend_across_graphs_sharing_a_checkpointer(surface: str) -> None:
    app, _ = _app(shared=True)
    _hold(app, "alpha")
    reg = app._registrations["beta"]
    request = _invoke_request("beta", "stream" if "stream" in surface else "invoke")
    if surface == "invoke":
        response = app._handle_invoke(request, reg)
    elif surface == "stream":
        response = app._handle_stream(request, reg)
    elif surface == "invoke_async":
        response = await app._handle_invoke_async(request, reg)
    else:
        response = await app._handle_stream_async(request, reg)
    assert response.status_code == 409


@pytest.mark.parametrize("surface", ["invoke", "stream", "invoke_async", "stream_async"])
async def test_native_surfaces_stay_independent_without_a_shared_checkpointer(
    surface: str,
) -> None:
    app, _ = _app(shared=False)
    _hold(app, "alpha")
    reg = app._registrations["beta"]
    request = _invoke_request("beta", "stream" if "stream" in surface else "invoke")
    if surface == "invoke":
        response = app._handle_invoke(request, reg)
    elif surface == "stream":
        response = app._handle_stream(request, reg)
    elif surface == "invoke_async":
        response = await app._handle_invoke_async(request, reg)
    else:
        response = await app._handle_stream_async(request, reg)
    assert response.status_code == 200


def test_lock_key_is_the_group_minimum_regardless_of_registration_order() -> None:
    saver = object()
    for order in (("alpha", "beta"), ("beta", "alpha")):
        lock = KeyRecordingLock()
        app = LangGraphApp(thread_lock=lock)
        for name in order:
            app.register(graph=ProbeGraph(checkpointer=saver), name=name)
        scoped = app._scoped_thread_lock()
        assert scoped is not None
        for name in order:
            token = scoped.acquire(name, THREAD_ID)
            assert token is not None
            scoped.release(name, THREAD_ID, token)
        assert set(lock.keys) == {"alpha"}


def test_unshared_and_checkpointerless_graphs_keep_their_own_key() -> None:
    lock = KeyRecordingLock()
    app = LangGraphApp(thread_lock=lock)
    app.register(graph=ProbeGraph(checkpointer=object()), name="solo")
    app.register(graph=ProbeGraph(checkpointer=None), name="none")
    app.register(graph=ProbeGraph(checkpointer=True), name="inherit")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    for name in ("solo", "none", "inherit"):
        token = scoped.acquire(name, THREAD_ID)
        assert token is not None
        scoped.release(name, THREAD_ID, token)
    assert lock.keys == ["solo", "none", "inherit"]


def test_resolve_lock_scope_unknown_name_is_unchanged() -> None:
    assert resolve_lock_scope("ghost", []) == "ghost"


def test_lock_backend_label_is_unchanged() -> None:
    app, _ = _app(shared=True)
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert type(scoped).__name__ == "KeyRecordingLock"


def test_owner_token_release_stays_safe_across_scoped_names() -> None:
    app, lock = _app(shared=True)
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    token = scoped.acquire("beta", THREAD_ID)
    assert token is not None
    scoped.release("alpha", THREAD_ID, "stale-token")  # stale: ignored
    assert scoped.acquire("alpha", THREAD_ID) is None
    scoped.release("alpha", THREAD_ID, token)  # holder may release under either name
    assert scoped.acquire("alpha", THREAD_ID) is not None


def test_ownership_guard_capability_is_forwarded_to_the_scoped_key() -> None:
    seen: list[str] = []

    class Provider(KeyRecordingLock):
        def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> None:
            seen.append(graph_name)

    app = LangGraphApp(thread_lock=Provider())
    saver = object()
    app.register(graph=ProbeGraph(checkpointer=saver), name="alpha")
    app.register(graph=ProbeGraph(checkpointer=saver), name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert isinstance(scoped, OwnershipGuardProvider)
    assert scoped.ownership_guard("beta", THREAD_ID, "t") is None
    assert seen == ["alpha"]


def test_ownership_guard_is_none_for_locks_without_the_capability() -> None:
    app, _ = _app(shared=True)
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert isinstance(scoped, OwnershipGuardProvider)
    assert scoped.ownership_guard("beta", THREAD_ID, "t") is None


def test_scoped_lock_is_none_without_a_configured_lock() -> None:
    app = LangGraphApp()
    app.thread_lock = None
    assert app._scoped_thread_lock() is None


def test_platform_run_contends_across_graphs_sharing_a_checkpointer() -> None:
    app, _ = _app(shared=True, platform_compat=True)
    store = InMemoryThreadStore(id_factory=lambda: THREAD_ID)
    app._thread_store = store
    store.create()
    _hold(app, "alpha")
    fa = app.function_app
    fa.functions_bindings = {}
    handler = next(
        fn.get_user_function()
        for fn in fa.get_functions()
        if fn.get_function_name() == "aflg_platform_runs_wait"
    )
    request = func.HttpRequest(
        method="POST",
        url=f"https://example.test/api/threads/{THREAD_ID}/runs/wait",
        body=json.dumps({"assistant_id": "beta", "input": {"v": 1}}).encode(),
        headers={"Content-Type": "application/json"},
        route_params={"thread_id": THREAD_ID},
    )
    assert handler(request).status_code == 409


def test_service_bus_contends_with_a_graph_sharing_its_checkpointer() -> None:
    from azure_functions_langgraph.triggers.service_bus import process_service_bus_message

    saver = object()
    app = LangGraphApp(thread_lock=KeyRecordingLock())
    app.register(graph=ProbeGraph(checkpointer=saver), name="alpha")
    app.register_service_bus(
        ProbeGraph(checkpointer=saver),
        "worker",
        connection="SB",
        queue_name="q",
        thread_id_factory=lambda _msg: THREAD_ID,
    )
    _hold(app, "alpha")
    reg = app._sb_registrations["worker"]
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    reg.lock_timeout = 0.0
    with pytest.raises(ThreadContentionError):
        process_service_bus_message(reg, FakeServiceBusMessage(b'{"v": 1}'), thread_lock=scoped)


def test_blob_lease_lock_names_collapse_to_one_marker_for_shared_graphs() -> None:
    names: list[str] = []

    class Spy(AzureBlobLeaseThreadLock):
        def __init__(self) -> None:  # no Azure client needed for naming
            self._prefix = "thread-locks/"

        def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
            names.append(self._blob_name(graph_name, thread_id))
            return None

    app = LangGraphApp(thread_lock=Spy())
    saver = object()
    app.register(graph=ProbeGraph(checkpointer=saver), name="alpha")
    app.register(graph=ProbeGraph(checkpointer=saver), name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    scoped.acquire("alpha", THREAD_ID)
    scoped.acquire("beta", THREAD_ID)
    assert names[0] == names[1] == "thread-locks/alpha/thread-1"


def test_streaming_app_shares_lock_scope_for_a_shared_checkpointer() -> None:
    from azure_functions_langgraph.streaming import StreamingLangGraphApp

    lock = KeyRecordingLock()
    app = StreamingLangGraphApp(thread_lock=lock)
    saver = object()
    app.register(graph=ProbeGraph(checkpointer=saver), name="alpha")
    app.register(graph=ProbeGraph(checkpointer=saver), name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert scoped.acquire("beta", THREAD_ID) is not None
    assert scoped.acquire("alpha", THREAD_ID) is None
    assert lock.keys == ["alpha", "alpha"]


def test_streaming_app_without_lock_has_no_scoped_lock() -> None:
    from azure_functions_langgraph.streaming import StreamingLangGraphApp

    app = StreamingLangGraphApp()
    app.thread_lock = None
    assert app._scoped_thread_lock() is None
