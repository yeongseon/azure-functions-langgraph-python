"""Cosmos DB checkpointer DX helper.

.. versionadded:: 0.7.0

Thin wrapper around the upstream :pypi:`langgraph-checkpoint-cosmosdb`
package that resolves credentials and builds a :class:`CosmosDBSaver`
suitable for Azure Functions cold-start (module-level instantiation).

The helper uses **key-based authentication**.  It temporarily wires the
``COSMOSDB_ENDPOINT`` and ``COSMOSDB_KEY`` environment variables that the
upstream package expects, directly instantiates ``CosmosDBSaver``, and
restores the original environment afterwards.

This module deliberately does **not** reimplement Cosmos DB checkpoint
storage.  It centralizes the credential convention, handles environment
variable wiring, and emits a clear ImportError pointing at the right
extra when the upstream package is missing.

.. note::

   Managed Identity / ``DefaultAzureCredential`` is not supported by this
   helper yet.  If upstream adds ``TokenCredential`` support later, this
   helper can be updated.
"""

from __future__ import annotations

import importlib
import os
import threading
from typing import TYPE_CHECKING, Any
import warnings
import weakref

if TYPE_CHECKING:
    from langgraph_checkpoint_cosmosdb import CosmosDBSaver


_EXTRA_HINT = (
    "Cosmos DB checkpointer requires the 'cosmos' extra: "
    "pip install azure-functions-langgraph[cosmos]"
)

# Thread-safety for env var manipulation during saver creation.
_env_lock = threading.Lock()

# Track savers created by this helper for close_cosmos_checkpointer().
# WeakSet allows GC of savers that go out of scope without explicit close.
# Falls back to marker attribute if upstream doesn't support weakrefs.
_managed_savers: weakref.WeakSet[Any] = weakref.WeakSet()
_USE_MARKER_FALLBACK = False


