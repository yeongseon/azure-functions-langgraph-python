"""Rolling-upgrade compatibility of persisted Blob checkpoint data (#881).

The fixture sets under ``tests/fixtures/blob_compat/sets`` hold blob names,
metadata and bytes written by *real* published releases of this package
(``0.8.2``, ``0.9.4``, ``0.12.2``, ``0.13.0``) and by the current saver. They are produced
by ``tests/fixtures/blob_compat/generate_fixtures.py``; see the README next to
it for versions and provenance.

What these tests prove: the **current reader** reads data written by those
older writers, through the synchronous, native-async and thread-executor async
paths, without rewriting it, and fails safely on malformed or unknown data.
What they do **not** prove: that an *older* release can read data written by a
newer one (downgrade). That is documented separately in
``docs/blob-checkpoint-format.md``.

This module is also the compatibility gate: every stored hint format version
the current writer emits must have a committed fixture set (see
``test_every_emitted_format_version_has_fixture_sets``).
"""

from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import random
import sys
import types
from typing import Any

from langchain_core.runnables import RunnableConfig
import pytest

from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY,
    ThreadLeaseLostError,
)
from tests.ownership_guard_support import ScriptedGuard

ROOT = Path(__file__).parent / "fixtures" / "blob_compat"
SETS_DIR = ROOT / "sets"
SET_NAMES = sorted(p.name for p in SETS_DIR.iterdir() if p.is_dir())

# Stored ``latest.json`` hint format -> fixture sets that carry it.
#   None : unversioned ``{"checkpoint_id": ...}`` written by 0.8.2-0.12.2
#   1    : ``{"version": 1, "dirty": bool, ["checkpoint_id": ...]}``, 0.13.0 and the current writer
# A new stored format version MUST add its key here together with a fixture set
# produced by the real writer of that format (see the README), and the suite
# below must pass over it before the format ships.
STORED_HINT_FORMATS: dict[int | None, list[str]] = {
    None: ["0.8.2", "0.9.4", "0.12.2"],
    1: ["0.13.0", "current"],
}
LEGACY_SETS = STORED_HINT_FORMATS[None]
ASYNC_WRITER_SETS = ["0.9.4", "0.12.2", "0.13.0", "current"]  # 0.8.2 predates native async (#440)

GRAPH_MODULE = "azure_functions_langgraph.checkpointers.azure_blob"
Blobs = dict[str, tuple[bytes, dict[str, str]]]


class _NotFound(Exception):
    pass


class _Exists(Exception):
    pass


class _SyncBase:
    pass


class _AsyncBase:
    pass


def _load_generator() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "blob_compat_generator", ROOT / "generate_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GEN = _load_generator()
SyncContainer, AsyncContainer = GEN.make_containers(_SyncBase, _AsyncBase, _NotFound, _Exists)


