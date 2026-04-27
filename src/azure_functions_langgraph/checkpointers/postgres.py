"""Postgres checkpointer DX helper.

.. versionadded:: 0.6.0

Thin wrapper around the upstream :mod:`langgraph.checkpoint.postgres`
package that owns the connection lifetime so the resulting saver can be
attached to a graph at module import time (the standard Azure Functions
cold-start pattern).

This module deliberately does **not** reimplement Postgres checkpoint
storage. It centralizes the env-var / connection-string convention,
runs ``setup()`` on cold start, and emits a clear ImportError pointing
at the right extra when the upstream package is missing.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres import PostgresSaver

logger = logging.getLogger(__name__)

_EXTRA_HINT = (
    "Postgres checkpointer requires the 'postgres' extra: "
    "pip install azure-functions-langgraph[postgres]"
)


def create_postgres_checkpointer(
    conn_string: str,
    *,
    setup: bool = True,
    autocommit: bool = True,
    prepare_threshold: int | None = 0,
) -> PostgresSaver:
    """Create a long-lived :class:`PostgresSaver` from a connection string.

    Opens a single :class:`psycopg.Connection` owned by the returned
    saver and (by default) runs ``setup()`` once so the migrations table
    and checkpoint tables exist. Intended for use at Azure Functions
    cold-start, where a single saver is attached to a registered graph
    for the lifetime of the worker process.

    Args:
        conn_string: psycopg-compatible connection string (e.g.
            ``postgresql://user:pass@host/db``).
        setup: When ``True`` (default), call ``saver.setup()`` after
            constructing it. Set to ``False`` if migrations are managed
            out-of-band (e.g. by a deployment pipeline).
        autocommit: Forwarded to :meth:`psycopg.Connection.connect`.
            ``True`` matches the upstream :meth:`PostgresSaver.from_conn_string`
            default and is required for ``setup()`` DDL to take effect
            without an explicit transaction.
        prepare_threshold: Forwarded to :meth:`psycopg.Connection.connect`.
            Defaults to ``0`` to match upstream
            :meth:`PostgresSaver.from_conn_string`. In psycopg, ``0`` means
            **prepare every query on first execution** (eager preparation,
            best for the LangGraph checkpoint workload of repeated identical
            statements). Pass ``None`` to disable prepared statements
            entirely (e.g. behind PgBouncer in transaction pooling mode,
            which does not preserve prepared-statement state across
            client connections).

    Returns:
        A :class:`PostgresSaver` ready to be passed to
        ``builder.compile(checkpointer=...)``.

    Raises:
        ImportError: If ``langgraph-checkpoint-postgres`` or ``psycopg``
            is not installed. Install via the ``postgres`` extra.
    """
    try:
        postgres_module = importlib.import_module("langgraph.checkpoint.postgres")
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    try:
        psycopg_module = importlib.import_module("psycopg")
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    PostgresSaver = getattr(postgres_module, "PostgresSaver", None)
    if PostgresSaver is None:
        raise ImportError(
            "langgraph.checkpoint.postgres is missing PostgresSaver; "
            "upgrade langgraph-checkpoint-postgres to >=3.0,<4."
        )

    Connection = getattr(psycopg_module, "Connection", None)
    if Connection is None:
        raise ImportError("psycopg is missing Connection; upgrade psycopg to >=3.0,<4.")

    try:
        rows_module = importlib.import_module("psycopg.rows")
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc
    dict_row = getattr(rows_module, "dict_row", None)
    if dict_row is None:
        raise ImportError("psycopg.rows is missing dict_row; upgrade psycopg to >=3.0,<4.")

    connect_kwargs: dict[str, Any] = {
        "autocommit": autocommit,
        "row_factory": dict_row,
    }
    if prepare_threshold is not None:
        connect_kwargs["prepare_threshold"] = prepare_threshold

    conn = Connection.connect(conn_string, **connect_kwargs)
    saver = PostgresSaver(conn)
    if setup:
        saver.setup()
    return saver


__all__ = ["create_postgres_checkpointer"]
