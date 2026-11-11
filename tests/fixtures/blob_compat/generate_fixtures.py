"""Generate Blob checkpoint compatibility fixtures with a *real* saver.

Run this with the Python environment that has the saver under test installed
(for a past release: a throwaway venv with the published wheel; for the
current tree: ``PYTHONPATH=src``). It drives the real
``AzureBlobCheckpointSaver`` (synchronous and native-async) against an
in-memory container and dumps every blob (name, metadata, bytes) to JSON.

    python generate_fixtures.py --label 0.12.2 --out <dir>

The scenario is deterministic: fixed thread/checkpoint ids and timestamps, a
seeded ``random`` for the saver's own version suffix, simple msgpack-able
values. The committed ``expected.json`` is derived from the scenario inputs,
not from reading the blobs back, so it is independent evidence.

This script is a maintainer tool; tests never execute it.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib
import importlib.metadata as md
import inspect
import json
from pathlib import Path
import platform
import random
from typing import Any

from langgraph.checkpoint.base import WRITES_IDX_MAP

TS = "2026-01-01T00:00:00+00:00"


class _Props:
    def __init__(self, metadata: dict[str, str]) -> None:
        self.metadata = metadata


class _Item:
    def __init__(self, name: str) -> None:
        self.name = name
        self.last_modified = None


class _Stream:
    """Mirrors ``StorageStreamDownloader``: ``properties.metadata`` comes with the body."""

    def __init__(self, data: bytes, metadata: dict[str, str]) -> None:
        self._data = data
        self.properties = _Props(dict(metadata))

    def readall(self) -> bytes:
        return self._data


class _AStream:
    def __init__(self, data: bytes, metadata: dict[str, str]) -> None:
        self._data = data
        self.properties = _Props(dict(metadata))

    async def readall(self) -> bytes:
        return self._data


class _AList:
    def __init__(self, items: list[_Item]) -> None:
        self._it = iter(items)

    def __aiter__(self) -> _AList:
        return self

    async def __anext__(self) -> _Item:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


Blobs = dict[str, tuple[bytes, dict[str, str]]]


def make_containers(
    sync_base: type,
    async_base: type,
    not_found: type[BaseException],
    exists: type[BaseException] | None = None,
) -> tuple[type, type]:
    """Build in-memory sync/async containers deriving from the given client classes.

    The saver checks ``isinstance`` against ``azure.storage.blob.ContainerClient``
    (and the ``aio`` class), so the generator passes the real classes while unit
    tests pass their own stand-ins. ``exists`` is the error a create-only upload
    (``overwrite=False``) raises on an existing blob (``ResourceExistsError``); releases
    before the conditional create never pass ``overwrite=False``, so it is optional.
    """

    class _Client:
        def __init__(self, blobs: Blobs, name: str) -> None:
            self._b, self._n = blobs, name

        def upload_blob(self, data: bytes, metadata: dict[str, str], overwrite: bool) -> None:
            if not overwrite and self._n in self._b:
                if exists is None:
                    raise AssertionError("create-only upload without an 'exists' error type")
                raise exists(self._n)
            self._b[self._n] = (bytes(data), dict(metadata))

        def download_blob(self) -> _Stream:
            if self._n not in self._b:
                raise not_found(self._n)
            return _Stream(self._b[self._n][0], self._b[self._n][1])

        def get_blob_properties(self) -> _Props:
            if self._n not in self._b:
                raise not_found(self._n)
            return _Props(dict(self._b[self._n][1]))

        def delete_blob(self) -> None:
            del self._b[self._n]

    class _AClient(_Client):
        async def upload_blob(  # type: ignore[override]
            self, data: bytes, metadata: dict[str, str], overwrite: bool
        ) -> None:
            _Client.upload_blob(self, data, metadata, overwrite)

        async def download_blob(self) -> _AStream:  # type: ignore[override]
            stream = _Client.download_blob(self)
            return _AStream(stream.readall(), stream.properties.metadata)

        async def get_blob_properties(self) -> _Props:  # type: ignore[override]
            return _Client.get_blob_properties(self)

        async def delete_blob(self) -> None:  # type: ignore[override]
            _Client.delete_blob(self)

    class SyncContainer(sync_base):  # type: ignore[misc]
        def __init__(self, blobs: Blobs) -> None:
            self._blobs = blobs

        def get_blob_client(self, blob: str) -> _Client:
            return _Client(self._blobs, blob)

        def list_blobs(self, name_starts_with: str = "", **_: Any) -> list[_Item]:
            return [_Item(n) for n in sorted(self._blobs) if n.startswith(name_starts_with)]

    class AsyncContainer(async_base):  # type: ignore[misc]
        def __init__(self, blobs: Blobs) -> None:
            self._blobs = blobs

        def get_blob_client(self, blob: str) -> _AClient:
            return _AClient(self._blobs, blob)

        def list_blobs(self, name_starts_with: str = "", **_: Any) -> _AList:
            return _AList([_Item(n) for n in sorted(self._blobs) if n.startswith(name_starts_with)])

    return SyncContainer, AsyncContainer


def _cp(n: int, values: dict[str, Any], versions: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": 1,
        "id": f"1f000000-0000-6000-8000-{n:012d}",
        "ts": TS,
        "channel_values": values,
        "channel_versions": versions,
        "versions_seen": {"__input__": {}, "node": dict(versions)},
        "pending_sends": [],
    }


# (thread_id, checkpoint_ns). Special characters exercise percent-encoding.
THREADS = {
    "sync": [("thread-sync", ""), ("thread-sync", "sub|child:1"), ("user/42 \u00e9", "")],
    "async": [("thread-async", ""), ("thread-async", "sub|child:1")],
}


def _scenario(supports_task_path: bool) -> list[dict[str, Any]]:
    """Logical steps for one (thread, ns); returned as plain data."""
    steps = []
    for i in range(3):
        steps.append(
            {
                "n": i + 1,
                # ``messages`` changes on steps 1-2 only, so step 3 reuses its value blob.
                "values": {
                    "messages": [f"m{min(i, 1)}", {"role": "ai", "n": min(i, 1)}],
                    "counter": i + 1,
                },
                "changed": ["counter"] if i == 2 else ["messages", "counter"],
                "metadata": {"source": "input" if i == 0 else "loop", "step": i - 1, "parents": {}},
                "writes": [
                    ("task-b", "messages", f"w-b{i}", "~__pregel_pull, nb"),
                    ("task-a", "counter", i + 10, "~__pregel_pull, na"),
                    ("task-a", "messages", f"w-a{i}", "~__pregel_pull, na"),
                ]
                + ([("task-i", "__interrupt__", f"pause-{i}", "")] if i == 2 else []),
            }
        )
    return steps


def _writes_order(
    writes: list[tuple[str, str, Any, str]], supports_task_path: bool
) -> list[list[Any]]:
    # Replay order is (task_path, task_id, write_index); legacy writers store no path.
    indexed = []
    per_task: dict[str, int] = {}
    for task_id, channel, value, path in writes:
        idx = WRITES_IDX_MAP.get(channel)
        if idx is None:
            idx = per_task.get(task_id, 0)
            per_task[task_id] = idx + 1
        indexed.append(((path if supports_task_path else "", task_id, idx), channel, value))
    indexed.sort(key=lambda t: t[0])
    return [[t[0][1], t[1], t[2]] for t in indexed]


async def _arun_thread(
    saver: Any,
    thread: str,
    ns: str,
    supports_task_path: bool,
    use_async: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    versions: dict[str, Any] = {}
    parent_id: str | None = None
    for step in _scenario(supports_task_path):
        for ch in step["changed"]:
            versions[ch] = saver.get_next_version(versions.get(ch), None)
        # A channel with a version but no value exercises the ``empty`` blob.
        versions["branch:to:idle"] = versions.get("branch:to:idle") or saver.get_next_version(
            None, None
        )
        cp = _cp(step["n"], step["values"], dict(versions))
        new_versions = {ch: versions[ch] for ch in step["changed"]}
        if step["n"] == 1:
            new_versions["branch:to:idle"] = versions["branch:to:idle"]
        put_cfg = {
            "configurable": {
                "thread_id": thread,
                "checkpoint_ns": ns,
                **({"checkpoint_id": parent_id} if parent_id else {}),
            }
        }
        if use_async:
            new_cfg = await saver.aput(put_cfg, cp, step["metadata"], new_versions)
        else:
            new_cfg = saver.put(put_cfg, cp, step["metadata"], new_versions)
        cp_id = new_cfg["configurable"]["checkpoint_id"]
        for task_id in ("task-b", "task-a", "task-i"):
            ws = [w for w in step["writes"] if w[0] == task_id]
            if not ws:
                continue
            pairs = [(w[1], w[2]) for w in ws]
            kwargs: dict[str, Any] = {}
            if supports_task_path:
                kwargs["task_path"] = ws[0][3]
            if use_async:
                await saver.aput_writes(new_cfg, pairs, task_id, **kwargs)
            else:
                saver.put_writes(new_cfg, pairs, task_id, **kwargs)
        out.append(
            {
                "id": cp_id,
                "parent_id": parent_id,
                "channel_values": step["values"],
                "metadata": step["metadata"],
                "pending_writes": _writes_order(step["writes"], supports_task_path),
            }
        )
        parent_id = cp_id
    return out


def run_scenario(
    saver: Any, *, supports_task_path: bool, supports_async: bool
) -> list[dict[str, Any]]:
    """Drive ``saver`` through the deterministic scenario; return the expected content.

    Seed ``random`` first (``random.seed(881)``) so the saver's own channel
    version suffixes are reproducible.
    """
    expected: list[dict[str, Any]] = []
    for thread, ns in THREADS["sync"]:
        cps = asyncio.run(_arun_thread(saver, thread, ns, supports_task_path, use_async=False))
        expected.append(
            {"writer": "sync", "thread_id": thread, "checkpoint_ns": ns, "checkpoints": cps}
        )
    for thread, ns in THREADS["async"] if supports_async else []:
        cps = asyncio.run(_arun_thread(saver, thread, ns, supports_task_path, use_async=True))
        expected.append(
            {"writer": "async", "thread_id": thread, "checkpoint_ns": ns, "checkpoints": cps}
        )
    return expected


def generate(label: str, source_ref: str) -> dict[str, Any]:
    from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
    from azure.storage.blob import ContainerClient
    from azure.storage.blob.aio import ContainerClient as AioContainerClient

    from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver

    sync_cls, async_cls = make_containers(
        ContainerClient, AioContainerClient, ResourceNotFoundError, ResourceExistsError
    )
    random.seed(881)
    blobs: Blobs = {}
    # Releases before native async I/O (#440, first shipped in 0.9.0) take no aio client.
    supports_async = (
        "aio_container_client" in inspect.signature(AzureBlobCheckpointSaver).parameters
    )
    saver = (
        AzureBlobCheckpointSaver(
            container_client=sync_cls(blobs), aio_container_client=async_cls(blobs)
        )
        if supports_async
        else AzureBlobCheckpointSaver(container_client=sync_cls(blobs))
    )
    supports_task_path = "task_path" in inspect.signature(saver.put_writes).parameters
    expected = run_scenario(
        saver, supports_task_path=supports_task_path, supports_async=supports_async
    )

    def _ver(name: str) -> str:
        if name == "azure-functions-langgraph":
            # Report the code actually imported (the editable-install metadata of a
            # source tree can be stale), not the distribution metadata.
            return str(importlib.import_module("azure_functions_langgraph").__version__)
        try:
            return md.version(name)
        except md.PackageNotFoundError:
            return "not-installed"

    return {
        "blobs": {
            "label": label,
            "blobs": [
                {
                    "name": n,
                    "metadata": blobs[n][1],
                    "data_b64": base64.b64encode(blobs[n][0]).decode(),
                }
                for n in sorted(blobs)
            ],
        },
        "expected": {
            "label": label,
            "task_path_persisted": supports_task_path,
            "native_async_writer": supports_async,
            "writes": expected,
        },
        "environment": {
            "label": label,
            "source_ref": source_ref,
            "azure-functions-langgraph": _ver("azure-functions-langgraph"),
            "python": platform.python_version(),
            "langgraph": _ver("langgraph"),
            "langgraph-checkpoint": _ver("langgraph-checkpoint"),
            "azure-storage-blob": _ver("azure-storage-blob"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--source-ref",
        default="pypi",
        help="Provenance of the code under test (e.g. a PyPI wheel sha256 or a git commit).",
    )
    args = parser.parse_args()
    result = generate(args.label, args.source_ref)
    args.out.mkdir(parents=True, exist_ok=True)
    for key, name in (
        ("blobs", "blobs.json"),
        ("expected", "expected.json"),
        ("environment", "environment.json"),
    ):
        (args.out / name).write_text(json.dumps(result[key], indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
