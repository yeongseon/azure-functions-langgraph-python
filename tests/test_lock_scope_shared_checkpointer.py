"""Graphs on the same checkpoint storage share one thread-lock identity (#858).

The scope derives from the checkpointer's own ``lock_scope_identity`` (physical
storage), never from the registration inventory, so it is stable across apps,
processes and late registrations. Hold the lock through the app's own scoped
lock under one graph name, then drive another registration through every
surface that takes the lock. Same storage must reject (contend); different
storage, or a saver with no identity, stays independent. No Azure access and no
sleeps: all contention is deterministic.
"""

from __future__ import annotations

import json
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.locks._scope import ScopedThreadLock, graph_lock_scope
from azure_functions_langgraph.locks.azure_blob import AzureBlobLeaseThreadLock
from azure_functions_langgraph.locks.guard import OwnershipGuardProvider
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from azure_functions_langgraph.triggers.service_bus import ThreadContentionError
from tests.ownership_guard_support import ProbeGraph
from tests.test_service_bus import FakeServiceBusMessage

THREAD_ID = "thread-1"


class StorageSaver:
    """Fake saver: distinct objects with the same identity share one storage."""

    def __init__(self, identity: str | None) -> None:
        self.lock_scope_identity = identity


def _scope(identity: str) -> str:
    graph = type("G", (), {"checkpointer": StorageSaver(identity)})()
    return graph_lock_scope("ignored", graph)


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
    app = LangGraphApp(thread_lock=lock, **kwargs)
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="alpha")
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1" if shared else "s2")), name="beta")
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


def test_lock_key_is_the_storage_scope_regardless_of_registration_order() -> None:
    for order in (("alpha", "beta"), ("beta", "alpha")):
        lock = KeyRecordingLock()
        app = LangGraphApp(thread_lock=lock)
        for name in order:
            app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name=name)
        scoped = app._scoped_thread_lock()
        assert scoped is not None
        for name in order:
            token = scoped.acquire(name, THREAD_ID)
            assert token is not None
            scoped.release(name, THREAD_ID, token)
        assert set(lock.keys) == {_scope("s1")}


def test_unshared_and_checkpointerless_graphs_keep_their_own_key() -> None:
    lock = KeyRecordingLock()
    app = LangGraphApp(thread_lock=lock)
    app.register(graph=ProbeGraph(checkpointer=object()), name="solo")
    app.register(graph=ProbeGraph(checkpointer=None), name="none")
    app.register(graph=ProbeGraph(checkpointer=True), name="inherit")
    app.register(graph=ProbeGraph(checkpointer=StorageSaver(None)), name="blank")
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("")), name="empty")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    for name in ("solo", "none", "inherit", "blank", "empty"):
        token = scoped.acquire(name, THREAD_ID)
        assert token is not None
        scoped.release(name, THREAD_ID, token)
    assert lock.keys == ["solo", "none", "inherit", "blank", "empty"]


def test_unregistered_name_keeps_its_own_scope() -> None:
    app = LangGraphApp(thread_lock=KeyRecordingLock())
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    token = scoped.acquire("ghost", THREAD_ID)
    assert token is not None
    scoped.release("ghost", THREAD_ID, token)


def test_same_storage_scope_is_independent_of_the_graph_name() -> None:
    graph = type("G", (), {"checkpointer": StorageSaver("s1")})()
    assert graph_lock_scope("a", graph) == graph_lock_scope("zzz", graph) == _scope("s1")
    assert _scope("s1") != _scope("s2")
    assert _scope("s1").startswith("storage-")


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
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="alpha")
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert isinstance(scoped, OwnershipGuardProvider)
    assert scoped.ownership_guard("beta", THREAD_ID, "t") is None
    assert seen == [_scope("s1")]


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

    app = LangGraphApp(thread_lock=KeyRecordingLock())
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="alpha")
    app.register_service_bus(
        ProbeGraph(checkpointer=StorageSaver("s1")),
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
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="alpha")
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    scoped.acquire("alpha", THREAD_ID)
    scoped.acquire("beta", THREAD_ID)
    assert names[0] == names[1] == f"thread-locks/{_scope('s1')}/thread-1"


def test_streaming_app_shares_lock_scope_for_a_shared_checkpointer() -> None:
    from azure_functions_langgraph.streaming import StreamingLangGraphApp

    lock = KeyRecordingLock()
    app = StreamingLangGraphApp(thread_lock=lock)
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="alpha")
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert scoped.acquire("beta", THREAD_ID) is not None
    assert scoped.acquire("alpha", THREAD_ID) is None
    assert lock.keys == [_scope("s1")] * 2


def test_streaming_app_without_lock_has_no_scoped_lock() -> None:
    from azure_functions_langgraph.streaming import StreamingLangGraphApp

    app = StreamingLangGraphApp()
    app.thread_lock = None
    assert app._scoped_thread_lock() is None


