"""Persistent storage integration tests — verify that the Platform API
works correctly with different storage backends (in-memory and Azure mocked).

Each backend is exercised through the full HTTP/SDK layer using
``httpx.MockTransport``, replicating how a real SDK client interacts
with the Azure Functions handlers.

Issue: #61
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import importlib
import operator
import re
import sys
import threading
import types
from typing import Annotated, Any, Callable, TypedDict

import azure.functions as func
import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph_sdk.client import SyncLangGraphClient
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.platform.stores import InMemoryThreadStore

# ---------------------------------------------------------------------------
# Graph state & deterministic nodes (same as test_sdk_compat.py)
# ---------------------------------------------------------------------------


class ChatState(TypedDict, total=False):
    user_text: str
    history: Annotated[list[str], operator.add]
    turn_count: int
    last_reply: str


def greet(state: ChatState) -> dict[str, Any]:
    text = state.get("user_text", "")
    reply = f"Hello, {text}!" if text else "Hello!"
    return {"history": [reply], "last_reply": reply}


def count(state: ChatState) -> dict[str, Any]:
    return {"turn_count": (state.get("turn_count") or 0) + 1}


def _build_graph(*, checkpointer: Any = None) -> Any:
    builder = StateGraph(ChatState)
    builder.add_node("greet", greet)
    builder.add_node("count", count)
    builder.add_edge(START, "greet")
    builder.add_edge("greet", "count")
    builder.add_edge("count", END)
    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Route table — same as test_sdk_compat.py
# ---------------------------------------------------------------------------

_ROUTE_TABLE: list[tuple[str, re.Pattern[str], str, list[str]]] = [
    (
        "POST",
        re.compile(r"^/assistants/search$"),
        "aflg_platform_assistants_search",
        [],
    ),
    (
        "POST",
        re.compile(r"^/assistants/count$"),
        "aflg_platform_assistants_count",
        [],
    ),
    (
        "GET",
        re.compile(r"^/assistants/(?P<assistant_id>[^/]+)$"),
        "aflg_platform_assistants_get",
        ["assistant_id"],
    ),
    (
        "POST",
        re.compile(r"^/threads$"),
        "aflg_platform_threads_create",
        [],
    ),
    (
        "POST",
        re.compile(r"^/threads/search$"),
        "aflg_platform_threads_search",
        [],
    ),
    (
        "POST",
        re.compile(r"^/threads/count$"),
        "aflg_platform_threads_count",
        [],
    ),
    (
        "GET",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)$"),
        "aflg_platform_threads_get",
        ["thread_id"],
    ),
    (
        "PATCH",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)$"),
        "aflg_platform_threads_update",
        ["thread_id"],
    ),
    (
        "DELETE",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)$"),
        "aflg_platform_threads_delete",
        ["thread_id"],
    ),
    (
        "GET",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)/state$"),
        "aflg_platform_threads_state_get",
        ["thread_id"],
    ),
    (
        "POST",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)/state$"),
        "aflg_platform_threads_state_update",
        ["thread_id"],
    ),
    (
        "POST",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)/history$"),
        "aflg_platform_threads_history",
        ["thread_id"],
    ),
    (
        "POST",
        re.compile(r"^/runs/wait$"),
        "aflg_platform_runs_wait_threadless",
        [],
    ),
    (
        "POST",
        re.compile(r"^/runs/stream$"),
        "aflg_platform_runs_stream_threadless",
        [],
    ),
    (
        "POST",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)/runs/wait$"),
        "aflg_platform_runs_wait",
        ["thread_id"],
    ),
    (
        "POST",
        re.compile(r"^/threads/(?P<thread_id>[^/]+)/runs/stream$"),
        "aflg_platform_runs_stream",
        ["thread_id"],
    ),
]


# ---------------------------------------------------------------------------
# MockTransport bridge (identical to test_sdk_compat.py)
# ---------------------------------------------------------------------------


def _get_fn(fa: func.FunctionApp, fn_name: str) -> Any:
    """Retrieve a registered function handler by name."""
    fa.functions_bindings = {}
    for fn in fa.get_functions():
        if fn.get_function_name() == fn_name:
            return fn.get_user_function()
    raise AssertionError(f"Function {fn_name!r} not found")


def _make_transport(fa: func.FunctionApp) -> httpx.MockTransport:
    """Build an ``httpx.MockTransport`` that dispatches to Azure Functions handlers."""
    handlers: dict[str, Any] = {}
    for _, _, fn_name, _ in _ROUTE_TABLE:
        if fn_name not in handlers:
            handlers[fn_name] = _get_fn(fa, fn_name)

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.raw_path.decode().split("?")[0]

        if path.startswith("/api/") or path == "/api":
            path = path[4:]

        for rt_method, pattern, fn_name, param_names in _ROUTE_TABLE:
            if method != rt_method:
                continue
            m = pattern.match(path)
            if m is None:
                continue

            route_params = {name: m.group(name) for name in param_names}

            body = request.content
            az_req = func.HttpRequest(
                method=method,
                url=str(request.url),
                body=body,
                headers=dict(request.headers),
                route_params=route_params,
            )

            az_resp: func.HttpResponse = handlers[fn_name](az_req)

            resp_headers = dict(az_resp.headers) if az_resp.headers else {}
            content_type = az_resp.mimetype or "application/json"
            resp_headers["content-type"] = content_type
            return httpx.Response(
                status_code=az_resp.status_code,
                content=az_resp.get_body(),
                headers=resp_headers,
            )

        return httpx.Response(status_code=404, content=b'{"detail": "Not found"}')

    return httpx.MockTransport(handler)


def _make_sdk_client(fa: func.FunctionApp) -> SyncLangGraphClient:
    """Build a ``SyncLangGraphClient`` backed by our MockTransport."""
    transport = _make_transport(fa)
    httpx_client = httpx.Client(transport=transport, base_url="http://test")
    return SyncLangGraphClient(httpx_client)


# ---------------------------------------------------------------------------
# Azure mock classes (adapted from test_checkpointers_azure_blob.py and
# test_stores_azure_table.py with thread-safe backing for concurrency tests)
# ---------------------------------------------------------------------------


class FakeResourceNotFoundError(Exception):
    """Raised when a mock blob or table entity is not present."""


@dataclass
class _BlobRecord:
    data: bytes
    metadata: dict[str, str]


@dataclass
class _BlobItem:
    name: str


class _MockDownloadStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class _MockBlobProperties:
    def __init__(self, metadata: dict[str, str]) -> None:
        self.metadata = metadata


class MockBlobClient:
    def __init__(self, container: MockContainerClient, blob_name: str) -> None:
        self._container = container
        self._blob_name = blob_name

    def upload_blob(self, data: bytes, metadata: dict[str, str], overwrite: bool) -> None:
        with self._container.lock:
            if not overwrite and self._blob_name in self._container.blobs:
                raise ValueError("Blob already exists")
            self._container.blobs[self._blob_name] = _BlobRecord(
                data=data, metadata=dict(metadata)
            )

    def download_blob(self) -> _MockDownloadStream:
        with self._container.lock:
            record = self._container.blobs.get(self._blob_name)
            if record is None:
                raise FakeResourceNotFoundError(self._blob_name)
            return _MockDownloadStream(record.data)

    def get_blob_properties(self) -> _MockBlobProperties:
        with self._container.lock:
            record = self._container.blobs.get(self._blob_name)
            if record is None:
                raise FakeResourceNotFoundError(self._blob_name)
            return _MockBlobProperties(metadata=dict(record.metadata))

    def delete_blob(self) -> None:
        with self._container.lock:
            if self._blob_name not in self._container.blobs:
                raise FakeResourceNotFoundError(self._blob_name)
            del self._container.blobs[self._blob_name]


class MockContainerClient:
    def __init__(self) -> None:
        self.blobs: dict[str, _BlobRecord] = {}
        self.lock = threading.RLock()

    def get_blob_client(self, blob: str) -> MockBlobClient:
        return MockBlobClient(self, blob)

    def list_blobs(self, name_starts_with: str = "") -> list[_BlobItem]:
        with self.lock:
            return [
                _BlobItem(name=name)
                for name in sorted(self.blobs)
                if name.startswith(name_starts_with)
            ]


class MockTableClient:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        self.lock = threading.RLock()

    def create_entity(self, entity: dict[str, Any]) -> None:
        with self.lock:
            key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
            if key in self.entities:
                raise ValueError(f"Entity already exists: {key}")
            self.entities[key] = deepcopy(entity)

    def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]:
        with self.lock:
            key = (partition_key, row_key)
            entity = self.entities.get(key)
            if entity is None:
                raise FakeResourceNotFoundError(row_key)
            return deepcopy(entity)

    def update_entity(self, entity: dict[str, Any], mode: str) -> None:
        with self.lock:
            key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
            if key not in self.entities:
                raise FakeResourceNotFoundError(key[1])
            if mode == "merge":
                merged = deepcopy(self.entities[key])
                merged.update(deepcopy(entity))
                self.entities[key] = merged
                return
            self.entities[key] = deepcopy(entity)

    def delete_entity(self, partition_key: str, row_key: str) -> None:
        with self.lock:
            key = (partition_key, row_key)
            if key not in self.entities:
                raise FakeResourceNotFoundError(row_key)
            del self.entities[key]

    def query_entities(
        self,
        query_filter: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del kwargs
        with self.lock:
            entities = [deepcopy(entity) for entity in self.entities.values()]
        if query_filter is None:
            return entities

        parts = [p.strip() for p in query_filter.split(" and ")]
        filtered = entities
        for part in parts:
            if part.startswith("PartitionKey eq '") and part.endswith("'"):
                pk = part[len("PartitionKey eq '") : -1].replace("''", "'")
                filtered = [e for e in filtered if e.get("PartitionKey") == pk]
            elif part.startswith("status eq '") and part.endswith("'"):
                status = part[len("status eq '") : -1].replace("''", "'")
                filtered = [e for e in filtered if e.get("status") == status]
            else:
                raise ValueError(f"Unsupported query filter part: {part}")
        return filtered


# ---------------------------------------------------------------------------
# Fake Azure module installation (needed for azure_mocked backend)
# ---------------------------------------------------------------------------


_FAKE_MODULE_KEYS = [
    "azure",
    "azure.storage",
    "azure.storage.blob",
    "azure.core",
    "azure.core.exceptions",
    "azure.data.tables",
]


def _install_fake_azure_modules() -> Callable[[], None]:
    """Install fake Azure SDK modules into sys.modules; return a cleanup callable."""
    originals: dict[str, types.ModuleType | None] = {
        key: sys.modules.get(key) for key in _FAKE_MODULE_KEYS
    }

    azure_mod = types.ModuleType("azure")
    azure_storage_mod = types.ModuleType("azure.storage")
    azure_blob_mod = types.ModuleType("azure.storage.blob")
    setattr(azure_blob_mod, "ContainerClient", MockContainerClient)

    azure_core_mod = types.ModuleType("azure.core")
    azure_core_exceptions_mod = types.ModuleType("azure.core.exceptions")
    setattr(azure_core_exceptions_mod, "ResourceNotFoundError", FakeResourceNotFoundError)

    azure_data_tables_mod = types.ModuleType("azure.data.tables")
    setattr(azure_data_tables_mod, "TableClient", MockTableClient)

    sys.modules["azure"] = azure_mod
    sys.modules["azure.storage"] = azure_storage_mod
    sys.modules["azure.storage.blob"] = azure_blob_mod
    sys.modules["azure.core"] = azure_core_mod
    sys.modules["azure.core.exceptions"] = azure_core_exceptions_mod
    sys.modules["azure.data.tables"] = azure_data_tables_mod

    def _cleanup() -> None:
        for key in _FAKE_MODULE_KEYS:
            prev = originals[key]
            if prev is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = prev

    return _cleanup


# ---------------------------------------------------------------------------
# Backend fixture — produces new_store/new_saver/new_app_client factories
# ---------------------------------------------------------------------------


class _BackendContext:
    """Holds factories for building backends and app clients."""

    def __init__(self, backend_name: str) -> None:
        self.backend_name = backend_name
        # For azure_mocked: shared backing state across restarts
        self._mock_container: MockContainerClient | None = None
        self._mock_table: MockTableClient | None = None
        self._cleanup: Callable[[], None] | None = None

        if backend_name == "azure_mocked":
            self._cleanup = _install_fake_azure_modules()
            self._mock_container = MockContainerClient()
            self._mock_table = MockTableClient()

    def new_saver(self) -> Any:
        """Create a new checkpoint saver instance."""
        if self.backend_name == "memory":
            return MemorySaver()
        else:
            module = importlib.import_module(
                "azure_functions_langgraph.checkpointers.azure_blob"
            )
            cls = getattr(module, "AzureBlobCheckpointSaver")
            return cls(container_client=self._mock_container)

    def new_store(self) -> Any:
        """Create a new thread store instance."""
        if self.backend_name == "memory":
            return InMemoryThreadStore()
        else:
            module = importlib.import_module(
                "azure_functions_langgraph.stores.azure_table"
            )
            cls = getattr(module, "AzureTableThreadStore")
            return cls(
                table_client=self._mock_table,
                not_found_error=FakeResourceNotFoundError,
            )

    def new_app_client(
        self, *, name: str = "agent"
    ) -> tuple[LangGraphApp, SyncLangGraphClient]:
        """Build a LangGraphApp + SDK client pair with fresh saver/store."""
        saver = self.new_saver()
        store = self.new_store()
        graph = _build_graph(checkpointer=saver)
        app = LangGraphApp(platform_compat=True)
        app._thread_store = store
        app.register(graph=graph, name=name)
        client = _make_sdk_client(app.function_app)
        return app, client

    def close(self) -> None:
        """Restore sys.modules to pre-test state."""
        if self._cleanup is not None:
            self._cleanup()


@pytest.fixture(params=["memory", "azure_mocked"])
def backend(request: Any) -> Any:  # Generator[_BackendContext, None, None]
    """Parameterized backend fixture — cleans up sys.modules on teardown."""
    ctx = _BackendContext(request.param)
    yield ctx
    ctx.close()


@pytest.fixture
def azure_backend() -> Any:  # Generator[_BackendContext, None, None]
    """Azure-only backend fixture (for restart simulation tests)."""
    ctx = _BackendContext("azure_mocked")
    yield ctx
    ctx.close()


# ---------------------------------------------------------------------------
# Integration tests — Persistent storage flows
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPersistentStorageFlows:
    """End-to-end flows exercised through the SDK layer against both backends."""

    def test_multi_turn_accumulates_state(self, backend: _BackendContext) -> None:
        """Multiple runs on the same thread accumulate state across turns."""
        _, client = backend.new_app_client()
        thread = client.threads.create()
        tid = thread["thread_id"]

        # Turn 1
        out1 = client.runs.wait(
            tid,
            "agent",
            input={"user_text": "Alice", "history": [], "turn_count": 0},
        )
        assert isinstance(out1, dict)
        assert out1["turn_count"] == 1
        assert out1["history"] == ["Hello, Alice!"]
        assert out1["last_reply"] == "Hello, Alice!"

        # Turn 2
        out2 = client.runs.wait(
            tid,
            "agent",
            input={"user_text": "Bob"},
        )
        assert isinstance(out2, dict)
        assert out2["turn_count"] == 2
        assert out2["history"] == ["Hello, Alice!", "Hello, Bob!"]
        assert out2["last_reply"] == "Hello, Bob!"

        # Turn 3
        out3 = client.runs.wait(
            tid,
            "agent",
            input={"user_text": "Charlie"},
        )
        assert isinstance(out3, dict)
        assert out3["turn_count"] == 3
        assert out3["history"] == ["Hello, Alice!", "Hello, Bob!", "Hello, Charlie!"]

        # Verify state via get_state
        state = client.threads.get_state(tid)
        values = state["values"]
        assert isinstance(values, dict)
        assert values["turn_count"] == 3
        assert len(values["history"]) == 3

    def test_thread_lifecycle_crud_search_delete(
        self, backend: _BackendContext
    ) -> None:
        """Full thread lifecycle: create → update → search → run → get_state → delete."""
        _, client = backend.new_app_client()

        # Create with metadata
        thread = client.threads.create(metadata={"env": "test", "version": "1"})
        tid = thread["thread_id"]
        assert thread["status"] == "idle"
        assert thread["metadata"] == {"env": "test", "version": "1"}

        # Update metadata
        updated = client.threads.update(tid, metadata={"env": "prod"})
        assert updated["metadata"] == {"env": "prod", "version": "1"}

        # Search — should find our thread
        results = client.threads.search(metadata={"env": "prod"})
        assert len(results) >= 1
        found = [r for r in results if r["thread_id"] == tid]
        assert len(found) == 1

        # Count
        total = client.threads.count()
        assert total >= 1

        # Run graph
        result = client.runs.wait(
            tid,
            "agent",
            input={"user_text": "Test", "history": [], "turn_count": 0},
        )
        assert isinstance(result, dict)
        assert result["last_reply"] == "Hello, Test!"

        # Get state after run
        state = client.threads.get_state(tid)
        values = state["values"]
        assert isinstance(values, dict)
        assert values["turn_count"] == 1

        # History after run
        history = client.threads.get_history(tid)
        assert isinstance(history, list)
        assert len(history) >= 1

        # Delete thread
        client.threads.delete(tid)

        # Verify deleted (should 404)
        from langgraph_sdk.errors import NotFoundError

        with pytest.raises(NotFoundError):
            client.threads.get(tid)

    def test_sdk_workflow_against_backend(self, backend: _BackendContext) -> None:
        """Full SDK workflow: thread create → run → update_state → get_state → history."""
        _, client = backend.new_app_client()
        thread = client.threads.create()
        tid = thread["thread_id"]

        # Run graph
        client.runs.wait(
            tid,
            "agent",
            input={"user_text": "Workflow", "history": [], "turn_count": 0},
        )

        # Verify state
        state = client.threads.get_state(tid)
        values = state["values"]
        assert isinstance(values, dict)
        assert values["last_reply"] == "Hello, Workflow!"
        assert values["turn_count"] == 1

        # Update state
        update_resp = client.threads.update_state(
            tid,
            values={"last_reply": "Overridden"},
            as_node="count",
        )
        assert "checkpoint" in update_resp
        assert update_resp["checkpoint"]["thread_id"] == tid

        # Verify state was updated
        state = client.threads.get_state(tid)
        values = state["values"]
        assert isinstance(values, dict)
        assert values["last_reply"] == "Overridden"

        # Run again — should accumulate from updated state
        out2 = client.runs.wait(
            tid,
            "agent",
            input={"user_text": "After"},
        )
        assert isinstance(out2, dict)
        assert out2["turn_count"] == 2
        assert "Hello, After!" in out2["history"]

        # History should show multiple checkpoints
        history = client.threads.get_history(tid)
        assert isinstance(history, list)
        assert len(history) >= 3  # initial run + update_state + second run

    def test_concurrent_thread_create_update(self, backend: _BackendContext) -> None:
        """Concurrent thread creation and updates don't corrupt state."""
        _, client = backend.new_app_client()

        thread_ids: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def create_and_run(index: int) -> None:
            try:
                thread = client.threads.create(
                    metadata={"worker": str(index)}
                )
                tid = thread["thread_id"]
                with lock:
                    thread_ids.append(tid)

                # Run graph on the thread
                result = client.runs.wait(
                    tid,
                    "agent",
                    input={
                        "user_text": f"Worker{index}",
                        "history": [],
                        "turn_count": 0,
                    },
                )
                assert isinstance(result, dict)
                assert result["last_reply"] == f"Hello, Worker{index}!"
                assert result["turn_count"] == 1
            except Exception as exc:
                with lock:
                    errors.append(exc)

        # Launch 5 concurrent workers
        threads = [
            threading.Thread(target=create_and_run, args=(i,))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"Concurrent errors: {errors}"
        assert len(thread_ids) == 5

        # Verify all threads are searchable
        total = client.threads.count()
        assert total == 5

        # Verify each thread has correct state
        for tid in thread_ids:
            state = client.threads.get_state(tid)
            values = state["values"]
            assert isinstance(values, dict)
            assert values["turn_count"] == 1

    def test_stream_with_persistent_state(self, backend: _BackendContext) -> None:
        """Streaming runs also persist state correctly."""
        _, client = backend.new_app_client()
        thread = client.threads.create()
        tid = thread["thread_id"]

        # Stream run
        events = list(
            client.runs.stream(
                tid,
                "agent",
                input={"user_text": "Stream", "history": [], "turn_count": 0},
                stream_mode="values",
            )
        )

        assert events[0].event == "metadata"
        assert events[-1].event == "end"

        values_events = [e for e in events if e.event == "values"]
        assert len(values_events) >= 1
        final = values_events[-1].data
        assert final["last_reply"] == "Hello, Stream!"

        # Verify state persisted after stream
        state = client.threads.get_state(tid)
        values = state["values"]
        assert isinstance(values, dict)
        assert values["turn_count"] == 1
        assert values["last_reply"] == "Hello, Stream!"

        # Second turn via stream should accumulate
        events2 = list(
            client.runs.stream(
                tid,
                "agent",
                input={"user_text": "Stream2"},
                stream_mode="values",
            )
        )
        values2 = [e for e in events2 if e.event == "values"]
        final2 = values2[-1].data
        assert final2["turn_count"] == 2
        assert final2["history"] == ["Hello, Stream!", "Hello, Stream2!"]


# ---------------------------------------------------------------------------
# Restart simulation tests — azure_mocked only
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPersistentRestart:
    """Verify that state survives process restart (new saver/store instances
    pointing to the same backing storage)."""

    def test_restart_simulation_loads_existing_checkpoint(
        self, azure_backend: _BackendContext
    ) -> None:
        """After 'restarting' (new saver/store, same backing), state is recovered."""
        # Session 1: create thread and run
        app1, client1 = azure_backend.new_app_client()
        thread = client1.threads.create(metadata={"session": "first"})
        tid = thread["thread_id"]

        out1 = client1.runs.wait(
            tid,
            "agent",
            input={"user_text": "Before", "history": [], "turn_count": 0},
        )
        assert isinstance(out1, dict)
        assert out1["turn_count"] == 1
        assert out1["last_reply"] == "Hello, Before!"

        # Verify state exists in session 1
        state1 = client1.threads.get_state(tid)
        values1 = state1["values"]
        assert isinstance(values1, dict)
        assert values1["turn_count"] == 1

        # Session 2: "restart" — new saver/store instances, same backing data
        app2, client2 = azure_backend.new_app_client()

        # Thread should still exist
        thread2 = client2.threads.get(tid)
        assert thread2["thread_id"] == tid
        assert thread2["metadata"] == {"session": "first"}

        # State should be recovered
        state2 = client2.threads.get_state(tid)
        values2 = state2["values"]
        assert isinstance(values2, dict)
        assert values2["turn_count"] == 1
        assert values2["last_reply"] == "Hello, Before!"

        # Continue from recovered state
        out2 = client2.runs.wait(
            tid,
            "agent",
            input={"user_text": "After"},
        )
        assert isinstance(out2, dict)
        assert out2["turn_count"] == 2
        assert out2["history"] == ["Hello, Before!", "Hello, After!"]
        assert out2["last_reply"] == "Hello, After!"

    def test_restart_preserves_multiple_threads(
        self, azure_backend: _BackendContext
    ) -> None:
        """Multiple threads survive restart and maintain independent state."""
        # Session 1: create and run on two threads
        _, client1 = azure_backend.new_app_client()

        t1 = client1.threads.create(metadata={"name": "thread-a"})
        t2 = client1.threads.create(metadata={"name": "thread-b"})

        client1.runs.wait(
            t1["thread_id"],
            "agent",
            input={"user_text": "Alpha", "history": [], "turn_count": 0},
        )
        client1.runs.wait(
            t2["thread_id"],
            "agent",
            input={"user_text": "Beta", "history": [], "turn_count": 0},
        )

        # Session 2: restart
        _, client2 = azure_backend.new_app_client()

        # Both threads should exist
        assert client2.threads.count() == 2

        # Each thread has independent state
        s1 = client2.threads.get_state(t1["thread_id"])
        s2 = client2.threads.get_state(t2["thread_id"])
        assert isinstance(s1["values"], dict)
        assert isinstance(s2["values"], dict)
        assert s1["values"]["last_reply"] == "Hello, Alpha!"
        assert s2["values"]["last_reply"] == "Hello, Beta!"

        # Continue thread A from session 2
        out_a = client2.runs.wait(
            t1["thread_id"],
            "agent",
            input={"user_text": "Alpha2"},
        )
        assert isinstance(out_a, dict)
        assert out_a["turn_count"] == 2
        assert out_a["history"] == ["Hello, Alpha!", "Hello, Alpha2!"]

        # Thread B is unaffected
        s2_after = client2.threads.get_state(t2["thread_id"])
        assert isinstance(s2_after["values"], dict)
        assert s2_after["values"]["turn_count"] == 1

    def test_restart_history_survives(
        self, azure_backend: _BackendContext
    ) -> None:
        """State history is accessible after restart."""
        # Session 1
        _, client1 = azure_backend.new_app_client()
        thread = client1.threads.create()
        tid = thread["thread_id"]

        client1.runs.wait(
            tid,
            "agent",
            input={"user_text": "Turn1", "history": [], "turn_count": 0},
        )
        client1.runs.wait(
            tid,
            "agent",
            input={"user_text": "Turn2"},
        )

        history1 = client1.threads.get_history(tid)
        assert len(history1) >= 2

        # Session 2: restart
        _, client2 = azure_backend.new_app_client()

        # History should still be available
        history2 = client2.threads.get_history(tid)
        assert len(history2) >= 2

        # Latest state is turn 2
        state = client2.threads.get_state(tid)
        values = state["values"]
        assert isinstance(values, dict)
        assert values["turn_count"] == 2
        assert values["history"] == ["Hello, Turn1!", "Hello, Turn2!"]
