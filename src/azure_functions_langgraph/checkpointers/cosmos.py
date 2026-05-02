"""Cosmos DB checkpointer DX helper.

.. versionadded:: 0.7.0

Thin wrapper around the upstream :pypi:`langgraph-checkpoint-cosmos`
package that resolves credentials and builds a :class:`CosmosDBSaver`
suitable for Azure Functions cold-start (module-level instantiation).

The upstream saver is designed as a **context manager**.  This helper
calls ``__enter__`` so the returned object is ready to use immediately
and can be passed straight to ``builder.compile(checkpointer=...)``.

This module deliberately does **not** reimplement Cosmos DB checkpoint
storage.  It centralizes the credential convention, handles the
context-manager lifecycle, and emits a clear ImportError pointing at
the right extra when the upstream package is missing.

.. note::

   The ``cosmos`` extra requires **Python 3.11+**.  The base package
   continues to support Python 3.10.
"""

from __future__ import annotations

import importlib
import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langgraph_checkpoint_cosmos import CosmosDBSaver

logger = logging.getLogger(__name__)

_EXTRA_HINT = (
    "Cosmos DB checkpointer requires the 'cosmos' extra (Python 3.11+): "
    "pip install azure-functions-langgraph[cosmos]"
)


def create_cosmos_checkpointer(
    *,
    endpoint: str,
    database_name: str,
    container_name: str,
    credential: object | None = None,
) -> CosmosDBSaver:
    """Create a long-lived :class:`CosmosDBSaver` from connection info.

    Resolves credentials (``None`` → :class:`DefaultAzureCredential`)
    and calls :meth:`CosmosDBSaver.from_conn_info` to build the saver.
    The context manager is entered immediately so the returned object is
    ready for use at Azure Functions cold-start.

    Args:
        endpoint: Cosmos DB account endpoint
            (e.g. ``https://<account>.documents.azure.com:443/``).
        database_name: Cosmos DB database name.
        container_name: Cosmos DB container name.  The container **must**
            be created with partition key path ``/partition_key``.
        credential: A credential object accepted by the Azure Cosmos SDK,
            or ``None`` (the default) to create a
            :class:`~azure.identity.DefaultAzureCredential`.

    Returns:
        A :class:`CosmosDBSaver` ready to be passed to
        ``builder.compile(checkpointer=...)``.

    Raises:
        RuntimeError: If running on Python < 3.11.
        ImportError: If ``langgraph-checkpoint-cosmos`` or
            ``azure-identity`` (when ``credential=None``) is not
            installed.  Install via the ``cosmos`` extra.
    """
    if sys.version_info < (3, 11):
        raise RuntimeError(
            "Cosmos DB checkpointer requires Python 3.11+ "
            "because langgraph-checkpoint-cosmos does not support Python 3.10."
        )
    try:
        cosmos_module = importlib.import_module("langgraph_checkpoint_cosmos")
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    CosmosDBSaver = getattr(cosmos_module, "CosmosDBSaver", None)
    if CosmosDBSaver is None:
        raise ImportError(
            "langgraph_checkpoint_cosmos is missing CosmosDBSaver; "
            "upgrade langgraph-checkpoint-cosmos to >=0.1.1,<0.2."
        )

    resolved_credential: Any
    if credential is None:
        try:
            identity_module = importlib.import_module("azure.identity")
        except ImportError as exc:
            raise ImportError(
                "credential=None requires azure-identity. "
                "It is normally installed as a dependency of langgraph-checkpoint-cosmos; "
                "if missing, run: pip install azure-identity>=1.16"
            ) from exc
        DefaultAzureCredential = getattr(identity_module, "DefaultAzureCredential", None)
        if DefaultAzureCredential is None:
            raise ImportError(
                "azure.identity is missing DefaultAzureCredential; "
                "upgrade azure-identity to >=1.16,<2."
            )
        resolved_credential = DefaultAzureCredential()
    else:
        resolved_credential = credential

    cm = CosmosDBSaver.from_conn_info(
        endpoint=endpoint,
        credential=resolved_credential,
        database_name=database_name,
        container_name=container_name,
    )

    # from_conn_info is a @contextmanager that yields the actual
    # CosmosDBSaver instance.  We must capture the return value of
    # __enter__ — it is the yielded saver, not the CM wrapper.
    saver = cm.__enter__()

    # Stash the context manager so it is not garbage-collected (which
    # would trigger __exit__ on a generator-based CM) and remains
    # available for future cleanup if needed.
    saver._langgraph_cm = cm  # noqa: SLF001

    return saver


__all__ = ["create_cosmos_checkpointer"]
