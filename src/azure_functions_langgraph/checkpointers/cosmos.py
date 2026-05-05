"""Cosmos DB checkpointer DX helper.

.. versionadded:: 0.7.0

Thin wrapper around the upstream :pypi:`langgraph-checkpoint-cosmosdb`
package that builds a :class:`CosmosDBSaver` suitable for Azure Functions
cold-start (module-level instantiation).

The upstream ``__init__`` reads connection details from environment
variables.  This helper temporarily sets those env vars, constructs the
saver, then restores the original environment.

This module deliberately does **not** reimplement Cosmos DB checkpoint
storage.  It centralizes the connection convention and emits a clear
ImportError pointing at the right extra when the upstream package is
missing.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph_checkpoint_cosmosdb import CosmosDBSaver


_EXTRA_HINT = (
    "Cosmos DB checkpointer requires the 'cosmos' extra: "
    "pip install azure-functions-langgraph[cosmos]"
)


def create_cosmos_checkpointer(
    *,
    endpoint: str,
    database_name: str,
    container_name: str,
    key: str | None = None,
    credential: object | None = None,
) -> CosmosDBSaver:
    """Create a :class:`CosmosDBSaver` from connection info.

    Instantiates the upstream saver directly, temporarily setting the
    ``COSMOSDB_ENDPOINT`` and ``COSMOSDB_KEY`` environment variables that
    the upstream constructor reads.

    Provide **one** of ``key`` or ``credential``:

    - ``key``: A Cosmos DB master key (string).  This is passed directly
      to the upstream ``from_conn_info(key=...)``.
    - ``credential``: **Deprecated alias** — if a string is provided it
      is treated as ``key``.  Non-string credential objects are not
      supported by the upstream package.

    If neither is supplied, the ``COSMOS_KEY`` environment variable is
    read as a fallback.

    Args:
        endpoint: Cosmos DB account endpoint
            (e.g. ``https://<account>.documents.azure.com:443/``).
        database_name: Cosmos DB database name.
        container_name: Cosmos DB container name.  The container **must**
            be created with partition key path ``/partition_key``.
        key: Cosmos DB master key (preferred).
        credential: Deprecated — use ``key`` instead.  If a string is
            passed it is treated as the master key.

    Returns:
        A :class:`CosmosDBSaver` ready to be passed to
        ``builder.compile(checkpointer=...)``.

    Raises:
        ImportError: If ``langgraph-checkpoint-cosmosdb`` is not installed.
            Install via the ``cosmos`` extra.
        ValueError: If no key is provided and ``COSMOS_KEY`` env var is unset.
    """
    try:
        cosmos_module = importlib.import_module("langgraph_checkpoint_cosmosdb")
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    CosmosDBSaver = getattr(cosmos_module, "CosmosDBSaver", None)
    if CosmosDBSaver is None:
        raise ImportError(
            "langgraph_checkpoint_cosmosdb is missing CosmosDBSaver; "
            "upgrade langgraph-checkpoint-cosmosdb to >=0.2.0,<0.3."
        )

    # Resolve the key from the various input options.
    resolved_key: str
    if key is not None:
        resolved_key = key
    elif credential is not None:
        # Back-compat: treat string credential as key.
        if isinstance(credential, str):
            resolved_key = credential
        else:
            raise TypeError(
                "langgraph-checkpoint-cosmosdb requires a master key string, "
                "not an Azure Identity credential object. "
                "Pass key=<master-key> instead."
            )
    else:
        import os

        env_key = os.environ.get("COSMOS_KEY", "")
        if not env_key:
            raise ValueError(
                "No Cosmos DB key provided. Pass key=... or set the "
                "COSMOS_KEY environment variable."
            )
        resolved_key = env_key

    # The upstream from_conn_info has a bug (passes 4 positional args to
    # __init__ which only accepts 2).  We instantiate directly by setting
    # the env vars that __init__ reads, then construct the saver.
    import os

    original_endpoint = os.environ.get("COSMOSDB_ENDPOINT")
    original_key = os.environ.get("COSMOSDB_KEY")
    try:
        os.environ["COSMOSDB_ENDPOINT"] = endpoint
        os.environ["COSMOSDB_KEY"] = resolved_key
        saver = CosmosDBSaver(database_name=database_name, container_name=container_name)
    finally:
        # Restore original env vars
        if original_endpoint is None:
            os.environ.pop("COSMOSDB_ENDPOINT", None)
        else:
            os.environ["COSMOSDB_ENDPOINT"] = original_endpoint
        if original_key is None:
            os.environ.pop("COSMOSDB_KEY", None)
        else:
            os.environ["COSMOSDB_KEY"] = original_key

    return saver


def close_cosmos_checkpointer(saver: CosmosDBSaver) -> None:
    """Close a :class:`CosmosDBSaver` created by :func:`create_cosmos_checkpointer`.

    Closes the underlying Cosmos DB client to release resources.
    Safe to call multiple times; the second and subsequent calls are no-ops.

    Args:
        saver: A saver returned by :func:`create_cosmos_checkpointer`.
    """
    if getattr(saver, "_langgraph_closed", False):
        return  # already closed — idempotent no-op
    client = getattr(saver, "client", None)
    if client is not None and hasattr(client, "__del__"):
        try:
            client.__del__()
        except Exception:  # noqa: BLE001
            pass
    saver._langgraph_closed = True  # noqa: SLF001

__all__ = ["create_cosmos_checkpointer", "close_cosmos_checkpointer"]
