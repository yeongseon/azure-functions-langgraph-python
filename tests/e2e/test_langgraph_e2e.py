"""Real-Azure end-to-end tests for azure-functions-langgraph.

These exercise the NATIVE LangGraph routes on a live Azure Functions host that
was deployed from the release commit's own source (see the e2e-azure workflow
and examples/e2e_app). They are the runtime-behavior proof behind the release
gate's Azure certification.

Usage::

    E2E_BASE_URL=https://<app>.azurewebsites.net pytest tests/e2e/test_langgraph_e2e.py -v -m e2e

Every test is marked ``e2e``, so ordinary unit runs (``-m "not e2e"``) never hit
the network. Under the dedicated ``-m e2e`` selector an unset ``E2E_BASE_URL``
fails the run (see ``tests/e2e/conftest.py``) instead of skipping it.
"""

from __future__ import annotations

import json
import os
import time

import pytest
import requests

BASE_URL = os.environ.get("E2E_BASE_URL", "").rstrip("/")
SKIP_REASON = "E2E_BASE_URL not set — skipping real-Azure e2e tests"
GRAPH = "e2e_agent"

pytestmark = pytest.mark.e2e


def _url(path: str) -> str:
    return f"{BASE_URL}{path}"


@pytest.fixture(scope="session", autouse=True)
def warmup() -> None:
    """Retry /api/health until the Consumption cold-start finishes (max 3 min)."""
    if not BASE_URL:
        pytest.skip(SKIP_REASON)
    deadline = time.time() + 180
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            r = requests.get(_url("/api/health"), timeout=15)
            if r.status_code == 200:
                return
        except requests.RequestException as exc:  # pragma: no cover - network
            last_exc = exc
        time.sleep(5)
    raise AssertionError(f"Function App never became healthy: {last_exc}")


@pytest.mark.skipif(not BASE_URL, reason=SKIP_REASON)
def test_health_lists_registered_graph() -> None:
    # Liveness probe is minimal — status only, no graph enumeration.
    r = requests.get(_url("/api/health"), timeout=30)
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok", r.text
    # Detailed inventory lives on /health/details (anonymous in this e2e app,
    # which is configured with auth_level=ANONYMOUS).
    r = requests.get(_url("/api/health/details"), timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    names = {g.get("name") for g in body.get("graphs", [])}
    assert GRAPH in names, body


@pytest.mark.skipif(not BASE_URL, reason=SKIP_REASON)
def test_invoke_runs_the_graph() -> None:
    payload = {
        "input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""},
        "config": {"configurable": {"thread_id": "e2e-invoke-001"}},
    }
    r = requests.post(
        _url(f"/api/graphs/{GRAPH}/invoke"),
        json=payload,
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    output = body.get("output", body)
    # greet -> "Hello, World!"; farewell appends "... Goodbye!"
    assert output.get("greeting") == "Hello, World!", body
    contents = [m.get("content") for m in output.get("messages", [])]
    assert any("Goodbye!" in (c or "") for c in contents), body


def _parse_sse_events(text: str) -> list[tuple[str, str]]:
    """Parse ``event: <type>\\ndata: <payload>\\n\\n`` blocks into (event, data) pairs."""
    pairs: list[tuple[str, str]] = []
    event_type: str | None = None
    for raw in text.splitlines():
        if raw.startswith("event:"):
            event_type = raw[len("event:") :].strip()
        elif raw.startswith("data:"):
            pairs.append((event_type or "", raw[len("data:") :].strip()))
    return pairs


@pytest.mark.skipif(not BASE_URL, reason=SKIP_REASON)
def test_stream_emits_events() -> None:
    payload = {
        "input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""},
        "config": {"configurable": {"thread_id": "e2e-stream-001"}},
    }
    r = requests.post(
        _url(f"/api/graphs/{GRAPH}/stream"),
        json=payload,
        timeout=60,
        stream=True,
    )
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers.get("Content-Type", ""), r.headers
    events = _parse_sse_events(r.text)

    error_events = [data for event, data in events if event == "error"]
    assert not error_events, f"stream must not certify an error frame: {error_events}"

    assert events and events[-1][0] == "end", f"stream must end with 'event: end': {events}"

    saw_expected_graph_output = False
    for event, data in events:
        if event != "data":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if parsed.get("greeting") != "Hello, World!":
            continue
        contents = [m.get("content") for m in parsed.get("messages", [])]
        if any("Goodbye!" in (c or "") for c in contents):
            saw_expected_graph_output = True
            break

    assert saw_expected_graph_output, (
        f"stream never emitted the deterministic e2e_agent output "
        f"(greeting='Hello, World!' + a 'Goodbye!' message): {events}"
    )
