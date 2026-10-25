"""Regression coverage for threaded Platform run setup ownership (#690)."""

from collections.abc import Mapping
from typing import Literal

import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.observability import RunContext
from azure_functions_langgraph.platform.contracts import Interrupt, Thread, ThreadStatus
from azure_functions_langgraph.platform.stores import InMemoryThreadStore
from tests.conftest import FakeCompiledGraph
from tests.test_platform_routes import _get_fn, _post_request


class SetupFailure(RuntimeError):
    pass


class FaultStore(InMemoryThreadStore):
    """Real in-memory ownership with independently injectable I/O faults."""

    fault: Literal["metadata", "acquire", "release", "none"] = "metadata"

    def __init__(self) -> None:
        super().__init__()
        self.releases: list[tuple[ThreadStatus, str | None]] = []
        self.owner: str | None = None

    def update(
        self,
        thread_id: str,
        *,
        metadata: Mapping[str, object] | None = None,
        status: ThreadStatus | None = None,
        values: dict[str, object] | None = None,
        interrupts: dict[str, list[Interrupt]] | None = None,
        assistant_id: str | None = None,
    ) -> Thread:
        if self.fault in {"metadata", "release"} and metadata:
            raise SetupFailure("private-storage-canary")
        return super().update(
            thread_id,
            metadata=metadata,
            status=status,
            values=values,
            interrupts=interrupts,
            assistant_id=assistant_id,
        )

    def try_acquire_run_lock(
        self, thread_id: str, *, assistant_id: str | None = None
    ) -> Thread | None:
        if self.fault == "acquire":
            raise SetupFailure("private-storage-canary")
        locked = super().try_acquire_run_lock(thread_id, assistant_id=assistant_id)
        if locked is not None:
            self.owner = locked.lock_token
        return locked

    def release_run_lock(
        self,
        thread_id: str,
        *,
        status: ThreadStatus,
        values: dict[str, object] | None = None,
        owner_token: str | None = None,
    ) -> Thread:
        self.releases.append((status, owner_token))
        if self.fault == "release":
            raise SetupFailure("private-storage-canary")
        return super().release_run_lock(
            thread_id, status=status, values=values, owner_token=owner_token
        )


class RecordingLock(InProcessThreadLock):
    def __init__(self) -> None:
        super().__init__()
        self.releases: list[str] = []
        self.fail_release = False

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self.releases.append(token)
        super().release(graph_name, thread_id, token)
        if self.fail_release:
            raise SetupFailure("private-lock-canary")


class Observer:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append("started")

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append("completed")

    def on_run_failed(self, ctx: RunContext, error: BaseException) -> None:
        self.events.append("failed")

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:
        self.events.append("rejected")


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("fault", ["metadata", "acquire", "release", "outer_release"])
def test_run_releases_ownership_when_setup_or_cleanup_fails(endpoint: str, fault: str) -> None:
    # Given real local/store exclusion and one failing setup or cleanup seam.
    store = FaultStore()
    lock = RecordingLock()
    observer = Observer()
    graph = FakeCompiledGraph()
    app = LangGraphApp(platform_compat=True, thread_lock=lock, observer=observer)
    app._thread_store = store
    app.register(graph=graph, name="agent")
    thread = store.create()
    if fault == "acquire":
        store.fault = "acquire"
    elif fault == "release":
        store.fault = "release"
    elif fault == "outer_release":
        lock.fail_release = True
    handler = _get_fn(app.function_app, f"aflg_platform_runs_{endpoint}")
    request = _post_request(
        f"/api/threads/{thread.thread_id}/runs/{endpoint}",
        {"assistant_id": "agent", "input": {}, "metadata": {"tag": "new"}},
        thread_id=thread.thread_id,
    )
    # When the request reaches the fault.
    response = handler(request)

    # Then errors are sanitized and every acquired owner is released once.
    assert response.status_code == 500
    assert b"private-" not in response.get_body()
    assert observer.events == ["failed"]
    assert len(lock.releases) == 1
    assert store.releases == ([] if fault == "acquire" else [("error", store.owner)])
    store.fault = "none"
    lock.fail_release = False
    if fault == "release":
        # Failed storage I/O cannot promise a persisted release; model operator recovery.
        InMemoryThreadStore.release_run_lock(
            store, thread.thread_id, status="error", owner_token=store.owner
        )
    assert handler(request).status_code == 200
