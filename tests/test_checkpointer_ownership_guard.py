"""Ownership checks against shared fake blobs, not Azure fencing certification.

One file is intentional: the task requires this complete write-path matrix here.
"""
# allow: SIZE_OK — the requested single-file matrix covers six write entry points.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import threading
from typing import Literal, assert_never

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import WRITES_IDX_MAP
import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY,
    ThreadLeaseLostError,
    bind_ownership_guard,
)
from tests.ownership_guard_support import ScriptedGuard
from tests.test_checkpointers_azure_blob import (
    AsyncMockContainerClient,
    MockContainerClient,
    _checkpoint,
    _config,
    _metadata,
)
from tests.test_checkpointers_azure_blob import (
    _install_fake_azure_modules as _install_fake_azure_modules,
)

pytestmark = pytest.mark.usefixtures("_install_fake_azure_modules")
Operation = Literal["put", "writes"]
PREFIX = "threads/thread-SECRET-7f3a/ns/guard/"
VERSIONS: dict[str, str | int | float] = {"alpha": "v2", "beta": "v2"}
SPECIAL = next(channel for channel, index in WRITES_IDX_MAP.items() if index < 0)


def config() -> RunnableConfig:
    return _config(thread_id="thread-SECRET-7f3a", checkpoint_ns="guard", checkpoint_id="cp-001")


@dataclass(frozen=True, slots=True)
class Store:
    saver: AzureBlobCheckpointSaver
    container: MockContainerClient
    synchronous: bool

    async def put(self, cfg: RunnableConfig, checkpoint_id: str = "cp-002") -> RunnableConfig:
        checkpoint = _checkpoint(
            checkpoint_id,
            channel_values={"alpha": "A", "beta": "B"},
            channel_versions=VERSIONS,
        )
        if self.synchronous:
            return self.saver.put(cfg, checkpoint, _metadata(), VERSIONS)
        return await self.saver.aput(cfg, checkpoint, _metadata(), VERSIONS)

    async def writes(self, cfg: RunnableConfig, writes: Sequence[tuple[str, str]]) -> None:
        if self.synchronous:
            self.saver.put_writes(cfg, writes, "task-7f3a")
        else:
            await self.saver.aput_writes(cfg, writes, "task-7f3a")

    async def write(self, operation: Operation, cfg: RunnableConfig) -> None:
        match operation:
            case "put":
                await self.put(cfg)
            case "writes":
                await self.writes(cfg, [("alpha", "A"), ("beta", "B")])
            case unreachable:
                assert_never(unreachable)


@pytest.fixture(params=["sync", "native", "fallback"])
def store(request: pytest.FixtureRequest) -> Store:
    container = MockContainerClient()
    aio = AsyncMockContainerClient(container) if request.param == "native" else None
    saver = AzureBlobCheckpointSaver(container_client=container, aio_container_client=aio)
    return Store(saver, container, request.param == "sync")


@pytest.mark.parametrize("operation", ["put", "writes"])
async def test_first_check_rejects_without_upload(store: Store, operation: Operation) -> None:
    # Given: a guard reporting a specific loss reason on its first check.
    guard = ScriptedGuard(trip_on=1, reason="superseded")
    cfg = config()
    cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = guard
    # When: either write entry point attempts its first upload.
    with pytest.raises(ThreadLeaseLostError) as caught:
        await store.write(operation, cfg)
    # Then: neither a checkpoint nor a pending write has reached storage.
    assert caught.value.reason == "superseded"
    assert guard.checks == 1
    assert store.container.blobs == {}


