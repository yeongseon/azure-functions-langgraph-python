"""Persistent checkpoint savers for LangGraph graphs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .azure_blob import (
        AzureBlobCheckpointSaver,
    )
    from .postgres import create_postgres_checkpointer
    from .sqlite import create_sqlite_checkpointer


def __getattr__(name: str) -> object:
    if name == "AzureBlobCheckpointSaver":
        from .azure_blob import AzureBlobCheckpointSaver

        return AzureBlobCheckpointSaver
    if name == "create_postgres_checkpointer":
        from .postgres import create_postgres_checkpointer

        return create_postgres_checkpointer
    if name == "create_sqlite_checkpointer":
        from .sqlite import create_sqlite_checkpointer

        return create_sqlite_checkpointer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AzureBlobCheckpointSaver",
    "create_postgres_checkpointer",
    "create_sqlite_checkpointer",
]
