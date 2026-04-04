"""Tests for platform._sse — SSE event formatting (issue #39).

Every test verifies the exact byte output so that any drift from the
``langgraph_sdk`` wire format is caught immediately.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from azure_functions_langgraph.platform._sse import (
    format_data_event,
    format_end_event,
    format_error_event,
    format_metadata_event,
)

# ---------------------------------------------------------------------------
# Helpers — mini SDK decoder that mirrors langgraph_sdk.sse.SSEDecoder
# ---------------------------------------------------------------------------


class _StreamPart:
    """Minimal replica of ``langgraph_sdk.schema.StreamPart``."""

    __slots__ = ("event", "data")

    def __init__(self, event: str, data: Any) -> None:
        self.event = event
        self.data = data


def _decode_sse_frame(frame: str) -> _StreamPart:
    """Parse a single SSE frame (``event: ...\\ndata: ...\\n\\n``) the same
    way the SDK's ``SSEDecoder`` would.

    Raises ``ValueError`` if the frame is malformed.
    """
    event_name: str | None = None
    data_str: str | None = None

    for line in frame.strip().split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: "):]
        elif line.startswith("data: "):
            data_str = line[len("data: "):]

    if event_name is None:
        raise ValueError(f"Missing event field in frame: {frame!r}")

    # SDK: orjson.loads(data) if data else None
    if data_str is None or data_str == "":
        parsed_data: Any = None
    else:
        parsed_data = json.loads(data_str)

    return _StreamPart(event=event_name, data=parsed_data)


def _decode_sse_body(body: str) -> list[_StreamPart]:
    """Split a full SSE response body into decoded stream parts."""
    frames = [f for f in body.split("\n\n") if f.strip()]
    return [_decode_sse_frame(f) for f in frames]


# ---------------------------------------------------------------------------
# format_metadata_event
# ---------------------------------------------------------------------------


class TestFormatMetadataEvent:
    def test_exact_output(self) -> None:
        result = format_metadata_event("abc-123")
        assert result == 'event: metadata\ndata: {"run_id": "abc-123"}\n\n'

    def test_double_newline_terminator(self) -> None:
        result = format_metadata_event("x")
        assert result.endswith("\n\n")

    def test_sdk_parseable(self) -> None:
        result = format_metadata_event("run-42")
        part = _decode_sse_frame(result)
        assert part.event == "metadata"
        assert part.data == {"run_id": "run-42"}

    def test_uuid_with_dashes(self) -> None:
        run_id = "550e8400-e29b-41d4-a716-446655440000"
        result = format_metadata_event(run_id)
        part = _decode_sse_frame(result)
        assert part.data["run_id"] == run_id


# ---------------------------------------------------------------------------
# format_data_event
# ---------------------------------------------------------------------------


class TestFormatDataEvent:
    def test_dict_payload(self) -> None:
        result = format_data_event("values", {"messages": [{"role": "ai", "content": "hi"}]})
        assert result.startswith("event: values\ndata: ")
        assert result.endswith("\n\n")
        part = _decode_sse_frame(result)
        assert part.event == "values"
        assert part.data["messages"][0]["content"] == "hi"

    def test_updates_stream_mode(self) -> None:
        result = format_data_event("updates", {"node": {"key": "val"}})
        part = _decode_sse_frame(result)
        assert part.event == "updates"
        assert part.data == {"node": {"key": "val"}}

    def test_empty_dict(self) -> None:
        result = format_data_event("values", {})
        part = _decode_sse_frame(result)
        assert part.event == "values"
        assert part.data == {}

    def test_non_dict_list_wrapped(self) -> None:
        """Non-dict payloads should be wrapped as {"data": payload}."""
        result = format_data_event("values", [1, 2, 3])
        part = _decode_sse_frame(result)
        assert part.data == {"data": [1, 2, 3]}

    def test_non_dict_string_wrapped(self) -> None:
        result = format_data_event("values", "hello")
        part = _decode_sse_frame(result)
        assert part.data == {"data": "hello"}

    def test_non_dict_number_wrapped(self) -> None:
        result = format_data_event("values", 42)
        part = _decode_sse_frame(result)
        assert part.data == {"data": 42}

    def test_non_dict_none_wrapped(self) -> None:
        result = format_data_event("values", None)
        part = _decode_sse_frame(result)
        assert part.data == {"data": None}

    def test_non_dict_bool_wrapped(self) -> None:
        result = format_data_event("values", True)
        part = _decode_sse_frame(result)
        assert part.data == {"data": True}

    def test_non_serializable_uses_default_str(self) -> None:
        """Objects that aren't JSON-serializable should use default=str."""
        from datetime import datetime, timezone

        dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
        result = format_data_event("values", {"ts": dt})
        part = _decode_sse_frame(result)
        assert isinstance(part.data["ts"], str)

    def test_exact_bytes_for_simple_dict(self) -> None:
        result = format_data_event("values", {"key": "val"})
        expected = 'event: values\ndata: {"key": "val"}\n\n'
        assert result == expected