async def test_each_put_check_preserves_commit_boundary(store: Store) -> None:
    # Given: a committed predecessor and six distinct upload boundaries for two channels.
    for trip_on in range(1, 7):
        store.container.blobs.clear()
        cfg = config()
        store.saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})
        before = dict(store.container.blobs)
        guard = ScriptedGuard(trip_on=trip_on)
        cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = guard
        # When: ownership is lost immediately before this selected upload.
        with pytest.raises(ThreadLeaseLostError) as caught:
            await store.put(cfg)
        # Then: only earlier uploads survive; the marker alone defines commitment.
        assert caught.value.reason == "lost"
        assert guard.checks == trip_on
        changed = {p for p, record in store.container.blobs.items() if before.get(p) != record}
        assert len(changed) == trip_on - 1
        assert (PREFIX + "checkpoints/cp-002/checkpoint.bin" in changed) == (trip_on == 6)
        reader = AzureBlobCheckpointSaver(container_client=store.container)
        latest_cfg = _config(thread_id="thread-SECRET-7f3a", checkpoint_ns="guard")
        latest = reader.get_tuple(latest_cfg)
        assert latest is not None
        assert latest.checkpoint["id"] == ("cp-002" if trip_on == 6 else "cp-001")
        if trip_on == 6:
            assert json.loads(store.container.blobs[PREFIX + "latest.json"].data)["dirty"] is True
            assert latest.checkpoint["channel_values"] == {"alpha": "A", "beta": "B"}


async def test_write_slots_check_only_uploads_and_preserve_partial_progress(store: Store) -> None:
    # Given: one nonnegative slot already exists, while special slots are overwriteable.
    cfg = config()
    await store.writes(cfg, [("alpha", "original")])
    guard = ScriptedGuard(trip_on=1)
    cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = guard
    before = dict(store.container.blobs)
    # When: a duplicate-only batch is skipped, then a mixed batch trips mid-list.
    await store.writes(cfg, [("alpha", "ignored")])
    assert guard.checks == 0
    assert store.container.blobs == before
    guard.trip_on = 3
    with pytest.raises(ThreadLeaseLostError) as caught:
        await store.writes(
            cfg, [("alpha", "ignored"), (SPECIAL, "first"), (SPECIAL, "second"), ("beta", "B")]
        )
    # Then: both negative-slot uploads ran; the later ordinary slot did not.
    assert caught.value.reason == "lost"
    assert guard.checks == 3
    writes_prefix = PREFIX + "checkpoints/cp-001/writes/task-7f3a/"
    assert set(store.container.blobs) == {
        writes_prefix + "0.bin",
        writes_prefix + f"{WRITES_IDX_MAP[SPECIAL]}.bin",
    }
    record = store.container.blobs[writes_prefix + f"{WRITES_IDX_MAP[SPECIAL]}.bin"]
    assert store.saver.serde.loads_typed((record.metadata["serde_type"], record.data)) == "second"
    assert store.container.blobs[writes_prefix + "0.bin"] == before[writes_prefix + "0.bin"]


