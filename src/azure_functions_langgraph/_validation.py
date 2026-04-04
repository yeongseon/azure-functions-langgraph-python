"""Transport-agnostic input validation utilities.

All validators are pure functions that return an error message string on
failure or ``None`` on success.  Route layers (native and platform) are
responsible for converting error messages into their own HTTP response
format (``ErrorResponse`` vs ``_platform_error``).

.. versionadded:: 0.3.0
"""

from __future__ import annotations

import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Graph-name / assistant-id validation
# ---------------------------------------------------------------------------

#: Pattern for valid graph names: starts with letter, then letters/digits/
#: underscores/hyphens, 1–64 characters total.
_GRAPH_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


def validate_graph_name(name: str) -> Optional[str]:
    """Validate a graph registration name or assistant_id.

    Returns an error message if invalid, ``None`` if valid.
    """
    if not name:
        return "Graph name must not be empty"
    if not _GRAPH_NAME_RE.match(name):
        return (
            f"Invalid graph name {name!r}. "
            "Must start with a letter, contain only letters, digits, "
            "underscores or hyphens, and be 1–64 characters."
        )
    return None


# ---------------------------------------------------------------------------
# Thread-id validation
# ---------------------------------------------------------------------------

#: Maximum allowed thread_id length.
_MAX_THREAD_ID_LENGTH = 256

#: Pattern for printable ASCII (space through tilde) — excludes control chars.
_PRINTABLE_RE = re.compile(r"^[\x20-\x7e]+$")


def validate_thread_id(thread_id: str) -> Optional[str]:
    """Validate a thread_id string (permissive — non-empty printable, max length).

    Returns an error message if invalid, ``None`` if valid.
    """
    if not thread_id:
        return "Thread ID must not be empty"
    if len(thread_id) > _MAX_THREAD_ID_LENGTH:
        return (
            f"Thread ID exceeds maximum length of {_MAX_THREAD_ID_LENGTH} characters"
        )
    if not _PRINTABLE_RE.match(thread_id):
        return "Thread ID must contain only printable ASCII characters"
    return None


# ---------------------------------------------------------------------------
# Request body size validation
# ---------------------------------------------------------------------------


def validate_body_size(body: bytes, max_bytes: int) -> Optional[str]:
    """Check that *body* does not exceed *max_bytes*.

    Returns an error message if too large, ``None`` if within limit.
    Must be called **before** JSON parsing to reject oversized payloads early.
    """
    if len(body) > max_bytes:
        return (
            f"Request body too large: {len(body)} bytes "
            f"(max {max_bytes} bytes)"
        )
    return None


# ---------------------------------------------------------------------------
# Input / config depth and node-count validation
# ---------------------------------------------------------------------------


def _count_depth_and_nodes(
    data: Any,
    current_depth: int,
    max_depth: int,
    node_count: int,
    max_nodes: int,
) -> tuple[int, Optional[str]]:
    """Recursively count depth and nodes.

    Returns ``(updated_node_count, error_message_or_none)``.
    """
    if current_depth > max_depth:
        return node_count, (
            f"Input exceeds maximum nesting depth of {max_depth}"
        )
    if node_count > max_nodes:
        return node_count, (
            f"Input exceeds maximum node count of {max_nodes}"
        )

    if isinstance(data, dict):
        for value in data.values():
            node_count += 1
            if node_count > max_nodes:
                return node_count, (
                    f"Input exceeds maximum node count of {max_nodes}"
                )
            node_count, err = _count_depth_and_nodes(
                value, current_depth + 1, max_depth, node_count, max_nodes
            )
            if err:
                return node_count, err
    elif isinstance(data, list):
        for item in data:
            node_count += 1
            if node_count > max_nodes:
                return node_count, (
                    f"Input exceeds maximum node count of {max_nodes}"
                )
            node_count, err = _count_depth_and_nodes(
                item, current_depth + 1, max_depth, node_count, max_nodes
            )
            if err:
                return node_count, err

    return node_count, None


def validate_input_structure(
    data: Any,
    *,
    max_depth: int = 32,
    max_nodes: int = 10_000,
) -> Optional[str]:
    """Validate nesting depth and total node count of user-supplied data.

    Intended for the ``input`` and ``config``/``configurable`` fields —
    the only fields that can contain arbitrarily deep user data.

    Returns an error message if invalid, ``None`` if valid.
    """
    if not isinstance(data, (dict, list)):
        # Scalars are always fine
        return None
    _, err = _count_depth_and_nodes(data, 1, max_depth, 0, max_nodes)
    return err


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "validate_graph_name",
    "validate_thread_id",
    "validate_body_size",
    "validate_input_structure",
]