def create_cosmos_checkpointer(
    *,
    endpoint: str,
    key: str | None = None,
    credential: str | None = None,
    database_name: str,
    container_name: str,
) -> CosmosDBSaver:
    """Create a long-lived :class:`CosmosDBSaver` from connection info.

    Uses key-based authentication.  The helper temporarily sets the
    ``COSMOSDB_ENDPOINT`` and ``COSMOSDB_KEY`` environment variables
    required by the upstream package, instantiates the saver, and
    restores the original environment.

    Key resolution order:
        1. ``key`` parameter (preferred)
        2. ``credential`` parameter (deprecated compatibility path, string only)
        3. ``COSMOS_KEY`` environment variable
        4. ``ValueError`` if none of the above

    Args:
        endpoint: Cosmos DB account endpoint
            (e.g. ``https://<account>.documents.azure.com:443/``).
        key: Cosmos DB account key.
        credential: **Deprecated.** Use ``key`` instead.  Accepts a string
            key for backward compatibility.  Non-string values raise
            ``TypeError``.
        database_name: Cosmos DB database name.
        container_name: Cosmos DB container name.  The container **must**
            be created with partition key path ``/partition_key``.

    Returns:
        A :class:`CosmosDBSaver` ready to be passed to
        ``builder.compile(checkpointer=...)``.

    Raises:
        TypeError: If ``key`` and ``credential`` are both provided, or if
            ``credential`` is not a string.
        ValueError: If no key can be resolved from parameters or environment.
        ImportError: If ``langgraph-checkpoint-cosmosdb`` is not installed.
            Install via the ``cosmos`` extra.
    """
    global _USE_MARKER_FALLBACK  # noqa: PLW0603

    # --- Credential resolution ---
    if key is not None and credential is not None:
        raise TypeError(
            "Cannot specify both 'key' and 'credential'. "
            "Use 'key' only; 'credential' is deprecated."
        )

    resolved_key: str | None = key

    if credential is not None:
        if not isinstance(credential, str):
            raise TypeError(
                f"'credential' must be a string (Cosmos DB account key), "
                f"got {type(credential).__name__}. "
                f"Managed Identity / DefaultAzureCredential is not supported "
                f"by this helper. Use 'key' parameter with a string key."
            )
        warnings.warn(
            "The 'credential' parameter is deprecated. Use 'key' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        resolved_key = credential

    if resolved_key is None:
        resolved_key = os.environ.get("COSMOS_KEY")

    if resolved_key is None:
        raise ValueError(
            "No Cosmos DB key provided. Supply 'key' parameter, or set the "
            "COSMOS_KEY environment variable."
        )

    # --- Import upstream ---
    try:
        cosmos_module = importlib.import_module("langgraph_checkpoint_cosmosdb")
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    CosmosDBSaverCls = getattr(cosmos_module, "CosmosDBSaver", None)
    if CosmosDBSaverCls is None:
        raise ImportError(
            "langgraph_checkpoint_cosmosdb is missing CosmosDBSaver; "
            "upgrade langgraph-checkpoint-cosmosdb to >=0.2.0,<0.3."
        )

    # --- Env var wiring (thread-safe) ---
    with _env_lock:
        old_endpoint = os.environ.get("COSMOSDB_ENDPOINT")
        old_key = os.environ.get("COSMOSDB_KEY")
        try:
            os.environ["COSMOSDB_ENDPOINT"] = endpoint
            os.environ["COSMOSDB_KEY"] = resolved_key
            saver = CosmosDBSaverCls(
                database_name=database_name,
                container_name=container_name,
            )
            # When running against the Cosmos emulator (self-signed cert),
            # replace the internal client with one that skips SSL verification.
            if os.environ.get("COSMOS_DISABLE_SSL", "").lower() in ("true", "1", "yes"):
                cosmos_sdk = importlib.import_module("azure.cosmos")
                CosmosClientCls = getattr(cosmos_sdk, "CosmosClient")
                saver.client = CosmosClientCls(
                    url=endpoint, credential=resolved_key, connection_verify=False
                )
                saver.database = saver.client.create_database_if_not_exists(
                    id=database_name
                )
                saver.container = saver.database.create_container_if_not_exists(
                    id=container_name,
                    partition_key=getattr(cosmos_sdk, "PartitionKey")(
                        path="/partition_key"
                    ),
                )
        finally:
            # Restore original env vars
            if old_endpoint is None:
                os.environ.pop("COSMOSDB_ENDPOINT", None)
            else:
                os.environ["COSMOSDB_ENDPOINT"] = old_endpoint

            if old_key is None:
                os.environ.pop("COSMOSDB_KEY", None)
            else:
                os.environ["COSMOSDB_KEY"] = old_key

    # --- Track saver ---
    if not _USE_MARKER_FALLBACK:
        try:
            _managed_savers.add(saver)
        except TypeError:
            # Upstream uses __slots__ without __weakref__; fall back to marker
            _USE_MARKER_FALLBACK = True
            saver._managed_by_cosmos_helper = True  # noqa: SLF001
    else:
        saver._managed_by_cosmos_helper = True  # noqa: SLF001

    return saver


def close_cosmos_checkpointer(saver: CosmosDBSaver) -> None:
    """Close a :class:`CosmosDBSaver` created by :func:`create_cosmos_checkpointer`.

    Calls the underlying client's ``close()`` method if available to release
    Cosmos DB client resources.  Safe to call multiple times; the second and
    subsequent calls are no-ops.

    Args:
        saver: A saver returned by :func:`create_cosmos_checkpointer`.

    Raises:
        TypeError: If *saver* was not created by
            :func:`create_cosmos_checkpointer`.
    """
    # Check if already closed
    if getattr(saver, "_cosmos_helper_closed", False):
        return  # idempotent no-op

    # Verify this saver was created by our helper
    is_managed = saver in _managed_savers or getattr(saver, "_managed_by_cosmos_helper", False)
    if not is_managed:
        raise TypeError(
            "saver was not created by create_cosmos_checkpointer (not tracked by helper)"
        )

    # Close: prefer saver.close(), fall back to saver.client.close()
    saver_close = getattr(saver, "close", None)
    if callable(saver_close):
        saver_close()
    else:
        client = getattr(saver, "client", None)
        if client is not None:
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                close_fn()

    # Mark closed
    saver._cosmos_helper_closed = True  # noqa: SLF001

    # Remove from tracking
    _managed_savers.discard(saver)


__all__ = ["create_cosmos_checkpointer", "close_cosmos_checkpointer"]
