from __future__ import annotations

import json
import os
from pathlib import Path
import time
from uuid import uuid4

import pytest
import requests

BASE_URL = os.environ.get("E2E_STREAM_BASE_URL", "").rstrip("/")
FUNCTION_KEY = os.environ.get("E2E_STREAM_CODE", "")
EVIDENCE_PATH = os.environ.get("STREAM_TIMING_EVIDENCE", "")
SKIP_REASON = "E2E_STREAM_BASE_URL not set — skipping true-streaming Azure e2e tests"
LOCK_RELEASE_DEADLINE_S = 10.0
LOCK_POLL_INTERVAL_S = 0.1
pytestmark = pytest.mark.e2e


def _stream(*, thread_id: str, fail: bool = False) -> requests.Response:
    return requests.post(
        f"{BASE_URL}/api/graphs/true_streaming_agent/stream",
        # A header, never `?code=`: a URL ends up in logs and in `requests` connection errors.
        headers={"x-functions-key": FUNCTION_KEY} if FUNCTION_KEY else {},
        json={
            "input": {
                "messages": [{"role": "human", "content": "certify streaming"}],
                "tokens": [],
                "fail_at_second_step": fail,
            },
            "config": {"configurable": {"thread_id": thread_id}},
            "stream_mode": "updates",
        },
        timeout=60,
        stream=True,
    )


def _stream_after_lock_release(*, thread_id: str) -> requests.Response:
    deadline = time.monotonic() + LOCK_RELEASE_DEADLINE_S
    while True:
        response = _stream(thread_id=thread_id)
        if response.status_code != 409 or time.monotonic() >= deadline:
            return response
        response.close()
        time.sleep(LOCK_POLL_INTERVAL_S)


@pytest.mark.skipif(not BASE_URL, reason=SKIP_REASON)
def test_first_data_frame_arrives_before_completion() -> None:
    # Given: a delayed deterministic graph on the streaming Azure app
    started = time.perf_counter()
    response = _stream(thread_id=f"timing-{uuid4()}")
    assert response.status_code == 200, response.text

    # When: SSE frames are timestamped as they arrive
    data_elapsed: float | None = None
    completed_elapsed: float | None = None
    for line in response.iter_lines(decode_unicode=True):
        elapsed = time.perf_counter() - started
        if line == "event: data" and data_elapsed is None:
            data_elapsed = elapsed
        if line == "event: end":
            completed_elapsed = elapsed

    # Then: the first frame precedes completion by at least one delayed step
    assert data_elapsed is not None
    assert completed_elapsed is not None
    assert data_elapsed < completed_elapsed - 0.2
    if EVIDENCE_PATH:
        Path(EVIDENCE_PATH).write_text(
            json.dumps(
                {
                    "first_data_ms": round(data_elapsed * 1000),
                    "completion_ms": round(completed_elapsed * 1000),
                    "incremental": True,
                },
                indent=2,
            )
        )


@pytest.mark.skipif(not BASE_URL, reason=SKIP_REASON)
def test_graph_failure_is_framed_in_band() -> None:
    # Given / When: the deterministic graph fails after its first frame
    response = _stream(thread_id=f"failure-{uuid4()}", fail=True)
    body = response.text

    # Then: the stream remains valid SSE and terminates after an error
    assert response.status_code == 200
    assert "event: data" in body
    assert "event: error" in body
    assert body.endswith("event: end\ndata: {}\n\n")


@pytest.mark.skipif(not BASE_URL, reason=SKIP_REASON)
def test_disconnect_releases_thread_lock() -> None:
    # Given: a checkpointed streaming request holding a per-thread lock
    thread_id = f"disconnect-{uuid4()}"
    response = _stream(thread_id=thread_id)
    iterator = response.iter_lines(decode_unicode=True)
    assert next(line for line in iterator if line) == "event: data"

    # When: the client disconnects before graph completion
    response.close()
    # The synchronous step that was in flight keeps the lock until it has finished (#621), so a
    # retry can meet lock contention for a moment after the disconnect: poll for the release.
    retry = _stream_after_lock_release(thread_id=thread_id)

    # Then: the same thread can run again instead of staying locked
    assert retry.status_code != 409, (
        f"thread {thread_id} was still locked (HTTP 409) {LOCK_RELEASE_DEADLINE_S:g} s "
        "after the client disconnected"
    )
    assert retry.status_code == 200, retry.text
    assert "event: end" in retry.text
