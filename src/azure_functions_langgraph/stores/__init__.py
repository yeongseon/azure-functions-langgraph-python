"""Persistent thread stores for LangGraph Platform API."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .azure_table import AzureTableThreadStore


def __getattr__(name: str) -> object:
    if name == "AzureTableThreadStore":
        from .azure_table import AzureTableThreadStore

        return AzureTableThreadStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AzureTableThreadStore"]
