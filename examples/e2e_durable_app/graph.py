"""Evidence-recording graph for the Durable real-Azure restart/recovery e2e (#811).

Shape::

    START -> enter -> hold -> effect -> END

* ``enter`` records one *attempt marker* blob per Activity execution, so the test can count how
  many times the single Activity actually ran (a host restart may or may not redeliver it; the
  test records what happened and never assumes either outcome).
* ``hold`` blocks for ``hold_seconds`` (from the run input) so a restart / terminate can be
  injected while the Activity is in flight, then records a ``completed`` marker. This shows when
  an Activity really finishes after a terminate (Durable terminate does not interrupt it).
* ``effect`` is a non-idempotent-tool substitute guarded by an idempotency key: every call is
  logged, but only the first call per key "applies" (create-if-absent blob). Calls minus applied
  = effects the key suppressed.

Evidence lives in a blob container in the app's own storage account (``AzureWebJobsStorage``) so
it survives host restarts. No secrets, request bodies or hostnames are stored: instance ids are
reduced to a short SHA-256 prefix.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, TypedDict
import uuid

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContainerClient
from langgraph.graph import END, START, StateGraph

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver

EVIDENCE_CONTAINER = os.environ.get("E2E_EVIDENCE_CONTAINER", "e2e-durable-evidence")
CHECKPOINT_CONTAINER = os.environ.get("E2E_CHECKPOINT_CONTAINER", "langgraph-checkpoints")
MAX_HOLD_SECONDS = 600.0


class DurableE2EState(TypedDict, total=False):
    run_id: str
    hold_seconds: float
    fail: bool
    entered: str
    completed: str
    effect_applied: bool


def _container(name: str) -> ContainerClient:
    client = ContainerClient.from_connection_string(os.environ["AzureWebJobsStorage"], name)
    try:
        client.create_container()
    except ResourceExistsError:  # pragma: no cover - depends on live Azure state
        pass
    return client


def evidence_container() -> ContainerClient:
    return _container(EVIDENCE_CONTAINER)


def instance_tag() -> str:
    """A short, non-reversible tag of the host instance (never the raw instance id)."""
    raw = os.environ.get("WEBSITE_INSTANCE_ID", "local")
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _mark(prefix: str, key: str) -> None:
    evidence_container().upload_blob(
        f"{prefix}/{key}/{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
        instance_tag().encode(),
    )


def enter(state: DurableE2EState) -> dict[str, Any]:
    run_id = state["run_id"]
    _mark("attempt", run_id)
    return {"entered": run_id}


def hold(state: DurableE2EState) -> dict[str, Any]:
    time.sleep(min(float(state.get("hold_seconds", 0)), MAX_HOLD_SECONDS))
    _mark("completed", state["run_id"])
    return {"completed": state["run_id"]}


def effect(state: DurableE2EState) -> dict[str, Any]:
    if state.get("fail"):
        raise RuntimeError("deliberate e2e failure")
    key = state["run_id"]  # the idempotency key
    _mark("effect-call", key)
    applied = False
    try:
        evidence_container().upload_blob(f"effect-applied/{key}", b"1", overwrite=False)
        applied = True
    except ResourceExistsError:
        pass  # the key already applied this effect: suppressed
    return {"effect_applied": applied}


def count_markers(prefix: str, key: str) -> list[str]:
    """Instance tags of every marker under ``prefix/key`` (one entry per marker)."""
    blobs = evidence_container().list_blobs(name_starts_with=f"{prefix}/{key}/")
    return [evidence_container().download_blob(b.name).readall().decode() for b in blobs]


def effect_applied_count(key: str) -> int:
    return sum(1 for _ in evidence_container().list_blobs(name_starts_with=f"effect-applied/{key}"))


builder = StateGraph(DurableE2EState)
builder.add_node("enter", enter)
builder.add_node("hold", hold)
builder.add_node("effect", effect)
builder.add_edge(START, "enter")
builder.add_edge("enter", "hold")
builder.add_edge("hold", "effect")
builder.add_edge("effect", END)


def build_graph() -> Any:
    saver = AzureBlobCheckpointSaver(container_client=_container(CHECKPOINT_CONTAINER))
    return builder.compile(checkpointer=saver)
