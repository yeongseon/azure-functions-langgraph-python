from __future__ import annotations

import binascii
from collections.abc import Callable, Iterator
import importlib
from typing import Protocol, TypedDict, cast

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
    assert getattr(saver, "_cosmos_helper_closed", False) is True


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


def test_malformed_checkpoint_record_raises_from_upstream(
    cosmos_emulator_target: tuple[str, str, str, str],
) -> None:
    # Given: a checkpoint written through the supported saver API, confirmed
    # readable via get_tuple before any corruption is introduced -- this
    # proves the document that will be corrupted is the one get_tuple
    # actually selects (same thread_id/checkpoint_ns/checkpoint_id key the
    # saver constructs internally), not an unrelated document that merely
    # produces a 404-shaped "missing" result.
    saver = _create_saver(cosmos_emulator_target)
    cfg = _config("malformed-thread", checkpoint_id="malformed-checkpoint")
    saved_cfg = saver.put(
        cfg, _checkpoint("malformed-checkpoint", 1), _metadata(1), {"messages": "v1"}
    )
    sanity = saver.get_tuple(saved_cfg)
    assert sanity is not None
    assert sanity.checkpoint["id"] == "malformed-checkpoint"

    # When: the real stored document's payload (and only its payload) is
    # corrupted in place through the raw Cosmos SDK container, preserving
    # its actual id, partition_key and the rest of the document envelope.
    raw_container = getattr(saver, "container")
    checkpoint_id = saved_cfg["configurable"]["checkpoint_id"]
    # The upstream saver stores a composite document id
    # ("checkpoint$<thread_id>$<checkpoint_ns>$<checkpoint_id>"), so the logical
    # checkpoint id is only the suffix of the physical id.
    query = "SELECT * FROM c WHERE ENDSWITH(c.id, @id)"
    parameters = [{"name": "@id", "value": checkpoint_id}]
    matches = list(
        raw_container.query_items(
            query=query, parameters=parameters, enable_cross_partition_query=True
        )
    )
    assert len(matches) == 1, "expected exactly one stored document for the checkpoint just written"
    stored_doc = matches[0]
    assert stored_doc["id"].endswith(checkpoint_id)
    stored_doc["checkpoint"] = "not-a-valid-checkpoint-payload"
    raw_container.upsert_item(stored_doc)

    # Then: the now-malformed persisted record is surfaced on the next read
    # of that exact document, never silently treated as valid state. The
    # upstream serializer base64-decodes the payload first, so this payload
    # fails with binascii.Error (a ValueError subclass).
    with pytest.raises(binascii.Error):
        saver.get_tuple(cfg)
    close_cosmos_checkpointer(saver)


def test_deprecated_credential_param_still_works(
    cosmos_emulator_target: tuple[str, str, str, str],
) -> None:
    """The deprecated credential= param should still work with a string key."""
    endpoint, key, database_name, container_name = cosmos_emulator_target
    with pytest.warns(DeprecationWarning):
        saver = create_cosmos_checkpointer(
            endpoint=endpoint,
            credential=key,
            database_name=database_name,
            container_name=container_name,
        )
    close_cosmos_checkpointer(cast(CheckpointSaver, saver))
