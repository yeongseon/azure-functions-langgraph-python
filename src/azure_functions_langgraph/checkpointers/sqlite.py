"""SQLite checkpointer DX helper.

.. versionadded:: 0.6.0

Thin wrapper around the upstream :mod:`langgraph.checkpoint.sqlite`
package that owns the connection lifetime so the resulting saver can be
attached to a graph at module import time. Primarily intended for local
development and single-instance deployments — for production multi-worker
workloads, prefer :func:`create_postgres_checkpointer`.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.checkpoint.sqlite import SqliteSaver

logger = logging.getLogger(__name__)

_EXTRA_HINT = (
    "SQLite checkpointer requires the 'sqlite' extra: "
    "pip install azure-functions-langgraph[sqlite]"
)


def create_sqlite_checkpointer(
    conn_string: str,
    *,
    setup: bool = True,
    check_same_thread: bool = False,
) -> SqliteSaver:
    """Create a long-lived :class:`SqliteSaver` from a connection string.

    Opens a single :class:`sqlite3.Connection` owned by the returned
    saver and (by default) runs ``setup()`` once so the checkpoint
    tables exist.

    Args:
        conn_string: SQLite path (e.g. ``"checkpoints.sqlite"``) or
            ``":memory:"``.
        setup: When ``True`` (default), call ``saver.setup()`` after
            constructing it.
        check_same_thread: Forwarded to :func:`sqlite3.connect`.
            Defaults to ``False`` to match upstream
            :meth:`SqliteSaver.from_conn_string`, which intentionally
            disables SQLite's same-thread check so the connection can be
            shared across the worker thread pool. See
            https://ricardoanderegg.com/posts/python-sqlite-thread-safety/

    Returns:
        A :class:`SqliteSaver` ready to be passed to
        ``builder.compile(checkpointer=...)``.

    Raises:
        ImportError: If ``langgraph-checkpoint-sqlite`` is not
            installed. Install via the ``sqlite`` extra.
    """
    try:
        sqlite_module = importlib.import_module("langgraph.checkpoint.sqlite")
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    SqliteSaver = getattr(sqlite_module, "SqliteSaver", None)
    if SqliteSaver is None:
        raise ImportError(
            "langgraph.checkpoint.sqlite is missing SqliteSaver; "
            "upgrade langgraph-checkpoint-sqlite to >=3.0,<4."
        )

    sqlite3_module = importlib.import_module("sqlite3")
    conn = sqlite3_module.connect(conn_string, check_same_thread=check_same_thread)
    saver = SqliteSaver(conn)
    if setup:
        saver.setup()
    return saver


__all__ = ["create_sqlite_checkpointer"]
