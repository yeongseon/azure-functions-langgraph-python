"""Pluggable per-thread lock backends for native graph endpoints.

The native ``invoke`` / ``stream`` endpoints on :class:`LangGraphApp` guard
concurrent access to the same ``(graph_name, thread_id)`` so that single-writer
checkpointers (for example the Azure Blob checkpoint saver in
:mod:`azure_functions_langgraph.checkpointers.azure_blob`)
never see racing writes for one thread.

The default backend, :class:`InProcessThreadLock`, uses :class:`threading.Lock`
and is correct **only within a single Python worker process**. Azure Functions
Consumption and Elastic Premium plans scale horizontally: two Function App
instances processing requests for the same ``thread_id`` will silently race
under the in-process default. Multi-instance deployments must supply a
distributed backend such as :class:`AzureBlobLeaseThreadLock` (Blob lease
CAS).

See :doc:`/production-guide` for the full scale-out matrix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from azure_functions_langgraph.locks.base import ThreadLock
from azure_functions_langgraph.locks.inprocess import InProcessThreadLock

if TYPE_CHECKING:
    from azure_functions_langgraph.locks.azure_blob import AzureBlobLeaseThreadLock


def __getattr__(name: str) -> object:
    if name == "AzureBlobLeaseThreadLock":
        from azure_functions_langgraph.locks.azure_blob import AzureBlobLeaseThreadLock

        return AzureBlobLeaseThreadLock
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AzureBlobLeaseThreadLock",
    "InProcessThreadLock",
    "ThreadLock",
]
