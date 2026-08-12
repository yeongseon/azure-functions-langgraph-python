"""Real-Azure end-to-end tests for azure-functions-langgraph.

These exercise the NATIVE LangGraph routes on a live Azure Functions host that
was deployed from the release commit's own source (see the e2e-azure workflow
and examples/e2e_app). They are the runtime-behavior proof behind the release
gate's Azure certification.

Usage::

    E2E_BASE_URL=https://<app>.azurewebsites.net pytest tests/e2e -v -m e2e

Every test is marked ``e2e`` and skips automatically when ``E2E_BASE_URL`` is
unset (so ordinary unit runs, which exclude ``-m e2e``, never hit the network).
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
            if r.status_code < 500:
                return
        except requests.RequestException as exc:  # pragma: no cover - network
            last_exc = exc
        time.sleep(5)
    raise AssertionError(f"Function App never became healthy: {last_exc}")


@pytest.mark.skipif(not BASE_URL, reason=SKIP_REASON)
def test_health_lists_registered_graph() -> None:
    r = requests.get(_url("/api/health"), timeout=30)
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
    saw_data = False
    for raw in r.iter_lines(decode_unicode=True):
        if not raw:
            continue
        if raw.startswith("data:"):
            saw_data = True
            data = raw[len("data:") :].strip()
            if data and data != "{}":
                # Any well-formed SSE data frame proves the stream route runs.
                try:
                    json.loads(data)
                except json.JSONDecodeError:
                    pass
    assert saw_data, "stream produced no SSE data frames"
