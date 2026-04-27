"""Persistent checkpoint savers for LangGraph graphs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .azure_blob import (
        AzureBlobCheckpointSaver,
        OrphanedValueCollectionResult,
    )
    from .cosmos import create_cosmos_checkpointer
    from .postgres import create_postgres_checkpointer
    from .sqlite import create_sqlite_checkpointer


def __getattr__(name: str) -> object:
    if name == "AzureBlobCheckpointSaver":
        from .azure_blob import AzureBlobCheckpointSaver

        return AzureBlobCheckpointSaver
    if name == "OrphanedValueCollectionResult":
        from .azure_blob import OrphanedValueCollectionResult

        return OrphanedValueCollectionResult
    if name == "create_postgres_checkpointer":
        from .postgres import create_postgres_checkpointer

        return create_postgres_checkpointer
    if name == "create_sqlite_checkpointer":
        from .sqlite import create_sqlite_checkpointer

        return create_sqlite_checkpointer
    if name == "create_cosmos_checkpointer":
        from .cosmos import create_cosmos_checkpointer

        return create_cosmos_checkpointer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AzureBlobCheckpointSaver",
    "OrphanedValueCollectionResult",
    "create_cosmos_checkpointer",
    "create_postgres_checkpointer",
    "create_sqlite_checkpointer",
]
