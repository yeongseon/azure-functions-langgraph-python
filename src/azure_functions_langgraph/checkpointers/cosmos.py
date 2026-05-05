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

Environment variables
---------------------
``COSMOS_KEY`` is a **wrapper-only** convention defined by this helper for
convenience when no ``key`` argument is passed.  It is **not** read by the
upstream ``langgraph-checkpoint-cosmosdb`` package itself.  The upstream
package reads ``COSMOSDB_ENDPOINT`` and ``COSMOSDB_KEY`` from the process
environment during its ``__init__``; this helper sets those transiently.
"""

from __future__ import annotations

import importlib
import threading
from typing import TYPE_CHECKING
import warnings
import weakref

if TYPE_CHECKING:
    from langgraph_checkpoint_cosmosdb import CosmosDBSaver


_EXTRA_HINT = (
    "Cosmos DB checkpointer requires the 'cosmos' extra: "
    "pip install azure-functions-langgraph[cosmos]"
)

# Lock to protect env var manipulation during construction.
_env_lock = threading.Lock()

# Track savers created by this helper so close_cosmos_checkpointer can
# reject non-helper savers with a clear TypeError.
_managed_savers: weakref.WeakSet[object] = weakref.WeakSet()


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

    - ``key``: A Cosmos DB master key (string).  Preferred parameter.
    - ``credential``: **Deprecated** — if a string is provided it is
      treated as ``key``.  Non-string credential objects are not supported
      by the upstream package.

    If neither is supplied, the ``COSMOS_KEY`` environment variable is
    read as a fallback.  Note that ``COSMOS_KEY`` is a convention of this
    wrapper only — the upstream package does not read it.

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
        TypeError: If both ``key`` and ``credential`` are provided, or if
            ``credential`` is a non-string object.
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

    # Reject ambiguous calls where both key and credential are provided.
    if key is not None and credential is not None:
        raise TypeError(
            "Cannot pass both 'key' and 'credential'. Use 'key' only; 'credential' is deprecated."
        )

    # Resolve the key from the various input options.
    resolved_key: str
    if key is not None:
        resolved_key = key
    elif credential is not None:
        warnings.warn(
            "The 'credential' parameter is deprecated and will be removed in "
            "a future version. Use 'key' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
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

    with _env_lock:
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

    _managed_savers.add(saver)
    return saver


def close_cosmos_checkpointer(saver: CosmosDBSaver) -> None:
    """Close a :class:`CosmosDBSaver` created by :func:`create_cosmos_checkpointer`.

    Marks the saver as closed.  If the underlying Cosmos client exposes a
    ``close()`` method, it is called to release resources.  Safe to call
    multiple times; the second and subsequent calls are no-ops.

    Args:
        saver: A saver returned by :func:`create_cosmos_checkpointer`.

    Raises:
        TypeError: If *saver* was not created by :func:`create_cosmos_checkpointer`.
    """
    if saver not in _managed_savers:
        raise TypeError(
            "close_cosmos_checkpointer() only accepts savers created by "
            "create_cosmos_checkpointer(). Got an unmanaged saver instance."
        )

    if getattr(saver, "_langgraph_closed", False):
        return  # already closed — idempotent no-op

    client = getattr(saver, "client", None)
    if client is not None:
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:  # noqa: BLE001
                pass

    saver._langgraph_closed = True  # noqa: SLF001


__all__ = ["create_cosmos_checkpointer", "close_cosmos_checkpointer"]
