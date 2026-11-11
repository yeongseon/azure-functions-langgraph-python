"""Offline v1-versus-v2 output-shape comparison for the versioned_output_agent example (#825).

Drives the example's deterministic counter graph through the real ``invoke``/``stream`` handlers
in-process (no Azure Functions host, no network, no credentials, no model calls) and asserts the
public JSON/SSE shape of the default (v1) response against the opt-in ``version="v2"`` response.
"""

from __future__ import annotations

from importlib.metadata import version as _pkg_version
import json
from pathlib import Path
from typing import Any

import azure.functions as func
import pytest

from tests.example_imports import load_example_file

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "versioned_output_agent"

_major, _minor = (int(p) for p in _pkg_version("langgraph").split(".")[:2])
requires_v2 = pytest.mark.skipif(
    (_major, _minor) < (1, 1),
    reason="langgraph<1.1 does not support version='v2'",
)

FINAL_STATE = {"count": 4, "history": [2, 4]}
PAYLOAD: dict[str, Any] = {"input": {"count": 1, "history": []}}


@pytest.fixture()
def compare() -> Any:
    return load_example_file(EXAMPLE_DIR / "compare_shapes.py", "_versioned_output_compare")


@pytest.fixture()
def handlers(compare: Any) -> Any:
    return compare.build_handlers()


def _invoke(compare: Any, handlers: Any, **extra: Any) -> dict[str, Any]:
    resp = compare.call(handlers, "invoke", {**PAYLOAD, **extra})
    assert resp.status_code == 200
    body: dict[str, Any] = json.loads(resp.get_body())
    return body


def test_invoke_v1_is_flat_state(compare: Any, handlers: Any) -> None:
    body = _invoke(compare, handlers)
    assert body == {"output": FINAL_STATE}


@requires_v2
def test_invoke_v2_wraps_state_in_graph_output(compare: Any, handlers: Any) -> None:
    body = _invoke(compare, handlers, version="v2")
    assert body == {"output": {"value": FINAL_STATE, "interrupts": []}}


def test_stream_v1_events_are_bare_state_snapshots(compare: Any, handlers: Any) -> None:
    resp = compare.call(handlers, "stream", {**PAYLOAD, "stream_mode": "values"})
    assert resp.status_code == 200
    events = compare.stream_data_events(resp)
    assert events == [
        {"count": 1, "history": []},
        {"count": 2, "history": [2]},
        FINAL_STATE,
    ]


@requires_v2
def test_stream_v2_events_are_stream_parts(compare: Any, handlers: Any) -> None:
    resp = compare.call(handlers, "stream", {**PAYLOAD, "stream_mode": "values", "version": "v2"})
    assert resp.status_code == 200
    events = compare.stream_data_events(resp)
    assert len(events) == 3
    for event in events:
        assert set(event) == {"type", "ns", "data", "interrupts"}
        assert event["type"] == "values"
        assert event["ns"] == []
        assert event["interrupts"] == []
    assert events[-1]["data"] == FINAL_STATE


@requires_v2
def test_v2_stream_payload_equals_v1_stream_payload(compare: Any, handlers: Any) -> None:
    """Migration invariant: v2 only wraps each v1 snapshot; the state itself is unchanged."""
    v1 = compare.stream_data_events(
        compare.call(handlers, "stream", {**PAYLOAD, "stream_mode": "values"})
    )
    v2 = compare.stream_data_events(
        compare.call(handlers, "stream", {**PAYLOAD, "stream_mode": "values", "version": "v2"})
    )
    assert [e["data"] for e in v2] == v1


def test_unknown_version_is_rejected(compare: Any, handlers: Any) -> None:
    resp: func.HttpResponse = compare.call(handlers, "invoke", {**PAYLOAD, "version": "v9"})
    assert resp.status_code == 422
