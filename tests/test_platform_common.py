"""Tests for platform._common shared helpers (issue #269).

Covers the SSE response builder and stream_mode normalizer extracted to
de-duplicate the platform run handlers.
"""

from __future__ import annotations

import pytest

from azure_functions_langgraph.platform._common import (
    _build_sse_response,
    _check_unknown_platform_fields,
    _normalize_stream_mode,
    _platform_strict_enabled,
    _unknown_request_fields,
)
from azure_functions_langgraph.platform.contracts import ThreadUpdate


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



class TestUnknownRequestFields:
    def test_returns_empty_for_non_dict_body(self) -> None:
        assert _unknown_request_fields(ThreadUpdate, ["not", "a", "dict"]) == []
        assert _unknown_request_fields(ThreadUpdate, None) == []

    def test_returns_empty_when_all_fields_known(self) -> None:
        assert _unknown_request_fields(ThreadUpdate, {"metadata": {"a": 1}}) == []

    def test_reports_sorted_unknown_keys(self) -> None:
        assert _unknown_request_fields(ThreadUpdate, {"ttl": 5, "foo": 1}) == [
            "foo",
            "ttl",
        ]


class TestPlatformStrictEnabled:
    def test_unset_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AZFUNC_LANGGRAPH_PLATFORM_STRICT", raising=False)
        assert _platform_strict_enabled() is False

    def test_falsey_values_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("", "0", "false", "NO", " false "):
            monkeypatch.setenv("AZFUNC_LANGGRAPH_PLATFORM_STRICT", val)
            assert _platform_strict_enabled() is False

    def test_truthy_values_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("1", "true", "yes", "on"):
            monkeypatch.setenv("AZFUNC_LANGGRAPH_PLATFORM_STRICT", val)
            assert _platform_strict_enabled() is True


class TestCheckUnknownPlatformFields:
    def test_no_unknown_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AZFUNC_LANGGRAPH_PLATFORM_STRICT", raising=False)
        assert _check_unknown_platform_fields(ThreadUpdate, {"metadata": {}}) is None

    def test_default_warns_and_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AZFUNC_LANGGRAPH_PLATFORM_STRICT", raising=False)
        with pytest.warns(UserWarning, match="ttl"):
            result = _check_unknown_platform_fields(ThreadUpdate, {"ttl": 10})
        assert result is None

    def test_strict_mode_returns_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZFUNC_LANGGRAPH_PLATFORM_STRICT", "1")
        result = _check_unknown_platform_fields(ThreadUpdate, {"ttl": 10})
        assert result is not None
        assert result.status_code == 400
        assert b"ttl" in result.get_body()

    def test_non_dict_body_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZFUNC_LANGGRAPH_PLATFORM_STRICT", "1")
        assert _check_unknown_platform_fields(ThreadUpdate, "not-a-dict") is None