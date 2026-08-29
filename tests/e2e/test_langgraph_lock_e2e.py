"""Two-app single-writer exclusivity e2e (#386).

Proves the core distributed-lock guarantee of ``AzureBlobLeaseThreadLock`` +
a single-writer checkpointer across TWO independent Azure Function Apps: when
both hosts invoke the same ``thread_id`` concurrently, exactly one execution
mutates the checkpoint and the other is rejected with HTTP 409.

The race is made **deterministic** (not a flaky "fire both simultaneously"
gamble) by gating on observed state:

1. Start App A's invoke asynchronously (it enters the mutation section, writes
   an ``entered`` marker, then holds the lease for ``E2E_LOCK_HOLD_SECONDS``).
2. Poll the shared checkpoint via App A's state endpoint until ``entered``
   equals App A's run id — proving A is inside the locked section and still
   holding the lease.
3. Only then fire App B with the same ``thread_id``; assert B gets 409.
4. Let App A finish (200) and assert the final checkpoint reflects exactly one
   run id (App A's) — App B never wrote anything.

Requires BOTH ``E2E_BASE_URL`` (App A) and ``E2E_BASE_URL_B`` (App B). Skips
otherwise, so ordinary unit runs (``-m "not e2e"``) and single-app local e2e
runs never hit this test.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import os
import time
import uuid

import pytest
import requests

BASE_URL_A = os.environ.get("E2E_BASE_URL", "").rstrip("/")
BASE_URL_B = os.environ.get("E2E_BASE_URL_B", "").rstrip("/")
GRAPH = "e2e_lock_agent"
SKIP_REASON = "E2E_BASE_URL and E2E_BASE_URL_B must both be set — skipping two-app exclusivity e2e"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (BASE_URL_A and BASE_URL_B), reason=SKIP_REASON),
]


def _invoke(base_url: str, thread_id: str, run_id: str) -> requests.Response:
    payload = {
        "input": {"messages": [], "run_id": run_id},
        "config": {"configurable": {"thread_id": thread_id}},
    }
    return requests.post(
        f"{base_url}/api/graphs/{GRAPH}/invoke",
        json=payload,
        timeout=120,
    )


def _get_state_values(base_url: str, thread_id: str) -> dict[str, object]:
    r = requests.get(
        f"{base_url}/api/graphs/{GRAPH}/threads/{thread_id}/state",
        timeout=30,
    )
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    body = r.json()
    values = body.get("values")
    return values if isinstance(values, dict) else {}


def _wait_for_entered(base_url: str, thread_id: str, run_id: str, timeout: float) -> None:
    """Poll the shared checkpoint until ``entered`` == ``run_id`` (bounded)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        values = _get_state_values(base_url, thread_id)
        if values.get("entered") == run_id:
            return
        time.sleep(1)
    raise AssertionError(
        f"App A never recorded entered={run_id!r} within {timeout}s "
        f"(last state values: {_get_state_values(base_url, thread_id)})"
    )


@pytest.fixture(scope="module", autouse=True)
def warmup_both_apps() -> None:
    """Retry /api/health on both hosts until Consumption cold-start finishes."""
    for base_url in (BASE_URL_A, BASE_URL_B):
        deadline = time.time() + 180
        last_exc: Exception | None = None
        while time.time() < deadline:
            try:
                r = requests.get(f"{base_url}/api/health", timeout=15)
                if r.status_code == 200:
                    break
            except requests.RequestException as exc:  # pragma: no cover - network
                last_exc = exc
            time.sleep(5)
        else:
            raise AssertionError(f"{base_url} never became healthy: {last_exc}")


def test_single_writer_exclusivity_across_two_apps() -> None:
    thread_id = f"e2e-lock-{uuid.uuid4().hex[:12]}"
    run_id_a = f"A-{uuid.uuid4().hex}"
    run_id_b = f"B-{uuid.uuid4().hex}"

    with ThreadPoolExecutor(max_workers=1) as pool:
        # 1. App A enters and holds the lease.
        future_a: Future[requests.Response] = pool.submit(_invoke, BASE_URL_A, thread_id, run_id_a)
        try:
            # 2. Wait until A is provably inside the locked section.
            _wait_for_entered(BASE_URL_A, thread_id, run_id_a, timeout=90)

            # 3. Now fire App B for the SAME thread — the lease is still held.
            resp_b = _invoke(BASE_URL_B, thread_id, run_id_b)
            assert resp_b.status_code == 409, (
                f"App B expected 409 lock-conflict, got {resp_b.status_code}: {resp_b.text}"
            )
            assert "in use" in resp_b.text.lower(), resp_b.text

            # 4. App A completes successfully.
            resp_a = future_a.result(timeout=120)
        finally:
            # Never leak the worker if an assertion fails mid-flight.
            future_a.cancel()

    assert resp_a.status_code == 200, f"App A expected 200, got {resp_a.status_code}: {resp_a.text}"

    # 5. Final checkpoint reflects exactly one run id — App A's. App B never wrote.
    final = _get_state_values(BASE_URL_A, thread_id)
    assert final.get("entered") == run_id_a, final
    assert final.get("completed") == run_id_a, final
    assert final.get("run_id") == run_id_a, final
    assert run_id_b not in final.values(), f"App B's run id leaked into checkpoint: {final}"
