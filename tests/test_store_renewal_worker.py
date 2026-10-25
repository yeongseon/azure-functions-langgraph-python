from threading import Event, Thread
from unittest.mock import patch

import azure.functions as func
import pytest

from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.platform import _store_renewal
from azure_functions_langgraph.platform._common import PlatformRouteDeps
from azure_functions_langgraph.platform.stores import InMemoryThreadStore


def test_stop_waits_for_inflight_renewal_failure() -> None:
    # Given a real renewal worker blocked inside store I/O when cleanup starts.
    store = InMemoryThreadStore()
    deps = PlatformRouteDeps(
        registrations={},
        thread_store=store,
        thread_lock=InProcessThreadLock(),
        auth_level=func.AuthLevel.FUNCTION,
        max_stream_response_bytes=1024,
    )
    entered = Event()
    failure = OSError("private-renewal-canary")

    def renew(thread_id: str, owner_token: str) -> bool:
        entered.set()
        renewal = _store_renewal._STORE_RENEWALS[owner_token]
        assert renewal.stop.wait(2)
        raise failure

    with (
        patch.object(_store_renewal, "RENEWAL_INTERVAL", 0),
        patch.object(store, "renew_run_lock", side_effect=renew),
    ):
        _store_renewal._start_store_renewal(deps, "thread", "owner")
        assert entered.wait(2)
        renewal = _store_renewal._STORE_RENEWALS["owner"]
        # When cleanup stops and joins the in-flight renewal.
        result = _store_renewal.stop_store_renewal("owner")

    # Then its late failure is observed and neither thread nor registry entry remains.
    assert result is failure
    assert renewal.worker is not None
    assert not renewal.worker.is_alive()
    assert "owner" not in _store_renewal._STORE_RENEWALS


def test_failed_worker_start_leaves_no_registry_entry() -> None:
    # Given the worker cannot be started after the store lock has been acquired.
    deps = PlatformRouteDeps(
        registrations={},
        thread_store=InMemoryThreadStore(),
        thread_lock=InProcessThreadLock(),
        auth_level=func.AuthLevel.FUNCTION,
        max_stream_response_bytes=1024,
    )
    with patch.object(Thread, "start", side_effect=RuntimeError("cannot start thread")):
        # When / Then startup fails normally and cleanup has no unstarted worker to join.
        with pytest.raises(RuntimeError, match="cannot start thread"):
            _store_renewal._start_store_renewal(deps, "thread", "owner")
    assert _store_renewal.stop_store_renewal("owner") is None
