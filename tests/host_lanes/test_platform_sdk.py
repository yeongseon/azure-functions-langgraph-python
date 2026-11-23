"""Platform SDK scenarios against a real local Functions host (#883).

``tests/test_sdk_compat.py`` runs the documented operations with the real ``langgraph-sdk`` client
over ``httpx.MockTransport`` and a route table kept in that test file. These scenarios run the same
operations over real HTTP against ``tests/host_lanes/platform_app``, served by ``func start`` on
Azurite: the host's own route registration and URL handling, real SSE, an Azure Table thread store,
an Azure Blob checkpointer and lease lock. Local evidence only; Azurite is not Azure Storage.

Run by ``make host-lanes``. Every test skips when ``LANE_PLATFORM_BASE_URL`` is unset.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
from pathlib import Path
from typing import Any
import uuid

import httpx
from langgraph_sdk import get_sync_client
from langgraph_sdk.client import SyncLangGraphClient
from langgraph_sdk.errors import ConflictError, InternalServerError, NotFoundError
import pytest

BASE_URL = os.environ.get("LANE_PLATFORM_BASE_URL", "").rstrip("/")
MANIFEST = json.loads(
    (Path(__file__).parents[1] / "data" / "platform_compat_manifest.json").read_text("utf-8")
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not BASE_URL,
        reason="LANE_PLATFORM_BASE_URL not set; the lane is started by make host-lanes",
    ),
]

# Every supported or partial manifest operation and the test that exercises it, so a new manifest
# entry has to be given a scenario here (tests/test_host_lanes_sdk_coverage.py enforces it).
COVERED_OPERATIONS: dict[str, str] = {
    "assistants.search": "test_assistants_search_get_and_count",
    "assistants.get": "test_assistants_search_get_and_count",
    "assistants.count": "test_assistants_search_get_and_count",
    "metadata` filter on `search`/`count": "test_thread_search_and_count_filters",
    "threads.create": "test_thread_create_get_update_delete",
    "threads.get": "test_thread_create_get_update_delete",
    "threads.update": "test_thread_create_get_update_delete",
    "threads.delete": "test_thread_create_get_update_delete",
    "threads.search": "test_thread_search_and_count_filters",
    "threads.count": "test_thread_search_and_count_filters",
    "runs.wait` (threaded)": "test_runs_wait_accumulates_state_across_requests",
    "runs.wait` (threadless via `runs/wait`)": "test_threadless_runs_do_not_touch_threads",
    "runs.stream` (threaded + threadless)": "test_runs_stream_emits_ordered_events",
    "threads.get_state": "test_state_update_and_history",
    "threads.update_state": "test_state_update_and_history",
    "threads.get_history": "test_state_update_and_history",
}

RUN_FIELD_VALUES: dict[str, Any] = {
    "interrupt_before": ["greet"],
    "interrupt_after": ["greet"],
    "webhook": "https://example.com/hook",
    "checkpoint_id": "checkpoint-1",
    "command": {"resume": "yes"},
    "on_completion": "delete",
    "after_seconds": 1,
    "if_not_exists": "create",
    "feedback_keys": ["quality"],
}


@pytest.fixture(scope="module")
def client() -> SyncLangGraphClient:
    return get_sync_client(url=f"{BASE_URL}/api")


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"{BASE_URL}/api", timeout=60) as session:
        yield session


@pytest.fixture
def token() -> str:
    return uuid.uuid4().hex


def _input(text: str, *, fresh: bool = True) -> dict[str, Any]:
    return {"user_text": text, "history": [], "turn_count": 0} if fresh else {"user_text": text}


def test_assistants_search_get_and_count(client: SyncLangGraphClient) -> None:
    # Given: one registered graph; When: it is listed, fetched and counted
    found = client.assistants.search(limit=10, offset=0)
    assistant = client.assistants.get("agent")

    # Then: it is the only assistant and every filter agrees
    assert [item["assistant_id"] for item in found] == ["agent"]
    assert (assistant["graph_id"], assistant["name"]) == ("agent", "agent")
    assert "created_at" in assistant and "updated_at" in assistant
    assert client.assistants.count() == 1
    assert client.assistants.count(graph_id="agent") == 1
    assert client.assistants.count(graph_id="nonexistent") == 0
    assert client.assistants.count(name="AGE") == 1
    assert client.assistants.count(metadata={"key": "value"}) == 0
    with pytest.raises(NotFoundError):
        client.assistants.get("nonexistent")


def test_thread_create_get_update_delete(client: SyncLangGraphClient, token: str) -> None:
    # Given: a new thread with nested metadata
    created = client.threads.create(metadata={"suite": token, "nested": {"x": 1}})
    thread_id = created["thread_id"]
    assert created["status"] == "idle"
    assert "created_at" in created and "updated_at" in created
    assert client.threads.get(thread_id)["thread_id"] == thread_id

    # When: metadata is updated (shallow merge, a nested value is replaced, not merged)
    merged = client.threads.update(thread_id, metadata={"other": 2, "nested": {"y": 2}})

    # Then: keys are merged shallowly
    assert merged["metadata"] == {"suite": token, "other": 2, "nested": {"y": 2}}

    # And: delete removes it once; a second delete, a get and an update answer 404
    client.threads.delete(thread_id)
    with pytest.raises(NotFoundError):
        client.threads.get(thread_id)
    with pytest.raises(NotFoundError):
        client.threads.delete(thread_id)
    with pytest.raises(NotFoundError):
        client.threads.update(thread_id, metadata={"a": 1})
    with pytest.raises(NotFoundError):
        client.threads.delete("never-existed")


def test_thread_search_and_count_filters(client: SyncLangGraphClient, token: str) -> None:
    # Given: five threads of this test, three of them "prod"
    for env in ("prod", "prod", "prod", "dev", "dev"):
        client.threads.create(metadata={"suite": token, "env": env})
    mine = {"suite": token}

    # When/Then: metadata, status, limit and offset filters agree with the counts
    assert len(client.threads.search(metadata=mine)) == 5
    assert len(client.threads.search(metadata={**mine, "env": "prod"})) == 3
    assert len(client.threads.search(metadata=mine, status="idle")) == 5
    assert len(client.threads.search(metadata=mine, limit=2)) == 2
    assert len(client.threads.search(metadata=mine, limit=10, offset=4)) == 1
    assert client.threads.count(metadata=mine) == 5
    assert client.threads.count(metadata={**mine, "env": "dev"}) == 2
    assert client.threads.count(metadata=mine, status="idle") == 5
    assert client.threads.count(metadata=mine, status="busy") == 0


@pytest.mark.parametrize("endpoint", ["search", "count"])
@pytest.mark.parametrize(
    ("field", "status"),
    [(entry["field"], entry["status"]) for entry in MANIFEST["unsupported_thread_filter_fields"]],
)
def test_unsupported_thread_filters_answer_501(
    http: httpx.Client, endpoint: str, field: str, status: int
) -> None:
    # When: an unsupported filter field is sent; Then: the documented status comes back
    assert http.post(f"/threads/{endpoint}", json={field: []}).status_code == status


def test_runs_wait_accumulates_state_across_requests(
    client: SyncLangGraphClient, token: str
) -> None:
    # Given: a thread, and two runs in separate requests
    thread_id = client.threads.create(metadata={"suite": token})["thread_id"]

    # When: the runs execute through the Table run lock and the Blob checkpointer
    first = client.runs.wait(thread_id, "agent", input=_input("Alice"))
    second = client.runs.wait(thread_id, "agent", input=_input("Bob", fresh=False))

    # Then: state accumulates across requests and the thread is idle again
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["turn_count"] == 1 and first["last_reply"] == "Hello, Alice!"
    assert second["turn_count"] == 2
    assert second["history"] == ["Hello, Alice!", "Hello, Bob!"]
    assert client.threads.get(thread_id)["status"] == "idle"


def test_runs_stream_emits_ordered_events(client: SyncLangGraphClient) -> None:
    # Given: a thread; When: a run is streamed in values mode over real SSE
    thread_id = client.threads.create()["thread_id"]
    events = list(
        client.runs.stream(thread_id, "agent", input=_input("Stream"), stream_mode="values")
    )

    # Then: metadata comes first, end last, and the final values event is the result
    assert events[0].event == "metadata" and events[-1].event == "end"
    assert events[0].data is not None and "run_id" in events[0].data
    values = [event.data for event in events if event.event == "values"]
    assert values and values[-1]["turn_count"] == 1
    assert values[-1]["history"] == ["Hello, Stream!"]


def test_threadless_runs_do_not_touch_threads(client: SyncLangGraphClient) -> None:
    # Given: the number of threads before; When: two threadless waits and one stream run
    before = client.threads.count()
    first = client.runs.wait(None, "agent", input=_input("Alice"))
    second = client.runs.wait(None, "agent", input=_input("Bob"))
    streamed = list(client.runs.stream(None, "agent", input=_input("Stream"), stream_mode="values"))

    # Then: nothing accumulates between runs and no thread was created
    assert isinstance(first, dict) and isinstance(second, dict)
    assert (first["turn_count"], first["history"]) == (1, ["Hello, Alice!"])
    assert (second["turn_count"], second["history"]) == (1, ["Hello, Bob!"])
    assert streamed[0].event == "metadata" and streamed[-1].event == "end"
    assert client.threads.count() == before


def test_state_update_and_history(client: SyncLangGraphClient) -> None:
    # Given: a thread that never ran; Then: state operations answer 409
    unbound = client.threads.create()["thread_id"]
    with pytest.raises(ConflictError):
        client.threads.get_state(unbound)
    with pytest.raises(ConflictError):
        client.threads.update_state(unbound, values={"key": "val"})
    with pytest.raises(ConflictError):
        client.threads.get_history(unbound)

    # And: after two runs the persisted state, an update and the history are readable
    thread_id = client.threads.create()["thread_id"]
    client.runs.wait(thread_id, "agent", input=_input("A"))
    client.runs.wait(thread_id, "agent", input=_input("B", fresh=False))
    values = client.threads.get_state(thread_id)["values"]
    assert isinstance(values, dict) and values["turn_count"] == 2
    updated = client.threads.update_state(
        thread_id, values={"last_reply": "Overridden"}, as_node="count"
    )
    assert updated["checkpoint"]["thread_id"] == thread_id
    after = client.threads.get_state(thread_id)["values"]
    assert isinstance(after, dict) and after["last_reply"] == "Overridden"
    history = client.threads.get_history(thread_id)
    assert len(history) >= 2
    assert all("values" in entry and "checkpoint" in entry for entry in history)
    assert len(client.threads.get_history(thread_id, limit=2)) <= 2


def test_unsupported_run_options_answer_501(
    client: SyncLangGraphClient, http: httpx.Client
) -> None:
    # Given: a thread
    thread_id = client.threads.create()["thread_id"]

    # When/Then: options the package does not support are refused through the SDK ...
    with pytest.raises(httpx.HTTPStatusError, match="501"):
        client.runs.wait(thread_id, "agent", input=_input("x"), interrupt_before=["greet"])
    with pytest.raises(httpx.HTTPStatusError, match="501"):
        client.runs.wait(thread_id, "agent", input=_input("x"), interrupt_after=["greet"])
    with pytest.raises(httpx.HTTPStatusError, match="501"):
        client.runs.wait(thread_id, "agent", input=_input("x"), webhook="https://example.com/hook")
    with pytest.raises(InternalServerError):
        list(
            client.runs.stream(
                thread_id, "agent", input=_input("x"), stream_mode=["values", "updates"]
            )
        )

    # ... and every field of the manifest answers the documented status over raw HTTP
    for entry in MANIFEST["unsupported_run_create_fields"]:
        field = entry["field"]
        response = http.post(
            f"/threads/{thread_id}/runs/wait",
            json={"assistant_id": "agent", "input": {}, field: RUN_FIELD_VALUES[field]},
        )
        assert response.status_code == entry["status"], field
