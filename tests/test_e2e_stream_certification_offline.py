"""Offline regression tests for the classic-stream release certification
assertion in tests/e2e/test_langgraph_e2e.py (#644).

test_stream_emits_events() accepted any nonempty JSON-decodable SSE
``data:`` line without checking the associated ``event:`` type or the
expected graph output. An adapter could legitimately encode a graph
failure as HTTP 200 plus an ``event: error`` frame, so the assertion
could certify a stream that produced no successful graph data.

These tests load the real e2e test module and invoke
test_stream_emits_events() directly against synthetic in-memory
``requests.Response`` objects (no network access, no live Azure host),
proving the strengthened assertion rejects error-only, terminal-only,
malformed-data, and unrelated-JSON responses while still accepting the
deterministic happy-path output.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent
_E2E_TEST_PATH = _REPO_ROOT / "tests" / "e2e" / "test_langgraph_e2e.py"

_spec = importlib.util.spec_from_file_location("review_e2e_stream", _E2E_TEST_PATH)
assert _spec and _spec.loader
e2e_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e_mod)


def _sse_response(body: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "text/event-stream"
    response._content = body
    response._content_consumed = True  # type: ignore[attr-defined]
    return response


def _run_with_mocked_stream(body: bytes) -> None:
    with patch.object(e2e_mod.requests, "post", return_value=_sse_response(body)):
        e2e_mod.test_stream_emits_events()


def test_error_only_stream_fails_certification() -> None:
    body = (
        b'event: error\ndata: {"error":"graph failed before producing output"}\n\n'
        b"event: end\ndata: {}\n\n"
    )
    with pytest.raises(AssertionError, match="error frame"):
        _run_with_mocked_stream(body)


def test_terminal_only_stream_fails_certification() -> None:
    body = b"event: end\ndata: {}\n\n"
    with pytest.raises(AssertionError, match="deterministic e2e_agent output"):
        _run_with_mocked_stream(body)


def test_malformed_data_stream_fails_certification() -> None:
    body = b"event: data\ndata: not-json\n\nevent: end\ndata: {}\n\n"
    with pytest.raises(AssertionError, match="deterministic e2e_agent output"):
        _run_with_mocked_stream(body)


def test_unrelated_json_stream_fails_certification() -> None:
    body = b'event: data\ndata: {"unrelated": "value"}\n\nevent: end\ndata: {}\n\n'
    with pytest.raises(AssertionError, match="deterministic e2e_agent output"):
        _run_with_mocked_stream(body)


def test_missing_end_frame_fails_certification() -> None:
    body = (
        b'event: data\ndata: {"greeting": "Hello, World!", '
        b'"messages": [{"content": "Goodbye!"}]}\n\n'
    )
    with pytest.raises(AssertionError, match="event: end"):
        _run_with_mocked_stream(body)


def test_happy_path_stream_passes_certification() -> None:
    body = (
        b'event: data\ndata: {"greeting": "Hello, World!", "messages": '
        b'[{"role": "assistant", "content": "Hello, World! Goodbye!"}]}\n\n'
        b"event: end\ndata: {}\n\n"
    )
    _run_with_mocked_stream(body)  # must not raise
