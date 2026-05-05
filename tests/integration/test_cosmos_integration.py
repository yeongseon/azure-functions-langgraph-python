from __future__ import annotations

from collections.abc import Iterator
import importlib
from typing import Callable, Protocol, TypedDict, cast

import pytest

pytest.importorskip("langgraph_checkpoint_cosmosdb")

cosmos_helper_module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
create_cosmos_checkpointer = cast(
    Callable[..., object],
    getattr(cosmos_helper_module, "create_cosmos_checkpointer"),
)
close_cosmos_checkpointer = cast(
    Callable[[object], None],
    getattr(cosmos_helper_module, "close_cosmos_checkpointer"),
)

pytestmark = pytest.mark.integration


class Checkpoint(TypedDict):
    v: int
    id: str
    ts: str
    channel_values: dict[str, list[str]]
    channel_versions: dict[str, str]
    versions_seen: dict[str, str]
    updated_channels: None


class CheckpointTuple(Protocol):
    checkpoint: Checkpoint


class CheckpointSaver(Protocol):
    def put(
        self,
        config: dict[str, dict[str, str]],
        checkpoint: Checkpoint,
        metadata: dict[str, object],
        new_versions: dict[str, str],
    ) -> dict[str, dict[str, str]]: ...

    def get_tuple(self, config: dict[str, dict[str, str]]) -> CheckpointTuple | None: ...

    def list(
        self,
        config: dict[str, dict[str, str]] | None,
        *,
        filter: dict[str, object] | None = None,
        before: dict[str, dict[str, str]] | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]: ...


def _config(
    thread_id: str, checkpoint_ns: str = "", checkpoint_id: str | None = None
) -> dict[str, dict[str, str]]:
    configurable: dict[str, str] = {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _checkpoint(checkpoint_id: str, step: int) -> Checkpoint:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00Z",
        "channel_values": {"messages": [f"m-{step}"]},
        "channel_versions": {"messages": f"v{step}"},
        "versions_seen": {},
        "updated_channels": None,
    }


def _metadata(step: int) -> dict[str, object]:
    return {"source": "loop", "step": step, "run_id": f"run-{step}", "parents": {}}


def _create_saver(target: tuple[str, str, str, str]) -> CheckpointSaver:
    endpoint, key, database_name, container_name = target
    return cast(
        CheckpointSaver,
        create_cosmos_checkpointer(
            endpoint=endpoint,
            key=key,
            database_name=database_name,
            container_name=container_name,
        ),
    )


def test_create_cosmos_checkpointer_creates_saver(
    cosmos_emulator_target: tuple[str, str, str, str],
) -> None:
    saver = _create_saver(cosmos_emulator_target)
    assert saver is not None
    close_cosmos_checkpointer(saver)


def test_close_cosmos_checkpointer_cleans_up(
    cosmos_emulator_target: tuple[str, str, str, str],
) -> None:
    saver = _create_saver(cosmos_emulator_target)
    close_cosmos_checkpointer(saver)
    assert getattr(saver, "_langgraph_closed", False) is True


def test_checkpoint_round_trip_put_get_tuple(
    cosmos_emulator_target: tuple[str, str, str, str],
) -> None:
    saver = _create_saver(cosmos_emulator_target)
    cfg = _config("integration-thread")
    saved_cfg = saver.put(cfg, _checkpoint("cp-001", 1), _metadata(1), {"messages": "v1"})
    result = saver.get_tuple(saved_cfg)
    assert result is not None
    assert result.checkpoint["id"] == "cp-001"
    close_cosmos_checkpointer(saver)


def test_list_checkpoints_returns_expected_results(
    cosmos_emulator_target: tuple[str, str, str, str],
) -> None:
    saver = _create_saver(cosmos_emulator_target)
    cfg = _config("integration-thread-list")
    _ = saver.put(cfg, _checkpoint("cp-001", 1), _metadata(1), {"messages": "v1"})
    _ = saver.put(cfg, _checkpoint("cp-002", 2), _metadata(2), {"messages": "v2"})
    checkpoints = list(saver.list(cfg, limit=10))
    ids = {item.checkpoint["id"] for item in checkpoints}
    assert {"cp-001", "cp-002"}.issubset(ids)
    close_cosmos_checkpointer(saver)
