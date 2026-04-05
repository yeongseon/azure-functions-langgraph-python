"""Persistent checkpoint savers for LangGraph graphs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .azure_blob import (
        AzureBlobCheckpointSaver,
    )


def __getattr__(name: str) -> object:
    if name == "AzureBlobCheckpointSaver":
        from .azure_blob import (
            AzureBlobCheckpointSaver,
        )

        return AzureBlobCheckpointSaver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AzureBlobCheckpointSaver"]
