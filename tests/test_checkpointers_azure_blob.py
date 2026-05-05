"""Unit tests for AzureBlobCheckpointSaver."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import json
import re
import sys
import types
from typing import Any, Iterator, Literal, Protocol, Sequence, cast
from urllib.parse import quote

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata
from langgraph.checkpoint.serde.types import ERROR, INTERRUPT

pytest = cast(Any, importlib.import_module("pytest"))


class _CheckpointSaverProtocol(Protocol):
    def get_next_version(self, current: str | None, channel: None) -> str: ...

    def get_tuple(self, config: RunnableConfig) -> Any: ...

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[Any]: ...

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, str | int | float],
    ) -> RunnableConfig: ...

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None: ...

    def delete_thread(self, thread_id: str) -> None: ...

    def delete_checkpoints_before(
        self,
        thread_id: str,
        *,
        before_checkpoint_id: str,
        checkpoint_ns: str | None = None,
    ) -> int: ...

    def delete_old_checkpoints(
        self,
        thread_id: str,
        *,
        keep_last: int = 20,
        checkpoint_ns: str | None = None,
    ) -> int: ...

    def collect_orphaned_values(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str | None = None,
        dry_run: bool = True,
        grace_period_seconds: int = 300,
    ) -> Any: ...


class FakeResourceNotFoundError(Exception):
    """Raised when a mock blob is not present."""


_DEFAULT_BLOB_LAST_MODIFIED = datetime(2020, 1, 1, tzinfo=timezone.utc)


@dataclass
class _BlobRecord:
    data: bytes
    metadata: dict[str, str]
    last_modified: datetime = field(default=_DEFAULT_BLOB_LAST_MODIFIED)


@dataclass
class _BlobItem:
    name: str
    last_modified: datetime | None = None


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
        if not overwrite and self._blob_name in self._container.blobs:
            raise ValueError("Blob already exists")
        existing = self._container.blobs.get(self._blob_name)
        last_modified = (
            existing.last_modified if existing is not None else _DEFAULT_BLOB_LAST_MODIFIED
        )
        self._container.blobs[self._blob_name] = _BlobRecord(
            data=data, metadata=dict(metadata), last_modified=last_modified
        )

    def download_blob(self) -> _MockDownloadStream:
        record = self._container.blobs.get(self._blob_name)
        if record is None:
            raise FakeResourceNotFoundError(self._blob_name)
        return _MockDownloadStream(record.data)

    def get_blob_properties(self) -> _MockBlobProperties:
        record = self._container.blobs.get(self._blob_name)
        if record is None:
            raise FakeResourceNotFoundError(self._blob_name)
        return _MockBlobProperties(metadata=dict(record.metadata))

    def delete_blob(self) -> None:
        if self._blob_name not in self._container.blobs:
            raise FakeResourceNotFoundError(self._blob_name)
        del self._container.blobs[self._blob_name]


class MockContainerClient:
    def __init__(self) -> None:
        self.blobs: dict[str, _BlobRecord] = {}

    def get_blob_client(self, blob: str) -> MockBlobClient:
        return MockBlobClient(self, blob)

    def list_blobs(self, name_starts_with: str = "") -> list[_BlobItem]:
        return [
            _BlobItem(name=name, last_modified=record.last_modified)
            for name, record in sorted(self.blobs.items())
            if name.startswith(name_starts_with)
        ]


@pytest.fixture(autouse=True)  # type: ignore[untyped-decorator]
def _install_fake_azure_modules(monkeypatch: Any) -> None:
    azure_mod = types.ModuleType("azure")
    azure_storage_mod = types.ModuleType("azure.storage")
    azure_blob_mod = types.ModuleType("azure.storage.blob")
    setattr(azure_blob_mod, "ContainerClient", MockContainerClient)

    azure_core_mod = types.ModuleType("azure.core")
    azure_core_exceptions_mod = types.ModuleType("azure.core.exceptions")
    setattr(azure_core_exceptions_mod, "ResourceNotFoundError", FakeResourceNotFoundError)

    monkeypatch.setitem(sys.modules, "azure", azure_mod)
    monkeypatch.setitem(sys.modules, "azure.storage", azure_storage_mod)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", azure_blob_mod)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core_mod)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions_mod)


@pytest.fixture  # type: ignore[untyped-decorator]
def saver_and_container() -> tuple[_CheckpointSaverProtocol, MockContainerClient]:
    container = MockContainerClient()
    module = importlib.import_module("azure_functions_langgraph.checkpointers.azure_blob")
    saver_cls = getattr(module, "AzureBlobCheckpointSaver")
    saver = cast(_CheckpointSaverProtocol, saver_cls(container_client=container))
    return saver, container


def _config(
    thread_id: str = "thread-1",
    checkpoint_ns: str = "",
    checkpoint_id: str | None = None,
) -> RunnableConfig:
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        }
    }
    if checkpoint_id is not None:
        config["configurable"]["checkpoint_id"] = checkpoint_id
    return config


def _checkpoint(
    checkpoint_id: str,
    *,
    channel_values: dict[str, Any] | None = None,
    channel_versions: dict[str, str | int | float] | None = None,
) -> Checkpoint:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00Z",
        "channel_values": channel_values or {},
        "channel_versions": channel_versions or {},
        "versions_seen": {},
        "updated_channels": None,
    }


def _metadata(
    *,
    source: Literal["input", "loop", "update", "fork"] = "loop",
    step: int = 1,
    run_id: str = "run-1",
) -> CheckpointMetadata:
    return {
        "source": source,
        "step": step,
        "run_id": run_id,
        "parents": {},
    }


def test_path_escaping(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="thread/a b", checkpoint_ns="ns/x y")
    checkpoint = _checkpoint(
        "cp/001",
        channel_values={"ch/name": "value"},
        channel_versions={"ch/name": "v/1"},
    )
    saver.put(cfg, checkpoint, _metadata(), {"ch/name": "v/1"})
    saver.put_writes(
        _config(thread_id="thread/a b", checkpoint_ns="ns/x y", checkpoint_id="cp/001"),
        [("result/channel", {"ok": True})],
        task_id="task/1",
    )

    escaped_thread = quote("thread/a b", safe="")
    escaped_ns = quote("ns/x y", safe="")
    escaped_checkpoint = quote("cp/001", safe="")
    escaped_channel = quote("ch/name", safe="")
    escaped_version = quote("v/1", safe="")
    escaped_task_id = quote("task/1", safe="")

    assert (
        f"threads/{escaped_thread}/ns/{escaped_ns}/checkpoints/{escaped_checkpoint}/checkpoint.bin"
    ) in container.blobs
    assert (
        f"threads/{escaped_thread}/ns/{escaped_ns}/values/{escaped_channel}/{escaped_version}.bin"
    ) in container.blobs
    assert (
        f"threads/{escaped_thread}/ns/{escaped_ns}/checkpoints/{escaped_checkpoint}/"
        f"writes/{escaped_task_id}/0.bin"
    ) in container.blobs


def test_get_next_version(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container

    version_1 = saver.get_next_version(None, None)
    version_2 = saver.get_next_version(version_1, None)

    assert re.fullmatch(r"\d{32}\.0\.\d+", version_1) is not None
    assert re.fullmatch(r"\d{32}\.0\.\d+", version_2) is not None
    assert int(version_2.split(".", maxsplit=1)[0]) == int(version_1.split(".", maxsplit=1)[0]) + 1


def test_put_and_get_tuple(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="")
    checkpoint = _checkpoint(
        "cp-001",
        channel_values={"messages": ["hello"]},
        channel_versions={"messages": "v1"},
    )

    saved_config = saver.put(cfg, checkpoint, _metadata(), {"messages": "v1"})
    result = saver.get_tuple(saved_config)

    assert result is not None
    assert result.config == saved_config
    assert result.checkpoint["id"] == "cp-001"
    assert result.checkpoint["channel_values"] == {"messages": ["hello"]}
    assert result.metadata["run_id"] == "run-1"
    assert result.parent_config is None
    assert result.pending_writes == []


def test_stale_latest_json_returns_actual_latest(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    """When latest.json is stale, get_tuple must return the actual latest checkpoint."""
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")

    saver.put(cfg, _checkpoint("cp-001"), _metadata(step=1), {})
    saver.put(cfg, _checkpoint("cp-002"), _metadata(step=2), {})

    # Manually set latest.json to the OLDER checkpoint (simulating stale hint)
    latest_path = "threads/t-1/ns/ns/latest.json"
    container.get_blob_client(latest_path).upload_blob(
        json.dumps({"checkpoint_id": "cp-001"}).encode(),
        metadata={},
        overwrite=True,
    )

    result = saver.get_tuple(_config(thread_id="t-1", checkpoint_ns="ns"))
    assert result is not None
    # Must return cp-002 (actual latest), NOT cp-001 (stale hint)
    assert result.checkpoint["id"] == "cp-002"


def test_get_tuple_no_checkpoint_returns_none(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    assert saver.get_tuple(_config(thread_id="missing", checkpoint_ns="ns")) is None


def test_get_tuple_by_checkpoint_id(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(step=1), {})
    saver.put(cfg, _checkpoint("cp-002"), _metadata(step=2), {})

    result = saver.get_tuple(_config(thread_id="t-1", checkpoint_ns="ns", checkpoint_id="cp-001"))
    assert result is not None
    assert result.checkpoint["id"] == "cp-001"


def test_get_tuple_fallback_when_latest_missing(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(step=1), {})
    saver.put(cfg, _checkpoint("cp-002"), _metadata(step=2), {})
    del container.blobs["threads/t-1/ns/ns/latest.json"]

    result = saver.get_tuple(_config(thread_id="t-1", checkpoint_ns="ns"))
    assert result is not None
    assert result.checkpoint["id"] == "cp-002"


def test_list_checkpoints(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(step=1), {})
    saver.put(cfg, _checkpoint("cp-002"), _metadata(step=2), {})
    saver.put(cfg, _checkpoint("cp-003"), _metadata(step=3), {})

    checkpoints = list(saver.list(_config(thread_id="t-1", checkpoint_ns="ns"), limit=2))
    assert [item.checkpoint["id"] for item in checkpoints] == ["cp-003", "cp-002"]


def test_list_with_before_filter(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(step=1), {})
    saver.put(cfg, _checkpoint("cp-002"), _metadata(step=2), {})
    saver.put(cfg, _checkpoint("cp-003"), _metadata(step=3), {})

    checkpoints = list(
        saver.list(
            _config(thread_id="t-1", checkpoint_ns="ns"),
            before=_config(thread_id="t-1", checkpoint_ns="ns", checkpoint_id="cp-003"),
        )
    )
    assert [item.checkpoint["id"] for item in checkpoints] == ["cp-002", "cp-001"]


def test_list_with_metadata_filter(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(run_id="run-1"), {})
    saver.put(cfg, _checkpoint("cp-002"), _metadata(run_id="run-2"), {})

    checkpoints = list(
        saver.list(_config(thread_id="t-1", checkpoint_ns="ns"), filter={"run_id": "run-2"})
    )
    assert [item.checkpoint["id"] for item in checkpoints] == ["cp-002"]


def test_put_writes_and_retrieve(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})

    write_config = _config(thread_id="t-1", checkpoint_ns="ns", checkpoint_id="cp-001")
    saver.put_writes(write_config, [("alpha", 1), ("beta", 2)], task_id="task-1")

    result = saver.get_tuple(write_config)
    assert result is not None
    assert result.pending_writes == [("task-1", "alpha", 1), ("task-1", "beta", 2)]


def test_put_writes_dedup(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})

    write_config = _config(thread_id="t-1", checkpoint_ns="ns", checkpoint_id="cp-001")
    writes = [("alpha", 1), ("beta", 2)]
    saver.put_writes(write_config, writes, task_id="task-1")
    saver.put_writes(write_config, writes, task_id="task-1")

    result = saver.get_tuple(write_config)
    assert result is not None
    assert result.pending_writes == [("task-1", "alpha", 1), ("task-1", "beta", 2)]


def test_put_writes_special_types(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})

    write_config = _config(thread_id="t-1", checkpoint_ns="ns", checkpoint_id="cp-001")
    saver.put_writes(write_config, [(ERROR, "err"), (INTERRUPT, "stop")], task_id="task-1")

    blob_names = set(container.blobs)
    assert "threads/t-1/ns/ns/checkpoints/cp-001/writes/task-1/-1.bin" in blob_names
    assert "threads/t-1/ns/ns/checkpoints/cp-001/writes/task-1/-3.bin" in blob_names

    result = saver.get_tuple(write_config)
    assert result is not None
    assert {(task, channel, value) for task, channel, value in result.pending_writes or []} == {
        ("task-1", ERROR, "err"),
        ("task-1", INTERRUPT, "stop"),
    }


def test_delete_thread(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    saver.put(_config(thread_id="t-1", checkpoint_ns="ns"), _checkpoint("cp-001"), _metadata(), {})
    saver.put(_config(thread_id="t-2", checkpoint_ns="ns"), _checkpoint("cp-001"), _metadata(), {})

    saver.delete_thread("t-1")

    assert saver.get_tuple(_config(thread_id="t-1", checkpoint_ns="ns")) is None
    assert saver.get_tuple(_config(thread_id="t-2", checkpoint_ns="ns")) is not None
    assert all(not name.startswith("threads/t-1/") for name in container.blobs)


def _put_sequence(
    saver: _CheckpointSaverProtocol,
    *,
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_ids: Sequence[str],
) -> None:
    for step, cid in enumerate(checkpoint_ids, start=1):
        saver.put(
            _config(thread_id=thread_id, checkpoint_ns=checkpoint_ns),
            _checkpoint(cid),
            _metadata(step=step),
            {},
        )


def test_delete_old_checkpoints_keeps_last_n(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    _put_sequence(
        saver,
        thread_id="t-1",
        checkpoint_ns="ns",
        checkpoint_ids=["cp-001", "cp-002", "cp-003", "cp-004", "cp-005"],
    )

    deleted = saver.delete_old_checkpoints("t-1", keep_last=2, checkpoint_ns="ns")

    assert deleted == 3
    remaining_checkpoint_blobs = sorted(
        name
        for name in container.blobs
        if name.startswith("threads/t-1/ns/ns/checkpoints/") and name.endswith("/checkpoint.bin")
    )
    assert remaining_checkpoint_blobs == [
        "threads/t-1/ns/ns/checkpoints/cp-004/checkpoint.bin",
        "threads/t-1/ns/ns/checkpoints/cp-005/checkpoint.bin",
    ]
    assert "threads/t-1/ns/ns/latest.json" in container.blobs


def test_delete_old_checkpoints_preserves_values_and_latest_pointer(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )

    deleted = saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")
    assert deleted == 1

    value_blobs = sorted(name for name in container.blobs if "/values/k/" in name)
    assert value_blobs == [
        "threads/t-1/ns/ns/values/k/v1.bin",
        "threads/t-1/ns/ns/values/k/v2.bin",
    ]
    latest = saver.get_tuple(cfg)
    assert latest is not None
    assert latest.checkpoint["id"] == "cp-002"


def test_delete_old_checkpoints_sweeps_all_namespaces(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    _put_sequence(
        saver,
        thread_id="t-1",
        checkpoint_ns="ns-a",
        checkpoint_ids=["cp-001", "cp-002", "cp-003"],
    )
    _put_sequence(
        saver,
        thread_id="t-1",
        checkpoint_ns="ns-b",
        checkpoint_ids=["cp-001", "cp-002"],
    )

    deleted = saver.delete_old_checkpoints("t-1", keep_last=1)

    assert deleted == 3


def test_delete_old_checkpoints_rejects_negative_keep_last(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    with pytest.raises(ValueError):
        saver.delete_old_checkpoints("t-1", keep_last=-1)


def test_delete_checkpoints_before_strict_cutoff(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    _put_sequence(
        saver,
        thread_id="t-1",
        checkpoint_ns="ns",
        checkpoint_ids=["cp-001", "cp-002", "cp-003"],
    )

    deleted = saver.delete_checkpoints_before(
        "t-1", before_checkpoint_id="cp-002", checkpoint_ns="ns"
    )

    assert deleted == 1
    remaining_checkpoint_blobs = sorted(
        name
        for name in container.blobs
        if name.startswith("threads/t-1/ns/ns/checkpoints/") and name.endswith("/checkpoint.bin")
    )
    assert remaining_checkpoint_blobs == [
        "threads/t-1/ns/ns/checkpoints/cp-002/checkpoint.bin",
        "threads/t-1/ns/ns/checkpoints/cp-003/checkpoint.bin",
    ]


def test_channel_value_dedup(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")

    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"count": 1}, channel_versions={"count": "v1"}),
        _metadata(step=1),
        {"count": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"count": 1}, channel_versions={"count": "v1"}),
        _metadata(step=2),
        {"count": "v1"},
    )

    value_blobs = [name for name in container.blobs if "/values/count/v1.bin" in name]
    assert len(value_blobs) == 1


def test_empty_checkpoint_ns(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, _ = saver_and_container
    cfg: RunnableConfig = {
        "configurable": {
            "thread_id": "thread-default-ns",
        }
    }

    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})
    result = saver.get_tuple(cfg)

    assert result is not None
    assert result.config["configurable"]["checkpoint_ns"] == ""
    assert result.checkpoint["id"] == "cp-001"


def test_special_chars_in_ids(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="thread/한 글 🚀", checkpoint_ns="name/space")
    checkpoint = _checkpoint(
        "cp/한글 1",
        channel_values={"messages": ["ok"]},
        channel_versions={"messages": "v/1"},
    )

    saved_config = saver.put(cfg, checkpoint, _metadata(), {"messages": "v/1"})
    result = saver.get_tuple(saved_config)

    assert result is not None
    assert result.checkpoint["id"] == "cp/한글 1"
    assert result.checkpoint["channel_values"] == {"messages": ["ok"]}
    assert any("%ED%95%9C" in blob_name for blob_name in container.blobs)


def test_unicode_parent_id_in_metadata(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    """parent_id containing Unicode is safely URL-encoded in blob metadata."""
    saver, container = saver_and_container
    parent_cfg = _config(thread_id="t-1", checkpoint_ns="", checkpoint_id="parent/한글")
    child_checkpoint = _checkpoint("child-001")
    saver.put(parent_cfg, child_checkpoint, _metadata(), {})

    # Verify the checkpoint blob metadata has URL-encoded parent_id
    cp_blob_path = "threads/t-1/ns//checkpoints/child-001/checkpoint.bin"
    record = container.blobs.get(cp_blob_path)
    assert record is not None
    assert record.metadata["parent_id"] == quote("parent/한글", safe="")

    # get_tuple should decode it back correctly
    result = saver.get_tuple(_config(thread_id="t-1", checkpoint_ns="", checkpoint_id="child-001"))
    assert result is not None
    assert result.parent_config is not None
    assert result.parent_config["configurable"]["checkpoint_id"] == "parent/한글"


def test_unicode_channel_and_task_id_in_writes(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    """channel and task_id containing Unicode are URL-encoded in write blob metadata."""
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="")
    saver.put(cfg, _checkpoint("cp-001"), _metadata(), {})

    write_cfg = _config(thread_id="t-1", checkpoint_ns="", checkpoint_id="cp-001")
    saver.put_writes(write_cfg, [("결과/채널", {"ok": True})], task_id="작업/1")

    # Verify metadata values are URL-encoded (ASCII-safe)
    write_blobs = [
        (name, rec)
        for name, rec in container.blobs.items()
        if "/writes/" in name and name.endswith(".bin")
    ]
    assert len(write_blobs) == 1
    _, write_rec = write_blobs[0]
    assert write_rec.metadata["channel"] == quote("결과/채널", safe="")
    assert write_rec.metadata["task_id"] == quote("작업/1", safe="")

    # get_tuple should decode them back correctly
    result = saver.get_tuple(write_cfg)
    assert result is not None
    assert len(result.pending_writes) == 1
    task_id_out, channel_out, value_out = result.pending_writes[0]
    assert task_id_out == "작업/1"
    assert channel_out == "결과/채널"
    assert value_out == {"ok": True}


def test_latest_json_monotonic_write(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    """latest.json is only updated when the new checkpoint_id >= the current one."""
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")

    # Write cp-002 first (latest.json = cp-002)
    saver.put(cfg, _checkpoint("cp-002"), _metadata(step=2), {})
    latest_path = "threads/t-1/ns/ns/latest.json"
    latest_data = json.loads(container.blobs[latest_path].data.decode())
    assert latest_data["checkpoint_id"] == "cp-002"

    # Write cp-001 (older) — latest.json should NOT be downgraded
    saver.put(cfg, _checkpoint("cp-001"), _metadata(step=1), {})
    latest_data = json.loads(container.blobs[latest_path].data.decode())
    assert latest_data["checkpoint_id"] == "cp-002"  # Still cp-002, not cp-001


def _value_blob_paths(container: MockContainerClient) -> list[str]:
    return sorted(name for name in container.blobs if "/values/" in name)


def test_collect_orphaned_values_no_orphans_when_single_checkpoint(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )

    before = _value_blob_paths(container)
    result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)
    after = _value_blob_paths(container)

    assert result.dry_run is False
    assert result.would_delete == []
    assert result.deleted == []
    assert result.skipped_namespaces == []
    assert before == after


def test_collect_orphaned_values_preserves_shared_versions(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=2),
        {},
    )

    saver.delete_checkpoints_before("t-1", before_checkpoint_id="cp-002", checkpoint_ns="ns")

    result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)

    assert result.would_delete == []
    assert result.deleted == []
    assert "threads/t-1/ns/ns/values/k/v1.bin" in container.blobs


def test_collect_orphaned_values_collects_orphans(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )

    deleted_count = saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")
    assert deleted_count == 1

    pre_paths = _value_blob_paths(container)
    assert "threads/t-1/ns/ns/values/k/v1.bin" in pre_paths
    assert "threads/t-1/ns/ns/values/k/v2.bin" in pre_paths

    result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)

    assert result.would_delete == ["threads/t-1/ns/ns/values/k/v1.bin"]
    assert result.deleted == ["threads/t-1/ns/ns/values/k/v1.bin"]
    assert "threads/t-1/ns/ns/values/k/v1.bin" not in container.blobs
    assert "threads/t-1/ns/ns/values/k/v2.bin" in container.blobs


def test_collect_orphaned_values_dry_run_does_not_delete(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )
    saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")

    blobs_before = dict(container.blobs)

    dry_result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=True)

    assert dry_result.dry_run is True
    assert dry_result.would_delete == ["threads/t-1/ns/ns/values/k/v1.bin"]
    assert dry_result.deleted == []
    assert container.blobs == blobs_before

    real_result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)
    assert real_result.deleted == dry_result.would_delete


def test_collect_orphaned_values_skips_namespace_without_latest(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )
    saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")

    del container.blobs["threads/t-1/ns/ns/latest.json"]

    blobs_before = dict(container.blobs)
    result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)

    assert result.skipped_namespaces == [("t-1", "ns")]
    assert result.would_delete == []
    assert result.deleted == []
    assert container.blobs == blobs_before


def test_collect_orphaned_values_concurrent_write_protects_blob(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    """Concurrent checkpoint write that references a candidate version
    must protect the blob even after it appeared in the snapshot."""
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )
    saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")

    original_collect = saver._collect_retained_versions  # type: ignore[attr-defined]
    call_count = {"n": 0}

    def patched(thread_id: str, checkpoint_ns: str) -> set[tuple[str, str]] | None:
        retained: set[tuple[str, str]] | None = original_collect(thread_id, checkpoint_ns)
        if call_count["n"] == 1:
            saver.put(
                cfg,
                _checkpoint(
                    "cp-003",
                    channel_values={"k": 1},
                    channel_versions={"k": "v1"},
                ),
                _metadata(step=3),
                {},
            )
            retained = original_collect(thread_id, checkpoint_ns)
        call_count["n"] += 1
        return retained

    saver._collect_retained_versions = patched  # type: ignore[attr-defined]

    result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)

    assert "threads/t-1/ns/ns/values/k/v1.bin" in result.would_delete
    assert result.deleted == []
    assert "threads/t-1/ns/ns/values/k/v1.bin" in container.blobs


def test_collect_orphaned_values_sweeps_all_namespaces(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    for ns in ("ns-a", "ns-b"):
        cfg = _config(thread_id="t-1", checkpoint_ns=ns)
        saver.put(
            cfg,
            _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
            _metadata(step=1),
            {"k": "v1"},
        )
        saver.put(
            cfg,
            _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
            _metadata(step=2),
            {"k": "v2"},
        )
        saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns=ns)

    result = saver.collect_orphaned_values("t-1", dry_run=False)

    assert sorted(result.deleted) == [
        "threads/t-1/ns/ns-a/values/k/v1.bin",
        "threads/t-1/ns/ns-b/values/k/v1.bin",
    ]


def _backdate_value_blobs(container: MockContainerClient, when: datetime) -> None:
    """Backdate every values/* blob so the grace-period filter does not skip them.

    Tests that exercise the orphan-collection logic on freshly-written
    blobs need to opt out of the recent-write grace period; bulk-setting
    last_modified is the cleanest way to do that without plumbing a
    clock through every test fixture.
    """
    for name, record in container.blobs.items():
        if "/values/" in name:
            record.last_modified = when


def test_collect_orphaned_values_skips_recent_blobs_inside_grace_window(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )
    saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")
    now = datetime.now(timezone.utc)
    for record in container.blobs.values():
        record.last_modified = now

    result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)

    assert "threads/t-1/ns/ns/values/k/v1.bin" in result.skipped_recent
    assert result.would_delete == []
    assert result.deleted == []
    assert "threads/t-1/ns/ns/values/k/v1.bin" in container.blobs


def test_collect_orphaned_values_grace_period_zero_disables_recent_guard(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )
    saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")
    now = datetime.now(timezone.utc)
    for record in container.blobs.values():
        record.last_modified = now

    result = saver.collect_orphaned_values(
        "t-1", checkpoint_ns="ns", dry_run=False, grace_period_seconds=0
    )

    assert result.skipped_recent == []
    assert "threads/t-1/ns/ns/values/k/v1.bin" in result.deleted
    assert "threads/t-1/ns/ns/values/k/v1.bin" not in container.blobs


def test_collect_orphaned_values_treats_missing_last_modified_as_recent(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    """A blob whose backend does not expose last_modified must not be
    deleted at the default grace period — fail safe rather than guess."""
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )
    saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")
    original_list_blobs = container.list_blobs

    def list_without_timestamp(name_starts_with: str = "") -> list[_BlobItem]:
        return [
            _BlobItem(name=item.name, last_modified=None)
            for item in original_list_blobs(name_starts_with=name_starts_with)
        ]

    container.list_blobs = list_without_timestamp  # type: ignore[method-assign]

    result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)

    assert "threads/t-1/ns/ns/values/k/v1.bin" in result.skipped_recent
    assert result.deleted == []


def test_collect_orphaned_values_fails_closed_on_corrupt_survivor(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    """A surviving checkpoint blob whose payload cannot be deserialized
    makes the survivor set untrustworthy — the entire namespace must be
    skipped rather than risk deleting still-referenced value blobs."""
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )
    saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")
    _backdate_value_blobs(container, datetime(2020, 1, 1, tzinfo=timezone.utc))
    surviving_checkpoint_blob = "threads/t-1/ns/ns/checkpoints/cp-002/checkpoint.bin"
    assert surviving_checkpoint_blob in container.blobs
    container.blobs[surviving_checkpoint_blob] = _BlobRecord(
        data=b"not-a-valid-msgpack-payload",
        metadata=dict(container.blobs[surviving_checkpoint_blob].metadata),
        last_modified=container.blobs[surviving_checkpoint_blob].last_modified,
    )

    result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)

    assert ("t-1", "ns") in result.skipped_namespaces
    assert result.deleted == []
    assert result.would_delete == []
    assert "threads/t-1/ns/ns/values/k/v1.bin" in container.blobs


def test_collect_orphaned_values_fails_closed_on_missing_serde_metadata(
    saver_and_container: tuple[_CheckpointSaverProtocol, MockContainerClient],
) -> None:
    """When _download_typed_blob returns None for a listed survivor
    (e.g. serde_type metadata stripped by an out-of-band tool) the
    namespace must be skipped, not silently treated as 'no references'."""
    saver, container = saver_and_container
    cfg = _config(thread_id="t-1", checkpoint_ns="ns")
    saver.put(
        cfg,
        _checkpoint("cp-001", channel_values={"k": 1}, channel_versions={"k": "v1"}),
        _metadata(step=1),
        {"k": "v1"},
    )
    saver.put(
        cfg,
        _checkpoint("cp-002", channel_values={"k": 2}, channel_versions={"k": "v2"}),
        _metadata(step=2),
        {"k": "v2"},
    )
    saver.delete_old_checkpoints("t-1", keep_last=1, checkpoint_ns="ns")
    _backdate_value_blobs(container, datetime(2020, 1, 1, tzinfo=timezone.utc))
    surviving_checkpoint_blob = "threads/t-1/ns/ns/checkpoints/cp-002/checkpoint.bin"
    record = container.blobs[surviving_checkpoint_blob]
    stripped_metadata = {k: v for k, v in record.metadata.items() if k != "serde_type"}
    container.blobs[surviving_checkpoint_blob] = _BlobRecord(
        data=record.data,
        metadata=stripped_metadata,
        last_modified=record.last_modified,
    )

    result = saver.collect_orphaned_values("t-1", checkpoint_ns="ns", dry_run=False)

    assert ("t-1", "ns") in result.skipped_namespaces
    assert result.deleted == []
    assert "threads/t-1/ns/ns/values/k/v1.bin" in container.blobs


def test_collect_orphaned_values_result_dataclass_has_skipped_recent_field() -> None:
    """Public-API contract: callers depend on `skipped_recent` to audit
    deferred deletions; verify the field is exported and defaults empty."""
    module = importlib.import_module("azure_functions_langgraph.checkpointers.azure_blob")
    result_cls = getattr(module, "OrphanedValueCollectionResult")
    instance = result_cls(dry_run=True)
    assert hasattr(instance, "skipped_recent")
    assert instance.skipped_recent == []
    package_module = importlib.import_module("azure_functions_langgraph.checkpointers")
    assert getattr(package_module, "OrphanedValueCollectionResult") is result_cls
