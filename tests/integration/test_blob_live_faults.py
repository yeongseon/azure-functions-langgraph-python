"""Opt-in Azure fault schedules; injected client faults are not service failures."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import closing
import importlib
import json
import os
import time
from typing import Any

import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.locks.azure_blob import AzureBlobLeaseThreadLock
from azure_functions_langgraph.locks.guard import OWNERSHIP_GUARD_CONFIG_KEY, ThreadLeaseLostError
from tests.integration.test_checkpoint_conformance import (
    _blob_connection_string,
)
from tests.integration.test_checkpoint_conformance import (
    azurite_container_client as azurite_container_client,
)
from tests.test_blob_checkpoint_io import _cfg, _cp, _md

pytestmark = pytest.mark.integration


@pytest.fixture
def live_container(request: pytest.FixtureRequest) -> Iterator[Any]:
    # Check before resolving the shared fixture: default CI must not contact Azure/Azurite.
    if "BLOB_TEST_CONNECTION_STRING_ENV" not in os.environ:
        pytest.skip("live Azure probes require BLOB_TEST_CONNECTION_STRING_ENV")
    yield request.getfixturevalue("azurite_container_client")


@pytest.mark.parametrize("after_marker", [False, True], ids=["before-marker", "after-marker"])
async def test_live_cancelled_native_publication_recovers(
    live_container: Any, after_marker: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same schedule/assertions as the offline cancellation test, at the real upload seam.
    # Its fake-client patch cannot be reused with SDK clients; all unpaused I/O here is real.
    aio_module = importlib.import_module("azure.storage.blob.aio")
    async with aio_module.ContainerClient.from_connection_string(
        _blob_connection_string(), live_container.container_name
    ) as aio:
        saver = AzureBlobCheckpointSaver(container_client=live_container, aio_container_client=aio)
        cp = _cp("cp-000", 2)
        await saver.aput_writes(_cfg("cp-000"), [("a", "original"), ("b", "complete")], "task")
        upload = saver._aupload_blob
        entered, release = asyncio.Event(), asyncio.Event()
        blocked = False

        async def paused(
            path: str, data: bytes, metadata: dict[str, str], overwrite: bool = True
        ) -> None:
            nonlocal blocked
            if path.endswith("checkpoint.bin") and not blocked:
                blocked = True
                if after_marker:
                    await upload(path, data, metadata, overwrite)
                entered.set()
                await release.wait()
            await upload(path, data, metadata, overwrite)

        monkeypatch.setattr(saver, "_aupload_blob", paused)
        started = time.monotonic()
        task = asyncio.create_task(saver.aput(_cfg(), cp, _md(0), cp["channel_versions"]))
        try:
            await asyncio.wait_for(entered.wait(), timeout=30)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # A fresh reader must expose only committed state, through both APIs.
        reader = AzureBlobCheckpointSaver(container_client=live_container, aio_container_client=aio)
        sync = reader.get_tuple(_cfg())
        assert await reader.aget_tuple(_cfg()) == sync
        if after_marker:
            assert sync is not None and sync.checkpoint == cp
        else:
            assert sync is None
            assert reader.get_tuple(_cfg("cp-000")) is None
            assert await reader.aget_tuple(_cfg("cp-000")) is None
        hint = live_container.get_blob_client("threads/t/ns//latest.json")
        assert json.loads(hint.download_blob().readall())["dirty"] is True
        await saver.aput(_cfg(), cp, _md(0), cp["channel_versions"])
        assert json.loads(hint.download_blob().readall())["dirty"] is False
        result = reader.get_tuple(_cfg())
        assert result is not None and result.checkpoint == cp and result.metadata == _md(0)
        assert result.pending_writes == [("task", "a", "original"), ("task", "b", "complete")]
        assert await reader.aget_tuple(_cfg()) == result
        assert [c.checkpoint["id"] for c in reader.list(_cfg())] == ["cp-000"]
        assert [c.checkpoint["id"] async for c in reader.alist(_cfg())] == ["cp-000"]
        print(
            f"cancellation after_marker={after_marker} "
            f"recovery_retry_s={time.monotonic() - started:.3f}"
        )


@pytest.mark.parametrize("native_async", [False, True], ids=["sync", "native"])
async def test_live_client_injected_accepted_marker_timeout_retry(
    live_container: Any, native_async: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given real pending slots; inject one lost response AFTER Azure accepts the marker.
    aio_module = importlib.import_module("azure.storage.blob.aio")
    async with aio_module.ContainerClient.from_connection_string(
        _blob_connection_string(), live_container.container_name
    ) as aio:
        saver = AzureBlobCheckpointSaver(container_client=live_container, aio_container_client=aio)
        saver.put_writes(_cfg("cp-000"), [("a", "original"), ("b", "complete")], "task")
        cp = _cp("cp-000", 2)
        upload, aupload = saver._upload_blob, saver._aupload_blob
        fault = TimeoutError("CLIENT-INJECTED accepted marker response lost")
        fired = False

        def fail(path: str, data: bytes, metadata: dict[str, str], overwrite: bool = True) -> None:
            nonlocal fired
            upload(path, data, metadata, overwrite)
            if path.endswith("checkpoint.bin") and not fired:
                fired = True
                raise fault

        async def afail(
            path: str, data: bytes, metadata: dict[str, str], overwrite: bool = True
        ) -> None:
            nonlocal fired
            await aupload(path, data, metadata, overwrite)
            if path.endswith("checkpoint.bin") and not fired:
                fired = True
                raise fault

        monkeypatch.setattr(saver, "_upload_blob", fail)
        monkeypatch.setattr(saver, "_aupload_blob", afail)

        async def attempt() -> None:
            if native_async:
                await saver.aput(_cfg(), cp, _md(0), cp["channel_versions"])
            else:
                saver.put(_cfg(), cp, _md(0), cp["channel_versions"])

        # When the response is lost, the original error propagates, not an implicit saver retry.
        started = time.monotonic()
        with pytest.raises(TimeoutError) as caught:
            await attempt()
        assert caught.value is fault and fired
        hint = live_container.get_blob_client("threads/t/ns//latest.json")
        assert json.loads(hint.download_blob().readall())["dirty"] is True
        reader = AzureBlobCheckpointSaver(container_client=live_container, aio_container_client=aio)
        committed = reader.get_tuple(_cfg())
        assert committed is not None and committed.checkpoint == cp
        # Then an explicit retry repairs publication and preserves the accepted slot values.
        await attempt()
        saver.put_writes(_cfg("cp-000"), [("a", "replacement"), ("b", "replacement")], "task")
        assert json.loads(hint.download_blob().readall())["dirty"] is False
        assert reader.get_tuple(_cfg()) == committed == await reader.aget_tuple(_cfg())
        assert committed.pending_writes == [("task", "a", "original"), ("task", "b", "complete")]
        print(
            f"CLIENT-INJECTED timeout native={native_async} "
            f"recovery_retry_s={time.monotonic() - started:.3f}"
        )


@pytest.mark.parametrize("guarded", [False, True], ids=["unguarded", "guarded"])
@pytest.mark.parametrize("native_async", [False, True], ids=["sync", "native"])
async def test_live_real_lease_break_stale_checkpoint_write(
    live_container: Any, guarded: bool, native_async: bool
) -> None:
    blob_module = importlib.import_module("azure.storage.blob")
    aio_module = importlib.import_module("azure.storage.blob.aio")
    # Given a real 15-second marker lease, with its normal 5-second renewal cadence.
    with (
        closing(
            AzureBlobLeaseThreadLock(
                container_client=live_container, lease_duration=15, guard_checkpoint_writes=guarded
            )
        ) as holder,
        closing(
            AzureBlobLeaseThreadLock(container_client=live_container, lease_duration=15)
        ) as successor,
    ):
        token = holder.acquire("live", "t")
        assert token is not None
        assert successor.acquire("live", "t") is None
        cfg = _cfg()
        guard = holder.ownership_guard("live", "t", token)
        if guard is not None:
            cfg["configurable"] = {
                **cfg.get("configurable", {}),
                OWNERSHIP_GUARD_CONFIG_KEY: guard,
            }
        marker = live_container.get_blob_client("thread-locks/live/t")
        assert marker.get_blob_properties().lease.state == "leased"
        started = time.monotonic()
        # When Azure REALLY breaks the lease (not a fake renew exception).
        remaining = blob_module.BlobLeaseClient(marker).break_lease(lease_break_period=0)
        broken = time.monotonic()
        assert remaining == 0
        successor_token = successor.acquire("live", "t", timeout=10)
        assert successor_token is not None
        state = holder._active_leases[("live", "t")]

        async def await_detection() -> None:
            while not state.lost:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(await_detection(), timeout=20)
        detected = time.monotonic()
        assert state.last_error is not None
        assert holder.acquire("live", "t") is None
        print(
            f"REAL lease break guarded={guarded} native={native_async} "
            f"break_s={broken - started:.3f} detection_after_break_s={detected - broken:.3f} "
            f"renew_status={getattr(state.last_error, 'status_code', None)} "
            f"renew_code={getattr(state.last_error, 'error_code', None)}"
        )
        async with aio_module.ContainerClient.from_connection_string(
            _blob_connection_string(), live_container.container_name
        ) as aio:
            saver = AzureBlobCheckpointSaver(
                container_client=live_container, aio_container_client=aio
            )

            async def stale_write() -> None:
                cp = _cp("cp-stale", 2)
                if native_async:
                    await saver.aput(cfg, cp, _md(0), cp["channel_versions"])
                else:
                    saver.put(cfg, cp, _md(0), cp["channel_versions"])

            # Then a successor owns the marker, but ONLY the opt-in guard rejects the old writer.
            if guarded:
                before = {
                    b.name: live_container.download_blob(b.name).readall()
                    for b in live_container.list_blobs(name_starts_with="threads/")
                }
                with pytest.raises(ThreadLeaseLostError) as caught:
                    await stale_write()
                assert caught.value.reason == "lost"
                assert {
                    b.name: live_container.download_blob(b.name).readall()
                    for b in live_container.list_blobs(name_starts_with="threads/")
                } == before
                assert saver.get_tuple(_cfg()) is None
            else:
                await stale_write()
                result = saver.get_tuple(_cfg())
                assert result is not None and result.checkpoint == _cp("cp-stale", 2)
                assert await saver.aget_tuple(_cfg()) == result
            holder.release("live", "t", token)
            successor.release("live", "t", successor_token)