# ---------------------------------------------------------------------------
# format_error_event
# ---------------------------------------------------------------------------


class TestFormatErrorEvent:
    def test_exact_output(self) -> None:
        result = format_error_event("something went wrong")
        assert result == 'event: error\ndata: {"error": "something went wrong"}\n\n'

    def test_sdk_parseable(self) -> None:
        result = format_error_event("boom")
        part = _decode_sse_frame(result)
        assert part.event == "error"
        assert part.data == {"error": "boom"}

    def test_special_characters_escaped(self) -> None:
        result = format_error_event('quote "here" and backslash \\')
        part = _decode_sse_frame(result)
        assert part.data["error"] == 'quote "here" and backslash \\'

    def test_double_newline_terminator(self) -> None:
        result = format_error_event("x")
        assert result.endswith("\n\n")


# ---------------------------------------------------------------------------
# format_end_event
# ---------------------------------------------------------------------------


class TestFormatEndEvent:
    def test_exact_output(self) -> None:
        result = format_end_event()
        assert result == "event: end\ndata: null\n\n"

    def test_sdk_parseable_data_is_none(self) -> None:
        """SDK parses ``data: null`` as Python ``None``."""
        result = format_end_event()
        part = _decode_sse_frame(result)
        assert part.event == "end"
        assert part.data is None

    def test_double_newline_terminator(self) -> None:
        result = format_end_event()
        assert result.endswith("\n\n")


# ---------------------------------------------------------------------------
# Full stream sequence — decode a multi-frame SSE body
# ---------------------------------------------------------------------------


class TestFullStreamSequence:
    def test_metadata_data_end_sequence(self) -> None:
        """Typical successful stream: metadata → data → data → end."""
        body = (
            format_metadata_event("run-1")
            + format_data_event("values", {"messages": ["a"]})
            + format_data_event("values", {"messages": ["a", "b"]})
            + format_end_event()
        )
        parts = _decode_sse_body(body)
        assert len(parts) == 4
        assert parts[0].event == "metadata"
        assert parts[0].data["run_id"] == "run-1"
        assert parts[1].event == "values"
        assert parts[2].event == "values"
        assert parts[3].event == "end"
        assert parts[3].data is None

    def test_error_then_end_sequence(self) -> None:
        """Error stream: metadata → error → end."""
        body = (
            format_metadata_event("run-2")
            + format_error_event("graph exploded")
            + format_end_event()
        )
        parts = _decode_sse_body(body)
        assert len(parts) == 3
        assert parts[0].event == "metadata"
        assert parts[1].event == "error"
        assert parts[1].data == {"error": "graph exploded"}
        assert parts[2].event == "end"
        assert parts[2].data is None

    def test_mixed_data_then_error_sequence(self) -> None:
        """Stream that emits some data before erroring."""
        body = (
            format_metadata_event("run-3")
            + format_data_event("values", {"x": 1})
            + format_error_event("max bytes")
            + format_end_event()
        )
        parts = _decode_sse_body(body)
        assert len(parts) == 4
        assert [p.event for p in parts] == ["metadata", "values", "error", "end"]


class TestNonFiniteFloat:
    """Non-finite float handling (NaN, Infinity)."""

    def test_nan_in_dict_payload_raises(self) -> None:
        """NaN values must raise ValueError (SDK rejects them)."""
        with pytest.raises(ValueError, match="not JSON compliant"):
            format_data_event("values", {"x": float("nan")})

    def test_infinity_in_dict_payload_raises(self) -> None:
        """Infinity values must raise ValueError."""
        with pytest.raises(ValueError, match="not JSON compliant"):
            format_data_event("values", {"x": float("inf")})

    def test_neg_infinity_in_dict_payload_raises(self) -> None:
        with pytest.raises(ValueError, match="not JSON compliant"):
            format_data_event("values", {"x": float("-inf")})

    def test_nan_in_non_dict_payload_raises(self) -> None:
        """Non-dict wrapping path also rejects NaN."""
        with pytest.raises(ValueError, match="not JSON compliant"):
            format_data_event("values", float("nan"))
