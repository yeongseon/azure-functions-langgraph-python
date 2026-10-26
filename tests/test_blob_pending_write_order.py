"""Pending-write replay follows task paths, including legacy Blob metadata."""

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
import pytest

from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from tests.test_checkpointers_azure_blob import (
    MockContainerClient,
    _install_fake_azure_modules,
    async_saver_and_container,
)

__all__ = ["_install_fake_azure_modules", "async_saver_and_container"]


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
async def test_get_tuple_pending_writes_in_writes_sort_key_order(
    async_mode: bool,
    legacy: bool,
    async_saver_and_container: tuple[AzureBlobCheckpointSaver, MockContainerClient],
) -> None:
    # Given task paths whose order deliberately disagrees with task IDs.
    saver, container = async_saver_and_container
    config: RunnableConfig = {"configurable": {"thread_id": "ordered"}}
    config = saver.put(config, empty_checkpoint(), {"source": "input", "step": 0}, {})
    saver.put_writes(config, [("output", "late")], "a", "z/path")
    saver.put_writes(config, [("output", "early-0"), ("output", "early-1")], "z", "a/path")
    if legacy:
        for record in container.blobs.values():
            record.metadata.pop("task_path", None)
    # When either access path loads the checkpoint with its pending writes.
    result = await saver.aget_tuple(config) if async_mode else saver.get_tuple(config)
    # Then task path precedes task ID/index; missing legacy paths use the empty path.
    assert result is not None
    early = [("z", "output", "early-0"), ("z", "output", "early-1")]
    late = [("a", "output", "late")]
    assert result.pending_writes == (late + early if legacy else early + late)
