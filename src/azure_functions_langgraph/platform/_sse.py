"""Internal SSE event formatting for Platform API compatibility.

Produces Server-Sent Events in the wire format expected by
``langgraph_sdk``'s ``SSEDecoder``.  The native streaming format
(``_handlers.py``) is **not** affected by this module.

Wire-format contract (each frame terminated by ``\\n\\n``):

* ``event: metadata\\ndata: {"run_id": "<uuid>"}\\n\\n``   — always first
* ``event: <stream_mode>\\ndata: <json>\\n\\n``            — zero or more
* ``event: error\\ndata: {"error": "<message>"}\\n\\n``    — on failure
* ``event: end\\ndata: null\\n\\n``                         — always last

.. versionadded:: 0.3.0
"""

from __future__ import annotations

import json
from typing import Any


def format_metadata_event(run_id: str) -> str:
    """Format the metadata SSE event (always first in stream).

    >>> format_metadata_event("abc-123")
    'event: metadata\\ndata: {"run_id": "abc-123"}\\n\\n'
    """
    data = json.dumps({"run_id": run_id})
    return f"event: metadata\ndata: {data}\n\n"


def format_data_event(stream_mode: str, payload: Any) -> str:
    """Format a data-chunk SSE event.

    *payload* is serialised as-is when it is a ``dict``; non-dict values
    are wrapped in ``{"data": payload}`` to preserve their original type
    (lists, numbers, booleans, ``None``).  Non-finite floats (``NaN``,
    ``Infinity``) raise ``ValueError`` because the SDK's ``orjson.loads``
    rejects them.
    >>> format_data_event("values", {"messages": []})
    'event: values\\ndata: {"messages": []}\\n\\n'
    """
    if isinstance(payload, dict):
        serialized = json.dumps(payload, default=str, allow_nan=False)
    else:
        serialized = json.dumps({"data": payload}, default=str, allow_nan=False)
    return f"event: {stream_mode}\ndata: {serialized}\n\n"


def format_error_event(message: str) -> str:
    """Format an error SSE event.

    Uses ``{"error": "<message>"}`` to match existing handler conventions.

    >>> format_error_event("boom")
    'event: error\\ndata: {"error": "boom"}\\n\\n'
    """
    data = json.dumps({"error": message})
    return f"event: error\ndata: {data}\n\n"


def format_end_event() -> str:
    """Format the terminal end SSE event (always last in stream).

    The SDK's ``SSEDecoder`` parses ``data: null`` as Python ``None``.

    >>> format_end_event()
    'event: end\\ndata: null\\n\\n'
    """
    return "event: end\ndata: null\n\n"


__all__ = [
    "format_data_event",
    "format_end_event",
    "format_error_event",
    "format_metadata_event",
]