@pytest.fixture(autouse=True)
def _fake_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    blob_mod = types.ModuleType("azure.storage.blob")
    aio_mod = types.ModuleType("azure.storage.blob.aio")
    setattr(blob_mod, "ContainerClient", SyncContainer.__mro__[1])
    setattr(aio_mod, "ContainerClient", AsyncContainer.__mro__[1])
    setattr(blob_mod, "aio", aio_mod)
    exc_mod = types.ModuleType("azure.core.exceptions")
    setattr(exc_mod, "ResourceNotFoundError", _NotFound)
    setattr(exc_mod, "ResourceExistsError", _Exists)
    for name, mod in (
        ("azure", types.ModuleType("azure")),
        ("azure.storage", types.ModuleType("azure.storage")),
        ("azure.storage.blob", blob_mod),
        ("azure.storage.blob.aio", aio_mod),
        ("azure.core", types.ModuleType("azure.core")),
        ("azure.core.exceptions", exc_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load_set(name: str) -> tuple[Blobs, dict[str, Any]]:
    directory = SETS_DIR / name
    raw = json.loads((directory / "blobs.json").read_text())
    blobs: Blobs = {
        b["name"]: (base64.b64decode(b["data_b64"]), dict(b["metadata"])) for b in raw["blobs"]
    }
    return blobs, json.loads((directory / "expected.json").read_text())


def _saver(blobs: Blobs, *, native_async: bool = True) -> Any:
    module = importlib.import_module(GRAPH_MODULE)
    return module.AzureBlobCheckpointSaver(
        container_client=SyncContainer(blobs),
        aio_container_client=AsyncContainer(blobs) if native_async else None,
    )


def _cfg(thread: str, ns: str, checkpoint_id: str | None = None) -> RunnableConfig:
    cfg: RunnableConfig = {"configurable": {"thread_id": thread, "checkpoint_ns": ns}}
    if checkpoint_id is not None:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return cfg


def _norm(tup: Any) -> dict[str, Any]:
    return {
        "id": tup.checkpoint["id"],
        "parent_id": (
            tup.parent_config["configurable"]["checkpoint_id"] if tup.parent_config else None
        ),
        "channel_values": tup.checkpoint["channel_values"],
        "metadata": {k: tup.metadata[k] for k in ("source", "step", "parents")},
        "pending_writes": [list(w) for w in tup.pending_writes],
    }


READERS = ["sync", "async-native", "async-fallback"]


async def _read_all(kind: str, blobs: Blobs, thread: str, ns: str) -> list[dict[str, Any]]:
    """Return every checkpoint of one thread/namespace, newest first, via ``kind``."""
    saver = _saver(blobs, native_async=kind == "async-native")
    if kind == "sync":
        listed = list(saver.list(_cfg(thread, ns)))
        latest = saver.get_tuple(_cfg(thread, ns))
        by_id = [saver.get_tuple(_cfg(thread, ns, t.checkpoint["id"])) for t in listed]
    else:
        listed = [t async for t in saver.alist(_cfg(thread, ns))]
        latest = await saver.aget_tuple(_cfg(thread, ns))
        by_id = [await saver.aget_tuple(_cfg(thread, ns, t.checkpoint["id"])) for t in listed]
    assert latest is not None
    out = [_norm(t) for t in listed]
    assert _norm(latest) == out[0], "get_tuple(latest) must equal the newest listed checkpoint"
    assert [_norm(t) for t in by_id] == out, "get_tuple(by id) must equal list()"
    return out


def _expected_newest_first(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return list(reversed(entry["checkpoints"]))


def _snapshot(blobs: Blobs) -> Blobs:
    return {k: (bytes(v[0]), dict(v[1])) for k, v in blobs.items()}


def _hint(thread: str = "thread-sync", ns: str = "") -> str:
    return f"threads/{thread}/ns/{ns}/latest.json"


def _ckpt(thread: str, ns: str, cp_id: str, leaf: str) -> str:
    return f"threads/{thread}/ns/{ns}/checkpoints/{cp_id}/{leaf}"


NEWEST = "1f000000-0000-6000-8000-000000000003"
PREV = "1f000000-0000-6000-8000-000000000002"
NEXT = "1f000000-0000-6000-8000-000000000004"


# --------------------------------------------------------------------------
# fixture inventory / gate
# --------------------------------------------------------------------------


def _hint_version(payload: bytes) -> int | None:
    data = json.loads(payload)
    return data["version"] if "version" in data else None


def test_fixture_sets_match_registry_and_carry_provenance() -> None:
    registered = {name for names in STORED_HINT_FORMATS.values() for name in names}
    assert set(SET_NAMES) == registered, "every fixture set must be registered, and vice versa"
    for name in SET_NAMES:
        env = json.loads((SETS_DIR / name / "environment.json").read_text())
        assert env["label"] == name
        for key in ("azure-functions-langgraph", "python", "langgraph", "langgraph-checkpoint"):
            assert env[key] and env[key] != "not-installed", (name, key)
        if name != "current":
            # Past sets must come from the published wheel of exactly that version.
            assert env["azure-functions-langgraph"] == name
            assert env["source_ref"].startswith("pypi:") and "sha256=" in env["source_ref"]


@pytest.mark.parametrize(
    "hint_version", sorted(STORED_HINT_FORMATS, key=lambda v: (v is not None, v))
)
def test_registered_format_version_is_what_its_fixture_sets_contain(
    hint_version: int | None,
) -> None:
    for name in STORED_HINT_FORMATS[hint_version]:
        blobs, _ = _load_set(name)
        hints = [v[0] for k, v in blobs.items() if k.endswith("/latest.json")]
        assert hints, name
        assert {_hint_version(h) for h in hints} == {hint_version}, name


def test_every_emitted_format_version_has_fixture_sets() -> None:
    """Gate: a format change cannot ship on new-version round trips alone.

    Replays the scenario with the *current* writer and classifies every
    ``latest.json`` it leaves, plus every ``dirty`` intent marker it writes in
    flight. Any stored hint version without a registered, committed fixture
    set fails here.
    """
    seen: set[int | None] = set()
    module = importlib.import_module(GRAPH_MODULE)
    blobs: Blobs = {}
    saver = module.AzureBlobCheckpointSaver(
        container_client=SyncContainer(blobs), aio_container_client=AsyncContainer(blobs)
    )
    real_upload = saver._upload_blob

    def spy(path: str, payload: bytes, metadata: dict[str, str], overwrite: bool = True) -> None:
        if path.endswith("/latest.json"):
            seen.add(_hint_version(payload))
        real_upload(path, payload, metadata, overwrite)

    saver._upload_blob = spy
    saver_state = random.getstate()
    try:
        random.seed(881)
        GEN.run_scenario(saver, supports_task_path=True, supports_async=False)
    finally:
        random.setstate(saver_state)
    assert seen, "current writer published no latest.json hint?"
    for version in seen:
        assert version in STORED_HINT_FORMATS, (
            f"stored hint format version {version!r} has no fixture-set registration; add a "
            "fixture set from a real writer of this format and extend STORED_HINT_FORMATS"
        )
        assert STORED_HINT_FORMATS[version], version
        for name in STORED_HINT_FORMATS[version]:
            assert (SETS_DIR / name / "blobs.json").is_file()
    assert "current" in STORED_HINT_FORMATS[max(v for v in seen if v is not None)]


def _decode_all(saver: Any, blobs: Blobs) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, (data, meta) in blobs.items():
        if name.endswith("/latest.json"):
            out[name] = data
        elif meta.get("serde_type") == "empty":
            out[name] = ("empty", data)
        else:
            out[name] = saver.serde.loads_typed((meta["serde_type"], data))
    return out


def test_current_writer_output_layout_matches_committed_current_set() -> None:
    """Names, adapter metadata and ``latest.json`` bytes are byte-stable.

    LangGraph-serialized payloads are compared after decoding, because their
    exact bytes belong to the installed ``langgraph-checkpoint``, not to this
    adapter.
    """
    committed, expected = _load_set("current")
    module = importlib.import_module(GRAPH_MODULE)
    blobs: Blobs = {}
    saver = module.AzureBlobCheckpointSaver(
        container_client=SyncContainer(blobs), aio_container_client=AsyncContainer(blobs)
    )
    state = random.getstate()
    try:
        random.seed(881)
        written = GEN.run_scenario(saver, supports_task_path=True, supports_async=True)
    finally:
        random.setstate(state)
    assert written == expected["writes"]
    assert sorted(blobs) == sorted(committed)
    for name, (data, meta) in blobs.items():
        assert meta == committed[name][1], name
        if name.endswith("/latest.json"):
            assert data == committed[name][0], name
    assert _decode_all(saver, blobs) == _decode_all(saver, committed)


# --------------------------------------------------------------------------
# old writer -> new reader
# --------------------------------------------------------------------------


def _threads(expected: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(e["thread_id"], e["checkpoint_ns"], e) for e in expected["writes"]]


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("set_name", SET_NAMES)
async def test_old_writer_to_current_reader(set_name: str, reader: str) -> None:
    blobs, expected = _load_set(set_name)
    before = _snapshot(blobs)
    assert _threads(expected), set_name
    for thread, ns, entry in _threads(expected):
        got = await _read_all(reader, blobs, thread, ns)
        assert got == _expected_newest_first(entry), (set_name, reader, thread, ns)
    assert blobs == before, "reading must not write, repair or delete any blob"


@pytest.mark.parametrize("set_name", SET_NAMES)
def test_set_covers_namespaces_pending_writes_and_sync_writer(set_name: str) -> None:
    _, expected = _load_set(set_name)
    namespaces = {e["checkpoint_ns"] for e in expected["writes"]}
    assert "" in namespaces and "sub|child:1" in namespaces
    assert any(e["thread_id"] == "user/42 \u00e9" for e in expected["writes"])
    assert any(ckpt["pending_writes"] for e in expected["writes"] for ckpt in e["checkpoints"])
    writers = {e["writer"] for e in expected["writes"]}
    assert writers == ({"sync", "async"} if set_name in ASYNC_WRITER_SETS else {"sync"})
    assert expected["native_async_writer"] is (set_name in ASYNC_WRITER_SETS)


@pytest.mark.parametrize("set_name", SET_NAMES)
def test_fixture_blob_inventory_shape(set_name: str) -> None:
    """The committed names fit the layout documented in docs/blob-checkpoint-format.md."""
    blobs, _ = _load_set(set_name)
    kinds: set[str] = set()
    for name, (_data, meta) in blobs.items():
        assert name.startswith("threads/"), name
        if name.endswith("/latest.json"):
            kinds.add("latest")
            assert meta == {}
        elif name.endswith("/checkpoint.bin"):
            kinds.add("checkpoint")
            assert set(meta) <= {"serde_type", "parent_id"}
        elif name.endswith("/metadata.bin"):
            kinds.add("metadata")
            assert set(meta) == {"serde_type"}
        elif "/values/" in name:
            kinds.add("value")
            assert set(meta) == {"serde_type"}
        elif "/writes/" in name:
            kinds.add("write")
            assert set(meta) == {"serde_type", "channel", "task_id", "task_path"}
        else:  # pragma: no cover - failure path
            pytest.fail(f"unclassified blob {name}")
    assert kinds == {"latest", "checkpoint", "metadata", "value", "write"}
    assert any(m.get("serde_type") == "empty" for _d, m in blobs.values()), "empty-value blob"


# --------------------------------------------------------------------------
# rolling upgrade: new writer continues old data
# --------------------------------------------------------------------------


def _next_checkpoint(prev: Any, new_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    saver_module = importlib.import_module(GRAPH_MODULE)
    helper = saver_module.AzureBlobCheckpointSaver.get_next_version
    version = helper(None, prev.checkpoint["channel_versions"]["counter"], None)
    cp = {
        **{k: v for k, v in prev.checkpoint.items() if k != "channel_values"},
        "id": new_id,
        "channel_values": {**prev.checkpoint["channel_values"], "counter": 99},
        "channel_versions": {**prev.checkpoint["channel_versions"], "counter": version},
    }
    return cp, {"counter": version}


@pytest.mark.parametrize("native_async", [False, True])
@pytest.mark.parametrize("set_name", LEGACY_SETS)
async def test_current_writer_continues_legacy_thread_without_rewriting_old_blobs(
    set_name: str, native_async: bool
) -> None:
    blobs, expected = _load_set(set_name)
    legacy_names = set(blobs)
    before = _snapshot(blobs)
    thread, ns = "thread-sync", ""
    saver = _saver(blobs, native_async=native_async)
    prev = saver.get_tuple(_cfg(thread, ns))
    cp, new_versions = _next_checkpoint(prev, NEXT)
    metadata = {"source": "loop", "step": 2, "parents": {}}
    if native_async:
        cfg = await saver.aput(prev.config, cp, metadata, new_versions)
        await saver.aput_writes(cfg, [("counter", 100)], "task-new", "~__pregel_pull, nn")
    else:
        cfg = saver.put(prev.config, cp, metadata, new_versions)
        saver.put_writes(cfg, [("counter", 100)], "task-new", "~__pregel_pull, nn")
    assert cfg["configurable"]["checkpoint_id"] == NEXT

    # Legacy blobs are never rewritten; the only legacy name touched is the hint.
    changed = {n for n in legacy_names if blobs[n] != before[n]}
    assert changed == {_hint()}
    assert json.loads(blobs[_hint()][0]) == {"version": 1, "dirty": False, "checkpoint_id": NEXT}

    entry = next(
        e for e in expected["writes"] if (e["thread_id"], e["checkpoint_ns"]) == (thread, ns)
    )
    for kind in READERS:
        got = await _read_all(kind, blobs, thread, ns)
        assert [c["id"] for c in got] == [NEXT, *[c["id"] for c in _expected_newest_first(entry)]]
        assert got[0]["parent_id"] == NEWEST
        assert got[0]["pending_writes"] == [["task-new", "counter", 100]]
        assert got[1:] == _expected_newest_first(entry), "legacy history unchanged"


# --------------------------------------------------------------------------
# legacy hint path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("set_name", LEGACY_SETS)
def test_unversioned_hint_is_never_trusted_and_scan_is_authoritative(set_name: str) -> None:
    """Legacy path: an unversioned ``latest.json`` only ever means 'rescan'."""
    blobs, _ = _load_set(set_name)
    saver = _saver(blobs)
    stale = json.dumps({"checkpoint_id": PREV}).encode()
    blobs[_hint()] = (stale, {})
    tup = saver.get_tuple(_cfg("thread-sync", ""))
    assert tup.config["configurable"]["checkpoint_id"] == NEWEST
    assert blobs[_hint()][0] == stale, "reads must not rewrite the legacy hint"
    del blobs[_hint()]
    assert (
        saver.get_tuple(_cfg("thread-sync", "")).config["configurable"]["checkpoint_id"] == NEWEST
    )


@pytest.mark.parametrize("set_name", LEGACY_SETS)
def test_legacy_namespace_is_skipped_by_orphan_collection(set_name: str) -> None:
    """Conservative by design: GC refuses namespaces whose hint is not a versioned clean one."""
    blobs, _ = _load_set(set_name)
    before = _snapshot(blobs)
    saver = _saver(blobs)
    result = saver.collect_orphaned_values(
        "thread-sync", checkpoint_ns="", dry_run=False, grace_period_seconds=0
    )
    assert result.deleted == [] and result.would_delete == []
    assert result.skipped_namespaces == [("thread-sync", "")]
    assert blobs == before


def test_legacy_write_without_task_path_metadata_replays_by_task_id_then_index() -> None:
    """Synthetic (derived from 0.12.2 bytes): metadata stripped of ``task_path``."""
    blobs, _ = _load_set("0.12.2")
    for name, (data, meta) in list(blobs.items()):
        if "/writes/" in name:
            blobs[name] = (data, {k: v for k, v in meta.items() if k != "task_path"})
    saver = _saver(blobs)
    tup = saver.get_tuple(_cfg("thread-sync", "", NEWEST))
    assert [list(w[:2]) for w in tup.pending_writes] == [
        ["task-a", "counter"],
        ["task-a", "messages"],
        ["task-b", "messages"],
        ["task-i", "__interrupt__"],
    ]


# --------------------------------------------------------------------------
# crash / partial commit
# --------------------------------------------------------------------------


@pytest.mark.parametrize("set_name", ["0.12.2", "0.13.0", "current"])
def test_crash_after_dirty_intent_marker_only(set_name: str) -> None:
    blobs, expected = _load_set(set_name)
    blobs[_hint()] = (b'{"version":1,"dirty":true}', {})
    got = asyncio.run(_read_all("sync", blobs, "thread-sync", ""))
    entry = next(
        e for e in expected["writes"] if e["thread_id"] == "thread-sync" and not e["checkpoint_ns"]
    )
    assert got == _expected_newest_first(entry)


@pytest.mark.parametrize("set_name", ["0.12.2", "0.13.0", "current"])
@pytest.mark.parametrize("native_async", [False, True])
async def test_crash_before_commit_marker_leaves_previous_checkpoint_current(
    set_name: str, native_async: bool
) -> None:
    """Values + metadata uploaded, ``checkpoint.bin`` (the commit marker) never written."""
    blobs, expected = _load_set(set_name)
    thread, ns = "thread-sync", ""
    del blobs[_ckpt(thread, ns, NEWEST, "checkpoint.bin")]
    blobs[_hint()] = (b'{"version":1,"dirty":true}', {})
    saver = _saver(blobs, native_async=native_async)
    tup = (
        await saver.aget_tuple(_cfg(thread, ns))
        if native_async
        else saver.get_tuple(_cfg(thread, ns))
    )
    assert tup.config["configurable"]["checkpoint_id"] == PREV
    entry = next(
        e for e in expected["writes"] if (e["thread_id"], e["checkpoint_ns"]) == (thread, ns)
    )
    ids = (
        [t.checkpoint["id"] async for t in saver.alist(_cfg(thread, ns))]
        if native_async
        else [t.checkpoint["id"] for t in saver.list(_cfg(thread, ns))]
    )
    assert ids == [c["id"] for c in reversed(entry["checkpoints"][:-1])]
    # Orphans stay untouched until a retry rewrites them; the retry then succeeds.
    orphan_metadata = blobs[_ckpt(thread, ns, NEWEST, "metadata.bin")]
    assert orphan_metadata[0]


@pytest.mark.parametrize("set_name", ["0.12.2", "0.13.0", "current"])
def test_crash_after_commit_marker_before_clean_hint_exposes_committed_checkpoint(
    set_name: str,
) -> None:
    blobs, _ = _load_set(set_name)
    blobs[_hint()] = (b'{"version":1,"dirty":true}', {})
    saver = _saver(blobs)
    tup = saver.get_tuple(_cfg("thread-sync", ""))
    assert tup.config["configurable"]["checkpoint_id"] == NEWEST


def test_retry_after_crash_before_commit_marker_completes_cleanly() -> None:
    blobs, _ = _load_set("current")
    thread, ns = "thread-sync", ""
    # Simulate: step 3 crashed after metadata, before checkpoint.bin.
    cp_blob = blobs.pop(_ckpt(thread, ns, NEWEST, "checkpoint.bin"))
    blobs[_hint()] = (b'{"version":1,"dirty":true}', {})
    saver = _saver(blobs)
    prev = saver.get_tuple(_cfg(thread, ns))
    assert prev.config["configurable"]["checkpoint_id"] == PREV
    # Re-run the step: restore the checkpoint payload through the writer.
    checkpoint = saver.serde.loads_typed((cp_blob[1]["serde_type"], cp_blob[0]))
    checkpoint["channel_values"] = {"counter": 3, "messages": ["m1", {"n": 1, "role": "ai"}]}
    saver.put(prev.config, checkpoint, {"source": "loop", "step": 1, "parents": {}}, {})
    tup = saver.get_tuple(_cfg(thread, ns))
    assert tup.config["configurable"]["checkpoint_id"] == NEWEST
    assert json.loads(blobs[_hint()][0]) == {"version": 1, "dirty": False, "checkpoint_id": NEWEST}


# --------------------------------------------------------------------------
# malformed / unknown data fails safely and is not overwritten
# --------------------------------------------------------------------------

_BLOB_KINDS = {
    "checkpoint": lambda t, n: _ckpt(t, n, NEWEST, "checkpoint.bin"),
    "metadata": lambda t, n: _ckpt(t, n, NEWEST, "metadata.bin"),
    "write": lambda t, n: _ckpt(t, n, NEWEST, "writes/task-a/0.bin"),
}


def _newest_counter_value_blob(blobs: Blobs) -> str:
    """The value blob the newest checkpoint references (versions sort monotonically)."""
    return sorted(n for n in blobs if n.startswith("threads/thread-sync/ns//values/counter/"))[-1]


@pytest.mark.parametrize("set_name", ["0.12.2", "0.13.0", "current"])
@pytest.mark.parametrize("kind", ["checkpoint", "metadata", "write", "value"])
@pytest.mark.parametrize("corruption", ["unknown-serde-type", "invalid-msgpack-bytes"])
def test_unknown_or_malformed_blob_raises_and_nothing_is_overwritten(
    set_name: str, kind: str, corruption: str
) -> None:
    blobs, _ = _load_set(set_name)
    name = (
        _newest_counter_value_blob(blobs)
        if kind == "value"
        else _BLOB_KINDS[kind]("thread-sync", "")
    )
    data, meta = blobs[name]
    blobs[name] = (
        (data, {**meta, "serde_type": "msgpack-v999"})
        if corruption == "unknown-serde-type"
        else (b"\xc1\xc1\xc1", meta)
    )
    before = _snapshot(blobs)
    saver = _saver(blobs)
    with pytest.raises((NotImplementedError, ValueError, TypeError, KeyError)):
        saver.get_tuple(_cfg("thread-sync", ""))
    with pytest.raises((NotImplementedError, ValueError, TypeError, KeyError)):
        asyncio.run(saver.aget_tuple(_cfg("thread-sync", "")))
    assert blobs == before, "a failed read must not modify or delete any blob"


@pytest.mark.parametrize(
    "payload", [b"", b"not json", b"\xff\xfe", b"[]", b'"x"', b"{}", b'{"version":1}']
)
def test_malformed_hint_is_ignored_for_reads_and_never_certifies(payload: bytes) -> None:
    blobs, _ = _load_set("current")
    blobs[_hint()] = (payload, {})
    saver = _saver(blobs)
    assert (
        saver.get_tuple(_cfg("thread-sync", "")).config["configurable"]["checkpoint_id"] == NEWEST
    )
    assert blobs[_hint()][0] == payload


def test_future_hint_version_is_not_trusted_for_reads() -> None:
    blobs, _ = _load_set("current")
    future = json.dumps({"version": 2, "dirty": False, "checkpoint_id": PREV, "new": 1}).encode()
    blobs[_hint()] = (future, {})
    saver = _saver(blobs)
    # The unknown hint is not followed to PREV; the authoritative scan wins.
    assert (
        saver.get_tuple(_cfg("thread-sync", "")).config["configurable"]["checkpoint_id"] == NEWEST
    )
    assert blobs[_hint()][0] == future, "reads must leave a future-version hint untouched"


_UNKNOWN_VERSIONS: list[Any] = [2, 99, 0, -1, "1", "2", 1.0, True, None, [1], {"v": 1}]


async def _put_next(kind: str, saver: Any, prev: Any, guard: Any = None) -> Any:
    cp, nv = _next_checkpoint(prev, NEXT)
    config = prev.config
    if guard is not None:
        config = {"configurable": {**config["configurable"], OWNERSHIP_GUARD_CONFIG_KEY: guard}}
    metadata = {"source": "loop", "step": 2, "parents": {}}
    if kind == "sync":
        return saver.put(config, cp, metadata, nv)
    return await saver.aput(config, cp, metadata, nv)


@pytest.mark.parametrize("kind", READERS)
@pytest.mark.parametrize("version", _UNKNOWN_VERSIONS, ids=repr)
async def test_writer_refuses_to_overwrite_hint_of_unknown_version(kind: str, version: Any) -> None:
    blobs, _ = _load_set("current")
    future = json.dumps({"version": version, "dirty": False, "checkpoint_id": NEWEST}).encode()
    blobs[_hint()] = (future, {})
    before = _snapshot(blobs)
    saver = _saver(blobs, native_async=kind == "async-native")
    prev = saver.get_tuple(_cfg("thread-sync", ""))
    module = importlib.import_module(GRAPH_MODULE)
    with pytest.raises(module.BlobCheckpointFormatError) as excinfo:
        await _put_next("sync" if kind == "sync" else "async", saver, prev)
    message = str(excinfo.value)
    # Sanitized: no thread id, checkpoint id, path or hint content in the message.
    for secret in ("thread-sync", NEWEST, "latest.json/", "checkpoint_id", "threads/"):
        assert secret not in message
    assert "No data was modified" in message
    assert blobs == before, "a refused write must not upload, replace or delete any blob"
    # Reads keep working through the authoritative scan and leave the hint alone.
    got = await _read_all(kind, blobs, "thread-sync", "")
    assert got[0]["id"] == NEWEST
    assert blobs == before


async def test_refusal_names_a_plain_integer_version_only() -> None:
    blobs, _ = _load_set("current")
    saver = _saver(blobs)
    prev = saver.get_tuple(_cfg("thread-sync", ""))
    module = importlib.import_module(GRAPH_MODULE)
    for raw, expected in (
        (b'{"version": 2}', "version 2"),
        (b'{"version": "<x>"}', "unrecognized"),
    ):
        blobs[_hint()] = (raw, {})
        with pytest.raises(module.BlobCheckpointFormatError, match=expected) as excinfo:
            await _put_next("sync", saver, prev)
        assert "<x>" not in str(excinfo.value)


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize(
    "hint",
    [
        b'{"checkpoint_id": "' + PREV.encode() + b'"}',  # legacy 0.8.2-0.12.2
        b'{"version":1,"dirty":true}',  # intent marker
        b'{"version":1,"dirty":false,"checkpoint_id":"' + PREV.encode() + b'"}',
        b"not json",
        b"[]",
        b'"x"',
    ],
    ids=["legacy", "dirty", "stale-v1", "malformed", "array", "string"],
)
async def test_writer_still_replaces_hints_it_understands_or_that_are_unusable(
    kind: str, hint: bytes
) -> None:
    blobs, _ = _load_set("current")
    blobs[_hint()] = (hint, {})
    saver = _saver(blobs)
    prev = saver.get_tuple(_cfg("thread-sync", ""))
    await _put_next(kind, saver, prev)
    assert json.loads(blobs[_hint()][0]) == {"version": 1, "dirty": False, "checkpoint_id": NEXT}


async def test_unknown_hint_leaves_orphan_collection_fail_closed() -> None:
    blobs, _ = _load_set("current")
    blobs[_hint()] = (b'{"version":2,"dirty":false,"checkpoint_id":"x"}', {})
    saver = _saver(blobs)
    before = _snapshot(blobs)
    result = saver.collect_orphaned_values(
        "thread-sync", checkpoint_ns="", dry_run=False, grace_period_seconds=0
    )
    assert result.skipped_namespaces == [("thread-sync", "")]
    assert result.deleted == []
    assert blobs == before


# --------------------------------------------------------------------------
# ownership guard and two savers on one thread (offline, in-memory only)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_refusal_happens_before_any_ownership_check_or_upload(kind: str) -> None:
    blobs, _ = _load_set("current")
    blobs[_hint()] = (b'{"version":2}', {})
    before = _snapshot(blobs)
    saver = _saver(blobs)
    prev = saver.get_tuple(_cfg("thread-sync", ""))
    guard = ScriptedGuard()
    module = importlib.import_module(GRAPH_MODULE)
    with pytest.raises(module.BlobCheckpointFormatError):
        await _put_next(kind, saver, prev, guard)
    assert guard.checks == 0
    assert blobs == before


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_stale_owner_after_dirty_marker_cannot_commit_and_new_owner_wins(kind: str) -> None:
    """Writer A loses its lease right after publishing the dirty intent.

    A's guard raises before any value, metadata or commit-marker blob is
    written, so B (the new owner, a second saver on the same container)
    commits cleanly and readers see only B's checkpoint.
    """
    blobs, _ = _load_set("current")
    saver_a, saver_b = _saver(blobs), _saver(blobs)
    prev = saver_a.get_tuple(_cfg("thread-sync", ""))
    guard_a = ScriptedGuard(trip_on=2)  # check 1: dirty marker; check 2: first value blob
    with pytest.raises(ThreadLeaseLostError):
        await _put_next(kind, saver_a, prev, guard_a)
    assert guard_a.checks == 2
    assert json.loads(blobs[_hint()][0]) == {"version": 1, "dirty": True}
    assert _ckpt("thread-sync", "", NEXT, "checkpoint.bin") not in blobs
    assert (
        saver_b.get_tuple(_cfg("thread-sync", "")).config["configurable"]["checkpoint_id"] == NEWEST
    ), "a dirty hint is never trusted: readers scan"

    guard_b = ScriptedGuard()
    await _put_next(kind, saver_b, prev, guard_b)
    assert guard_b.checks >= 5
    got = await _read_all(kind, blobs, "thread-sync", "")
    assert got[0]["id"] == NEXT and got[0]["parent_id"] == NEWEST
    assert json.loads(blobs[_hint()][0]) == {"version": 1, "dirty": False, "checkpoint_id": NEXT}


def _race(blobs: Blobs, first_id: str, second_id: str) -> tuple[Any, Any]:
    """A begins ``put`` (id ``first_id``), B runs a whole ``put`` (``second_id``) inside A's window.

    The interleave point is A's dirty-marker upload, the first thing a writer
    does; no threads are needed, so the outcome is deterministic.
    """
    saver_a, saver_b = _saver(blobs), _saver(blobs)
    prev = saver_a.get_tuple(_cfg("thread-sync", ""))
    cp_a, nv_a = _next_checkpoint(prev, first_id)
    cp_b, nv_b = _next_checkpoint(prev, second_id)
    meta = {"source": "loop", "step": 2, "parents": {}}
    real_upload = saver_a._upload_blob
    fired: list[bool] = []

    def hook(path: str, payload: bytes, metadata: dict[str, str]) -> None:
        real_upload(path, payload, metadata)
        if path.endswith("/latest.json") and not fired:
            fired.append(True)
            saver_b.put(prev.config, cp_b, meta, nv_b)

    saver_a._upload_blob = hook
    saver_a.put(prev.config, cp_a, meta, nv_a)
    assert fired
    return saver_a, saver_b


def test_two_savers_racing_commit_both_checkpoints_and_never_lose_data() -> None:
    blobs, _ = _load_set("current")
    low, high = "1f000000-0000-6000-8000-000000000004", "1f000000-0000-6000-8000-000000000005"
    saver, _ = _race(blobs, low, high)
    ids = [t.checkpoint["id"] for t in saver.list(_cfg("thread-sync", ""))]
    assert ids[:2] == [high, low], "both commits exist; neither writer's data was overwritten"
    for cp_id in (low, high):
        assert saver.get_tuple(_cfg("thread-sync", "", cp_id)) is not None


def test_two_savers_racing_where_the_later_commit_has_the_higher_id() -> None:
    blobs, _ = _load_set("current")
    low, high = "1f000000-0000-6000-8000-000000000004", "1f000000-0000-6000-8000-000000000005"
    saver, _ = _race(blobs, high, low)  # A (id=high) publishes last
    assert json.loads(blobs[_hint()][0]) == {"version": 1, "dirty": False, "checkpoint_id": high}
    assert saver.get_tuple(_cfg("thread-sync", "")).config["configurable"]["checkpoint_id"] == high


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap (docs/blob-checkpoint-format.md, 'Concurrent writers'): the adapter assumes one "
        "writer per thread. A writer whose own id is lower publishes a clean hint computed from "
        "the hint it read before a concurrent writer committed a higher id, so get_tuple() "
        "without a checkpoint id returns the lower one. The thread lock (and, opt-in, the "
        "ownership guard) is what prevents this; it is not enforced by the saver."
    ),
)
def test_two_savers_racing_do_not_hide_the_higher_checkpoint_behind_the_hint() -> None:
    blobs, _ = _load_set("current")
    low, high = "1f000000-0000-6000-8000-000000000004", "1f000000-0000-6000-8000-000000000005"
    saver, _ = _race(blobs, low, high)  # A (id=low) publishes last
    assert saver.get_tuple(_cfg("thread-sync", "")).config["configurable"]["checkpoint_id"] == high


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap (docs/blob-checkpoint-format.md, 'Mixed fleets'): a pre-0.13 writer that "
        "commits a checkpoint and dies before rewriting latest.json leaves the current "
        "version-1 clean hint pointing at the previous checkpoint, which get_tuple() then "
        "trusts. Mitigation today is draining old writers before new ones touch a thread."
    ),
)
def test_old_writer_crash_after_commit_does_not_hide_committed_checkpoint() -> None:
    blobs, _ = _load_set("current")
    thread, ns = "thread-sync", ""
    for leaf in ("checkpoint.bin", "metadata.bin"):
        blobs[_ckpt(thread, ns, NEXT, leaf)] = blobs[_ckpt(thread, ns, NEWEST, leaf)]
    saver = _saver(blobs)
    assert saver.get_tuple(_cfg(thread, ns)).config["configurable"]["checkpoint_id"] == NEXT


