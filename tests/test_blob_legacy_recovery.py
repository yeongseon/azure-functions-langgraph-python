"""Offline recovery runbook over published-wheel bytes, not Azure certification."""

from __future__ import annotations

from collections.abc import Iterator
import json
from typing import Literal
from urllib.parse import quote, unquote

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, get_checkpoint_id
import pytest

from tests.test_blob_format_compat import (
    NEWEST,
    PREV,
    SET_NAMES,
    Blobs,
    SyncContainer,
    _cfg,
    _load_set,
    _norm,
    _saver,
    _snapshot,
)
from tests.test_blob_format_compat import (
    _fake_azure as _fake_azure,
)

Reader = Literal["sync", "async-native", "async-fallback"]


async def _latest(
    saver: BaseCheckpointSaver[str], kind: Reader, config: RunnableConfig
) -> CheckpointTuple | None:
    if kind == "sync":
        return saver.get_tuple(config)
    return await saver.aget_tuple(config)


async def _scan(
    saver: BaseCheckpointSaver[str], kind: Reader, config: RunnableConfig
) -> CheckpointTuple | None:
    if kind == "sync":
        return next(saver.list(config, limit=1), None)
    return await anext(saver.alist(config, limit=1), None)


@pytest.fixture(params=SET_NAMES)
def stale_fleet(request: pytest.FixtureRequest) -> Iterator[tuple[Blobs, str]]:
    """Leave real committed legacy bytes behind synthetic older clean hints.

    The final checkpoint differs in channel state and pending writes from PREV;
    unlike copying a marker under a new name, its payload id matches its path.
    """
    set_name = str(request.param)
    blobs, _ = _load_set(set_name)
    for path in blobs:
        if path.endswith("/latest.json"):
            blobs[path] = (
                json.dumps({"version": 1, "dirty": False, "checkpoint_id": PREV}).encode(),
                {},
            )
    yield blobs, set_name


@pytest.mark.parametrize("kind", ["sync", "async-native", "async-fallback"])
async def test_scan_detects_stale_clean_hint_without_changing_legacy_data(
    stale_fleet: tuple[Blobs, str], kind: Reader
) -> None:
    # Given: a drained fleet with a stale clean hint for every fixture namespace.
    blobs, set_name = stale_fleet
    _, expected = _load_set(set_name)
    before = _snapshot(blobs)
    saver = _saver(blobs, native_async=kind == "async-native")

    for entry in expected["writes"]:
        config = _cfg(entry["thread_id"], entry["checkpoint_ns"])
        hinted = await _latest(saver, kind, config)
        assert hinted is not None
        assert get_checkpoint_id(hinted.config) == PREV

        # When: list without checkpoint_id, filters or before bypasses the hint.
        committed = await _scan(saver, kind, config)

        # Then: a higher committed id is observable, with original payload intact.
        assert committed is not None
        assert get_checkpoint_id(committed.config) == NEWEST
        assert _norm(committed) == entry["checkpoints"][-1]
        assert committed.checkpoint["channel_values"] != hinted.checkpoint["channel_values"]
    assert blobs == before


@pytest.mark.parametrize("kind", ["sync", "async-native", "async-fallback"])
async def test_hint_invalidation_recovers_legacy_state_when_repeated(
    stale_fleet: tuple[Blobs, str], kind: Reader
) -> None:
    # Given: preserve a backup; recovery is explicitly limited to known v1 hints.
    blobs, set_name = stale_fleet
    _, expected = _load_set(set_name)
    before = _snapshot(blobs)
    saver = _saver(blobs, native_async=kind == "async-native")
    container = SyncContainer(blobs)

    for entry in expected["writes"]:
        thread, ns = entry["thread_id"], entry["checkpoint_ns"]
        config = _cfg(thread, ns)
        committed = await _scan(saver, kind, config)
        assert committed is not None
        prefix = f"threads/{quote(thread, safe='')}/ns/{quote(ns, safe='')}/checkpoints/"
        marker_ids = [
            unquote(path[len(prefix) :].split("/")[0])
            for path in blobs
            if path.startswith(prefix) and path.endswith("/checkpoint.bin")
        ]
        assert get_checkpoint_id(committed.config) == max(marker_ids)
        by_id = await _latest(saver, kind, committed.config)
        assert by_id is not None
        assert _norm(by_id) == entry["checkpoints"][-1]
        hint = f"threads/{quote(thread, safe='')}/ns/{quote(ns, safe='')}/latest.json"

        for _ in range(2):
            # When: operator invalidates ONLY this hint (missing means already done).
            try:
                container.get_blob_client(hint).delete_blob()
            except KeyError as missing:
                # The shared in-memory client's missing DELETE raises KeyError,
                # equivalent here to handling only this blob's BlobNotFound.
                assert missing.args == (hint,)
            # Then: unqualified reads recover exactly the scanned state on every run.
            recovered = await _latest(saver, kind, config)
            assert recovered is not None
            assert _norm(recovered) == entry["checkpoints"][-1]
            assert recovered.config == committed.config

    # No checkpoint, value, metadata, pending write or other object was rewritten.
    assert blobs == {
        path: value for path, value in before.items() if not path.endswith("/latest.json")
    }
