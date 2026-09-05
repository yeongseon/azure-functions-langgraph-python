"""Azurite-backed persistence test for the production_persistent_agent example.

Proves the example's core promise: graph state persisted through
``AzureBlobCheckpointSaver`` **survives recreation of the app/checkpointer
object** against the same underlying storage — i.e. it is durable, not just
in-process memory.

The test uses a deterministic fake model (no ``AZURE_OPENAI_*`` env vars, so
``graph.build_chat_model`` returns ``GenericFakeChatModel``) and a live Azurite
blob backend. It is marked ``integration`` and skips cleanly when Azurite is
unavailable, so the default unit-test run is unaffected.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import sys
from typing import Any, Iterator
import uuid

import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver

pytestmark = pytest.mark.integration

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "production_persistent_agent"

# Blob endpoint on Azurite's default blob port (10000). Well-known devstore key.
AZURITE_BLOB_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1"
)


def _load_example_graph_module() -> Any:
    """Import the example's graph.py as an isolated module (fake-model path)."""
    path = EXAMPLE_DIR / "graph.py"
    spec = importlib.util.spec_from_file_location("_production_persistent_agent_graph", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


@pytest.fixture
def azurite_container_client() -> Iterator[Any]:
    """Yield a fresh, uniquely-named live Azurite blob ContainerClient."""
    try:
        blob_module = importlib.import_module("azure.storage.blob")
        blob_service_client = blob_module.BlobServiceClient.from_connection_string(
            AZURITE_BLOB_CONNECTION_STRING
        )
        # Probe connectivity so an unavailable Azurite skips rather than errors.
        _ = blob_service_client.get_service_properties()
    except Exception as exc:  # pragma: no cover - only when Azurite is absent
        pytest.skip(f"Azurite Blob Storage not available: {exc}")

    container_name = f"aflgprod{uuid.uuid4().hex[:18]}"
    container_client = blob_service_client.create_container(container_name)
    try:
        yield container_client
    finally:
        try:
            container_client.delete_container()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass


def _connection_string_container(container_name: str) -> Any:
    """Build a NEW ContainerClient object for the same Azurite container."""
    blob_module = importlib.import_module("azure.storage.blob")
    return blob_module.ContainerClient.from_connection_string(
        AZURITE_BLOB_CONNECTION_STRING, container_name
    )


def test_state_survives_checkpointer_recreation(azurite_container_client: Any) -> None:
    """State written by one app/checkpointer is readable after full recreation."""
    graph_mod = _load_example_graph_module()
    container_name = azurite_container_client.container_name
    thread_id = f"demo-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    # --- App instance #1: write a turn through a Blob-backed checkpointer. ---
    saver_1 = AzureBlobCheckpointSaver(container_client=azurite_container_client)
    graph_1 = graph_mod.build_graph().compile(checkpointer=saver_1)
    graph_1.invoke(
        {"messages": [{"role": "human", "content": "My name is Ada."}]},
        config=config,
    )

    state_1 = graph_1.get_state(config)
    contents_1 = [m.content for m in state_1.values["messages"] if getattr(m, "content", "")]
    assert any("Ada" in c for c in contents_1), contents_1

    # --- Simulate a restart: brand-new container client, checkpointer, and
    #     compiled graph objects — nothing shared with instance #1 except the
    #     underlying Azurite storage. ---
    fresh_container = _connection_string_container(container_name)
    saver_2 = AzureBlobCheckpointSaver(container_client=fresh_container)
    graph_2 = graph_mod.build_graph().compile(checkpointer=saver_2)

    # The "My name is Ada." turn must still be there — loaded from Blob.
    state_2 = graph_2.get_state(config)
    contents_2 = [m.content for m in state_2.values["messages"] if getattr(m, "content", "")]
    assert any("Ada" in c for c in contents_2), contents_2

    # Continuing the same thread on the recreated app appends to prior history.
    graph_2.invoke(
        {"messages": [{"role": "human", "content": "What is my name?"}]},
        config=config,
    )
    state_3 = graph_2.get_state(config)
    human_turns = [
        m.content for m in state_3.values["messages"] if getattr(m, "type", None) == "human"
    ]
    assert any("Ada" in c for c in human_turns)
    assert any("What is my name" in c for c in human_turns)


def test_threads_are_isolated(azurite_container_client: Any) -> None:
    """A different thread_id starts empty — no cross-thread leakage."""
    graph_mod = _load_example_graph_module()
    saver = AzureBlobCheckpointSaver(container_client=azurite_container_client)
    graph = graph_mod.build_graph().compile(checkpointer=saver)

    graph.invoke(
        {"messages": [{"role": "human", "content": "Secret for thread A."}]},
        config={"configurable": {"thread_id": "thread-a"}},
    )

    other_state = graph.get_state({"configurable": {"thread_id": "thread-b"}})
    # thread-b has never been written — no checkpoint, empty values.
    assert not other_state.values.get("messages")