def test_a_saver_without_identity_is_not_grouped_even_when_the_object_is_shared() -> None:
    """Documented limit: only a reported storage identity can prove sharing."""
    saver = object()
    app = LangGraphApp(thread_lock=KeyRecordingLock())
    app.register(graph=ProbeGraph(checkpointer=saver), name="alpha")
    app.register(graph=ProbeGraph(checkpointer=saver), name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert scoped.acquire("alpha", THREAD_ID) is not None
    assert scoped.acquire("beta", THREAD_ID) is not None


# --- #867 review (1): overlapping names across HTTP and Service Bus -----------


def _register_sb(app: LangGraphApp, name: str, identity: str) -> None:
    app.register_service_bus(
        ProbeGraph(checkpointer=StorageSaver(identity)),
        name,
        connection="SB",
        queue_name="q",
        thread_id_factory=lambda _msg: THREAD_ID,
    )


def test_cross_surface_name_overlap_with_different_storage_is_rejected() -> None:
    """HTTP worker@A, HTTP alpha@B, SB worker@B: the B writers must not split scopes."""
    app = LangGraphApp(thread_lock=KeyRecordingLock())
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("A")), name="worker")
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("B")), name="alpha")
    with pytest.raises(ValueError, match="different checkpoint storage"):
        _register_sb(app, "worker", "B")


def test_cross_surface_name_overlap_is_rejected_in_either_registration_order() -> None:
    app = LangGraphApp(thread_lock=KeyRecordingLock())
    _register_sb(app, "worker", "B")
    with pytest.raises(ValueError, match="different checkpoint storage"):
        app.register(graph=ProbeGraph(checkpointer=StorageSaver("A")), name="worker")


def test_cross_surface_name_overlap_on_the_same_storage_is_allowed_and_contends() -> None:
    app = LangGraphApp(thread_lock=KeyRecordingLock())
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("B")), name="worker")
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("B")), name="alpha")
    _register_sb(app, "worker", "B")
    held = _hold(app, "alpha")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert scoped.acquire("worker", THREAD_ID) is None
    scoped.release("alpha", THREAD_ID, held)
    assert scoped.acquire("worker", THREAD_ID) is not None


# --- #867 review (2): scope must not depend on the app's inventory ------------


def test_scope_is_aligned_across_apps_with_different_graph_sets() -> None:
    from azure_functions_langgraph.streaming import StreamingLangGraphApp

    lock = KeyRecordingLock()  # one backend shared by both apps (e.g. one Blob container)
    full = LangGraphApp(thread_lock=lock)
    full.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="alpha")
    full.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="beta")
    partial = StreamingLangGraphApp(thread_lock=lock)
    partial.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="beta")
    held = full._scoped_thread_lock()
    other = partial._scoped_thread_lock()
    assert held is not None and other is not None
    token = held.acquire("beta", THREAD_ID)
    assert token is not None
    assert other.acquire("beta", THREAD_ID) is None  # same storage, same key
    held.release("beta", THREAD_ID, token)
    assert other.acquire("beta", THREAD_ID) is not None
    assert len(set(lock.keys)) == 1


# --- #867 review (3): scope is bound to the acquisition -----------------------


def test_late_registration_cannot_strand_an_in_flight_lock() -> None:
    lock = KeyRecordingLock()
    app = LangGraphApp(thread_lock=lock)
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    token = scoped.acquire("beta", THREAD_ID)
    assert token is not None
    app.register(graph=ProbeGraph(checkpointer=StorageSaver("s1")), name="alpha")  # late
    assert scoped.acquire("beta", THREAD_ID) is None  # still held, one key
    scoped.release("beta", THREAD_ID, token)
    assert scoped.acquire("beta", THREAD_ID) is not None  # actually released
    assert len(set(lock.keys)) == 1


def test_token_release_and_guard_use_the_scope_it_was_acquired_under() -> None:
    """Even a scope function that changes mid-flight cannot redirect release."""
    seen: list[tuple[str, str]] = []

    class Provider(KeyRecordingLock):
        def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> None:
            seen.append(("guard", graph_name))

    inner = Provider()
    current = {"scope": "first"}
    scoped = ScopedThreadLock(inner, lambda _name: current["scope"])
    token = scoped.acquire("g", THREAD_ID)
    assert token is not None
    current["scope"] = "second"
    assert scoped.ownership_guard("g", THREAD_ID, token) is None
    scoped.release("g", THREAD_ID, token)
    assert seen == [("guard", "first")]
    assert scoped.acquire("g", THREAD_ID) is not None  # "second" key; "first" was freed
    current["scope"] = "first"
    assert scoped.acquire("g", THREAD_ID) is not None  # proves "first" is released
