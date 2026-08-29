"""Two-app single-writer exclusivity graph for real-Azure e2e certification (#386).

This graph proves the core value proposition of :class:`AzureBlobLeaseThreadLock`
+ a single-writer checkpointer: two Function App instances invoking the SAME
``thread_id`` concurrently must NOT both mutate the checkpoint. Exactly one
execution enters the mutation section; the other is rejected with HTTP 409.

Shape (deliberately two nodes, not one)::

    START -> enter -> hold -> END

``enter`` writes the ``entered`` marker and returns, so LangGraph persists a
checkpoint *between* super-steps — this is what lets the driving test observe
``entered`` (via ``GET .../threads/{thread_id}/state``) while the winning
execution is still holding the lease inside ``hold``. A single node that wrote
``entered``, slept, then wrote ``completed`` would only checkpoint *after* the
sleep, defeating the deterministic gate.

The compiled graph is wired with an :class:`AzureBlobCheckpointSaver` so state
survives across the two Function Apps (which share one storage account and one
checkpoint container). The distributed lock itself is wired at the app level in
``function_app.py`` via :class:`AzureBlobLeaseThreadLock`.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, TypedDict

from azure.storage.blob import ContainerClient
from langgraph.graph import END, START, StateGraph

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver

# How long the winning execution holds the lease inside the mutation section.
# Must comfortably exceed the driving test's observe-then-fire-loser latency so
# the loser's request lands while the lease is still held. Overridable via app
# setting for tuning without a code change.
HOLD_SECONDS = float(os.environ.get("E2E_LOCK_HOLD_SECONDS", "20"))

# Container names are supplied as app settings so App A and App B receive the
# SAME values and therefore share one lock container and one checkpoint
# container. Pre-created by infra/main.bicep to avoid cold-start create races.
CHECKPOINT_CONTAINER = os.environ.get("E2E_CHECKPOINT_CONTAINER", "langgraph-checkpoints")


class LockState(TypedDict):
    """State channels for the lock-exclusivity graph.

    ``run_id`` is supplied by the caller so the driving test can prove which
    execution won: the final checkpoint must reflect exactly one ``run_id``.
    """

    messages: list[dict[str, str]]
    run_id: str
    entered: str
    completed: str


def enter(state: LockState) -> dict[str, Any]:
    """Record that this execution entered the mutation section.

    Returns immediately so LangGraph checkpoints ``entered`` before ``hold``
    runs — making the marker observable via the state endpoint while the lease
    is still held.
    """
    run_id = state.get("run_id") or uuid.uuid4().hex
    return {"run_id": run_id, "entered": run_id}


def hold(state: LockState) -> dict[str, Any]:
    """Hold the lease long enough to guarantee overlap, then mark completion."""
    time.sleep(HOLD_SECONDS)
    return {"completed": state["run_id"]}


def _build_checkpointer() -> AzureBlobCheckpointSaver:
    """Build the single-writer Blob checkpointer from ``AzureWebJobsStorage``."""
    connection_string = os.environ["AzureWebJobsStorage"]
    container_client = ContainerClient.from_connection_string(
        connection_string, CHECKPOINT_CONTAINER
    )
    # Idempotent — the container is pre-created by Bicep, but tolerate reruns
    # and local (Azurite) reproduction where it may not yet exist.
    try:
        container_client.create_container()
    except Exception:  # pragma: no cover - depends on live Azure state
        # Already exists (or a benign concurrent create). Safe to ignore.
        pass
    return AzureBlobCheckpointSaver(container_client=container_client)


builder = StateGraph(LockState)
builder.add_node("enter", enter)
builder.add_node("hold", hold)
builder.add_edge(START, "enter")
builder.add_edge("enter", "hold")
builder.add_edge("hold", END)

compiled_lock_graph = builder.compile(checkpointer=_build_checkpointer())
