"""Tests for platform._common shared helpers (issue #269).

Covers the SSE response builder and stream_mode normalizer extracted to
de-duplicate the platform run handlers.
"""

from __future__ import annotations

from azure_functions_langgraph.platform._common import (
    _build_sse_response,
    _normalize_stream_mode,
)


class TestBuildSSEResponse:
    def test_builds_event_stream_response_with_headers(self) -> None:
        resp = _build_sse_response(
            ["event: end\ndata: {}\n\n"],
            content_location="/api/runs/abc",
        )
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        assert resp.headers["Cache-Control"] == "no-cache"
        assert resp.headers["X-Accel-Buffering"] == "no"
        assert resp.headers["Content-Location"] == "/api/runs/abc"
        assert resp.get_body() == b"event: end\ndata: {}\n\n"

    def test_joins_multiple_chunks_in_order(self) -> None:
        resp = _build_sse_response(
            ["a", "b", "c"],
            content_location="/api/threads/t1/runs/r1",
        )
        assert resp.get_body() == b"abc"
        assert resp.headers["Content-Location"] == "/api/threads/t1/runs/r1"


class TestNormalizeStreamMode:
    def test_plain_string_passes_through(self) -> None:
        mode, err = _normalize_stream_mode("values")
        assert mode == "values"
        assert err is None

    def test_none_passes_through(self) -> None:
        mode, err = _normalize_stream_mode(None)
        assert mode is None
        assert err is None

    def test_single_element_list_collapses(self) -> None:
        mode, err = _normalize_stream_mode(["updates"])
        assert mode == "updates"
        assert err is None

    def test_empty_list_defaults_to_values(self) -> None:
        mode, err = _normalize_stream_mode([])
        assert mode == "values"
        assert err is None

    def test_multi_element_list_returns_501(self) -> None:
        mode, err = _normalize_stream_mode(["values", "updates"])
        assert mode is None
        assert err is not None
        assert err.status_code == 501