@pytest.mark.parametrize("operation", ["put", "writes"])
async def test_check_immediately_precedes_every_upload(
    store: Store, operation: Operation, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: one ordered log observes the actual guard and upload boundaries.
    events: list[str] = []

    class LoggingGuard:
        def check(self) -> None:
            events.append("check")

    cfg = config()
    if operation == "writes":
        await store.writes(cfg, [("alpha", "original")])
    cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = LoggingGuard()
    upload, aupload = store.saver._upload_blob, store.saver._aupload_blob

    def logged_upload(path: str, data: bytes, metadata: dict[str, str]) -> None:
        events.append("upload")
        upload(path, data, metadata)

    async def logged_aupload(path: str, data: bytes, metadata: dict[str, str]) -> None:
        events.append("upload")
        await aupload(path, data, metadata)

    monkeypatch.setattr(store.saver, "_upload_blob", logged_upload)
    monkeypatch.setattr(store.saver, "_aupload_blob", logged_aupload)
    # When: writing a checkpoint or a batch containing a skipped ordinary slot.
    await store.write(operation, cfg)
    # Then: no extra, omitted, or displaced checks can hide behind a final count.
    assert events == ["check", "upload"] * (6 if operation == "put" else 1)


@pytest.mark.parametrize("operation", ["put", "writes"])
async def test_required_guard_fails_closed_and_accepts_both_sources(
    store: Store, operation: Operation
) -> None:
    # Given: required mode shares the same sync/aio fake clients as the ordinary saver.
    saver = AzureBlobCheckpointSaver(
        container_client=store.container,
        aio_container_client=store.saver._aio_container_client,
        require_ownership_guard=True,
    )
    required = Store(saver, store.container, store.synchronous)
    # When: no guard or an invalid guard is supplied, even with an invalid thread config.
    missing_configs: tuple[RunnableConfig, ...] = ({}, config())
    for cfg in missing_configs:
        with pytest.raises(ThreadLeaseLostError) as caught:
            await required.write(operation, cfg)
        assert caught.value.reason == "missing"
        assert store.container.blobs == {}
    for junk in ("x", {}, None, object(), threading.Event()):
        cfg = config()
        cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = junk
        with pytest.raises(ThreadLeaseLostError) as caught:
            await required.write(operation, cfg)
        assert caught.value.reason == "missing"
        assert store.container.blobs == {}
    # Then: either genuine guard source authorizes all upload checks.
    for scoped in (False, True):
        store.container.blobs.clear()
        guard = ScriptedGuard()
        cfg = config()
        if not scoped:
            cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = guard
        with bind_ownership_guard(guard if scoped else None):
            await required.write(operation, cfg)
        assert guard.checks == (6 if operation == "put" else 2)


@pytest.mark.parametrize("operation", ["put", "writes"])
async def test_default_mode_ignores_missing_and_junk_guards(
    store: Store, operation: Operation
) -> None:
    # Given: opt-in is off, with absent or untrusted private config values.
    configs = [config()]
    for junk in ("x", {}, None, object(), threading.Event()):
        cfg = config()
        cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = junk
        configs.append(cfg)
    for cfg in configs:
        store.container.blobs.clear()
        # When: a legacy unguarded write runs through each entry point.
        await store.write(operation, cfg)
        # Then: real checkpoint state or both pending writes were persisted.
        if operation == "put":
            result = store.saver.get_tuple(
                _config(thread_id="thread-SECRET-7f3a", checkpoint_ns="guard")
            )
            assert result is not None
            assert result.checkpoint["channel_values"] == {"alpha": "A", "beta": "B"}
        else:
            assert len(store.container.blobs) == 2


@pytest.mark.parametrize("operation", ["put", "writes"])
async def test_scoped_guard_wins_and_binding_is_restored(
    store: Store, operation: Operation
) -> None:
    # Given: a tripped scoped guard, optionally competing with a healthy config guard.
    for config_guard in (None, ScriptedGuard()):
        cfg = config()
        if config_guard is not None:
            cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = config_guard
        scoped = ScriptedGuard(trip_on=1, reason="released")
        # When: the scoped binding surrounds the write, including executor fallback.
        with bind_ownership_guard(scoped), pytest.raises(ThreadLeaseLostError) as caught:
            await store.write(operation, cfg)
        # Then: the scoped guard rejected it without consulting the config guard.
        assert caught.value.reason == "released"
        assert scoped.checks == 1
        assert store.container.blobs == {}
        if config_guard is not None:
            assert config_guard.checks == 0
    await store.write(operation, config())
    assert store.container.blobs


async def test_binding_in_another_thread_is_isolated(store: Store) -> None:
    # Given: another live thread holds a tripped binding until explicitly released.
    ready, release = threading.Event(), threading.Event()
    guard = ScriptedGuard(trip_on=1)
    completed: list[bool] = []

    def bind_in_thread() -> None:
        with bind_ownership_guard(guard):
            ready.set()
            completed.append(release.wait(5))

    worker = threading.Thread(target=bind_in_thread)
    worker.start()
    try:
        assert ready.wait(5)
        # When: this context writes while the other context remains bound.
        await store.put(config())
    finally:
        release.set()
        worker.join(5)
    # Then: thread isolation preserves the write without ever checking that guard.
    assert not worker.is_alive()
    assert completed == [True]
    assert guard.checks == 0
    assert PREFIX + "checkpoints/cp-002/checkpoint.bin" in store.container.blobs


async def test_read_and_maintenance_are_unguarded_by_design(store: Store) -> None:
    # Given: committed history and a guard that would reject any check.
    cfg = config()
    for checkpoint_id in ("cp-000", "cp-001", "cp-002"):
        store.saver.put(cfg, _checkpoint(checkpoint_id), _metadata(), {})
    store.saver.put(_config(thread_id="delete-sync"), _checkpoint("cp-001"), _metadata(), {})
    guard = ScriptedGuard(trip_on=1)
    latest_cfg = _config(thread_id="thread-SECRET-7f3a", checkpoint_ns="guard")
    # When: reads, pruning, dry-run GC and deletion run under that binding.
    with bind_ownership_guard(guard):
        latest = store.saver.get_tuple(latest_cfg)
        history = list(store.saver.list(latest_cfg))
        before = store.saver.delete_checkpoints_before(
            "thread-SECRET-7f3a", before_checkpoint_id="cp-001"
        )
        old = store.saver.delete_old_checkpoints("thread-SECRET-7f3a", keep_last=1)
        gc = store.saver.collect_orphaned_values("thread-SECRET-7f3a", dry_run=True)
        async_latest = await store.saver.aget_tuple(latest_cfg)
        store.saver.delete_thread("delete-sync")
        if store.synchronous:
            store.saver.delete_thread("thread-SECRET-7f3a")
        else:
            await store.saver.adelete_thread("thread-SECRET-7f3a")
    # Then: these operations retain their contracts without checking ownership.
    assert latest is not None and latest.checkpoint["id"] == "cp-002"
    assert [item.checkpoint["id"] for item in history] == ["cp-002", "cp-001", "cp-000"]
    assert (before, old) == (1, 1)
    assert gc.deleted == []
    assert async_latest is not None and async_latest.checkpoint["id"] == "cp-002"
    assert guard.checks == 0


async def test_guard_never_leaks_into_metadata_or_returned_config(store: Store) -> None:
    # Given: a genuine guard with a distinctive serialization-sensitive repr.
    cfg = config()
    guard = ScriptedGuard()
    cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = guard
    # When: the guarded checkpoint commits successfully.
    result = await store.put(cfg)
    # Then: only public checkpoint coordinates and metadata reach durable state.
    assert guard.checks == 6
    assert result.get("configurable") == {
        "thread_id": "thread-SECRET-7f3a",
        "checkpoint_ns": "guard",
        "checkpoint_id": "cp-002",
    }
    record = store.container.blobs[PREFIX + "checkpoints/cp-002/metadata.bin"]
    metadata = store.saver.serde.loads_typed((record.metadata["serde_type"], record.data))
    assert isinstance(metadata, dict)
    assert all(not key.startswith("__") for key in metadata)
    assert metadata["source"] == "loop"
    for blob in store.container.blobs.values():
        assert b"OwnershipGuard" not in blob.data
        assert OWNERSHIP_GUARD_CONFIG_KEY.encode() not in blob.data


async def test_non_lease_guard_exception_propagates_with_partial_state(store: Store) -> None:
    # Given: a guard raising the same non-lease exception at each possible boundary.
    fault = ArithmeticError("guard fault")

    class FaultGuard:
        def __init__(self) -> None:
            self.checks = 0

        def check(self) -> None:
            self.checks += 1
            if self.checks == trip_on:
                raise fault

    for trip_on in range(1, 7):
        store.container.blobs.clear()
        store.saver.put(config(), _checkpoint("cp-001"), _metadata(), {})
        before = dict(store.container.blobs)

        cfg = config()
        guard = FaultGuard()
        cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = guard
        # When: the check fails rather than returning an ownership verdict.
        with pytest.raises(ArithmeticError) as caught:
            await store.put(cfg)
        # Then: identity and the ordinary partial-commit rules are preserved.
        assert caught.value is fault
        assert guard.checks == trip_on
        changed = {p for p, record in store.container.blobs.items() if before.get(p) != record}
        assert len(changed) == trip_on - 1
        assert (PREFIX + "checkpoints/cp-002/checkpoint.bin" in changed) == (trip_on == 6)
        latest = store.saver.get_tuple(
            _config(thread_id="thread-SECRET-7f3a", checkpoint_ns="guard")
        )
        assert latest is not None
        assert latest.checkpoint["id"] == ("cp-002" if trip_on == 6 else "cp-001")
    trip_on = 2
    guard = FaultGuard()
    cfg = config()
    cfg.setdefault("configurable", {})[OWNERSHIP_GUARD_CONFIG_KEY] = guard
    before = dict(store.container.blobs)
    with pytest.raises(ArithmeticError) as caught:
        await store.writes(cfg, [("alpha", "A"), ("beta", "B"), ("gamma", "C")])
    assert caught.value is fault
    assert guard.checks == 2
    assert set(store.container.blobs) - set(before) == {
        PREFIX + "checkpoints/cp-001/writes/task-7f3a/0.bin"
    }