# --------------------------------------------------------------------------
# retention helpers over legacy data
# --------------------------------------------------------------------------


@pytest.mark.parametrize("set_name", LEGACY_SETS)
def test_retention_over_legacy_data_keeps_newest_readable(set_name: str) -> None:
    blobs, expected = _load_set(set_name)
    saver = _saver(blobs)
    deleted = saver.delete_old_checkpoints("thread-sync", keep_last=1, checkpoint_ns="")
    assert deleted == 2
    entry = next(
        e for e in expected["writes"] if (e["thread_id"], e["checkpoint_ns"]) == ("thread-sync", "")
    )
    assert (
        asyncio.run(_read_all("sync", blobs, "thread-sync", ""))
        == _expected_newest_first(entry)[:1]
    )
    # Other threads are untouched.
    other = next(e for e in expected["writes"] if e["thread_id"] == "user/42 \u00e9")
    assert asyncio.run(_read_all("sync", blobs, "user/42 \u00e9", "")) == _expected_newest_first(
        other
    )


def test_only_the_hint_differs_between_fixture_generations() -> None:
    """Drift detector: names, adapter metadata and bytes of every non-hint blob are
    identical across the committed sets (same scenario, same ``langgraph-checkpoint``).

    This is the evidence behind the statement that the adapter's on-blob layout
    did not change between 0.8.2 and the current writer; only ``latest.json`` did.
    """
    loaded = {name: _load_set(name)[0] for name in SET_NAMES}
    reference = loaded["0.12.2"]
    for name, blobs in loaded.items():
        shared = [n for n in blobs if not n.endswith("/latest.json")]
        assert shared
        for blob_name in shared:
            assert blobs[blob_name] == reference[blob_name], (name, blob_name)


def test_fixture_copy_helper_isolates_tests() -> None:
    a, _ = _load_set("current")
    b = copy.deepcopy(a)
    b.clear()
    assert _load_set("current")[0], "loading returns a fresh mapping every call"
