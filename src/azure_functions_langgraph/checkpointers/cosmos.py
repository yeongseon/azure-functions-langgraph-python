"""Cosmos DB checkpointer DX helper.

.. versionadded:: 0.7.0

Thin wrapper around the upstream :pypi:`langgraph-checkpoint-cosmosdb`
package that resolves credentials and builds a :class:`CosmosDBSaver`
suitable for Azure Functions cold-start (module-level instantiation).

The helper uses **key-based authentication**.  It temporarily wires the
process-global ``COSMOSDB_ENDPOINT`` and ``COSMOSDB_KEY`` environment
variables that the upstream package expects, directly instantiates
``CosmosDBSaver``, and restores the original environment afterwards.  This
path is deprecated because unrelated threads can observe those temporary
credentials.

This module deliberately does **not** reimplement Cosmos DB checkpoint
storage.  It centralizes the credential convention, handles environment
variable wiring, and emits a clear ImportError pointing at the right
extra when the upstream package is missing.

.. note::

   This helper always uses **key-based authentication**.  The upstream
   ``CosmosDBSaver`` (>= 0.2.8) *does* support Managed Identity /
   ``DefaultAzureCredential`` when ``COSMOSDB_KEY`` is unset, but this DX
   wrapper resolves and wires a key, so the passwordless path is bypassed.
   To use Managed Identity today, instantiate the upstream
   ``CosmosDBSaver`` directly with only ``COSMOSDB_ENDPOINT`` set (no key).
   This helper is deprecated until upstream supports explicit endpoint and
   credential or client injection.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlsplit
import warnings
import weakref

if TYPE_CHECKING:
    from langgraph_checkpoint_cosmosdb import CosmosDBSaver


logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _endpoint_host(endpoint: str) -> str | None:
    """Return the endpoint host, or raise ``ValueError`` if the URL is ambiguous.

    Ambiguous means different parsers could disagree about where the request
    goes: backslashes, whitespace/control or non-ASCII characters, userinfo
    (``@`` in the authority), a parse failure, or a host that
    ``urllib.parse.urlparse`` (used by azure-cosmos to decide whether to skip
    TLS verification), ``urlsplit`` and ``urllib3.util.parse_url`` (used by
    ``requests``) do not extract identically. The message never contains the
    endpoint.
    """
    error = ValueError(
        "Cosmos DB endpoint is malformed or ambiguous (backslash, whitespace, control or "
        "non-ASCII character, userinfo, or inconsistent host); refusing to build a client."
    )
    if not endpoint.isascii() or not endpoint.isprintable():
        raise error
    if any(ch.isspace() or ch == "\\" for ch in endpoint):
        raise error
    try:
        parsed = urlparse(endpoint)
        if "@" in parsed.netloc:
            raise error
        host = parsed.hostname
        split_host = urlsplit(endpoint).hostname
        url_host = importlib.import_module("urllib3.util").parse_url(endpoint).host
    except ValueError as exc:
        raise error from exc
    except Exception as exc:  # urllib3 LocationParseError and friends
        raise error from exc
    url_host = url_host.strip("[]").lower() if url_host else None
    if host != split_host or host != url_host:
        raise error
    return host


def _is_loopback_endpoint(endpoint: str) -> bool:
    """Return True only for an unambiguous endpoint whose host is loopback."""
    try:
        return _endpoint_host(endpoint) in _LOOPBACK_HOSTS
    except ValueError:
        return False


_EXTRA_HINT = (
    "Cosmos DB checkpointer requires the 'cosmos' extra: "
    "pip install azure-functions-langgraph[cosmos]"
)

# Serializes cooperating helper calls but cannot isolate process-global values
# from unrelated threads.
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

    Uses key-based authentication.  This helper is deprecated because the
    supported upstream saver versions require process-global environment
    variables.  Unrelated threads can observe the temporary key while the
    saver is being constructed.

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
        ValueError: If no key can be resolved from parameters or environment,
            or if ``endpoint`` is malformed or ambiguous (raised before any
            client is created).
        ImportError: If ``langgraph-checkpoint-cosmosdb`` is not installed.
            Install via the ``cosmos`` extra.
    """
    global _USE_MARKER_FALLBACK

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

    warnings.warn(
        "create_cosmos_checkpointer is deprecated because supported upstream "
        "CosmosDBSaver versions require process-global credential environment "
        "variables that unrelated threads can observe. Instantiate the upstream "
        "saver from startup-configured environment variables instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    # Validate BEFORE any client exists: azure-cosmos disables TLS verification
    # for hosts it parses as localhost/127.0.0.1, so an ambiguous endpoint must
    # never reach the upstream constructor.
    _endpoint_host(endpoint)

    # --- Import upstream ---
    try:
        cosmos_module = importlib.import_module("langgraph_checkpoint_cosmosdb")
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    CosmosDBSaverCls = getattr(cosmos_module, "CosmosDBSaver", None)
    if CosmosDBSaverCls is None:
        raise ImportError(
            "langgraph_checkpoint_cosmosdb is missing CosmosDBSaver; "
            "upgrade langgraph-checkpoint-cosmosdb to >=0.2.0,<0.5."
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
            # When running against the local Cosmos emulator (self-signed cert),
            # replace the internal client with one that skips SSL verification.
            # Honored only for loopback endpoints; ignored for any other host.
            disable_ssl = os.environ.get("COSMOS_DISABLE_SSL", "").lower() in ("true", "1", "yes")
            if disable_ssl and not _is_loopback_endpoint(endpoint):
                logger.warning(
                    "COSMOS_DISABLE_SSL is ignored because the Cosmos endpoint is not a "
                    "loopback host (localhost, 127.0.0.1, ::1); TLS verification stays enabled."
                )
                disable_ssl = False
            if disable_ssl:
                cosmos_sdk = importlib.import_module("azure.cosmos")
                CosmosClientCls = getattr(cosmos_sdk, "CosmosClient")
                original_client = saver.client
                replacement_client = CosmosClientCls(
                    url=endpoint, credential=resolved_key, connection_verify=False
                )
                try:
                    original_client.close()
                except BaseException:
                    replacement_client.close()
                    raise
                saver.client = replacement_client
                saver.database = saver.client.create_database_if_not_exists(id=database_name)
                saver.container = saver.database.create_container_if_not_exists(
                    id=container_name,
                    partition_key=getattr(cosmos_sdk, "PartitionKey")(path="/partition_key"),
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
            saver._managed_by_cosmos_helper = True
    else:
        saver._managed_by_cosmos_helper = True

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
    saver._cosmos_helper_closed = True

    # Remove from tracking
    _managed_savers.discard(saver)


__all__ = ["create_cosmos_checkpointer", "close_cosmos_checkpointer"]
