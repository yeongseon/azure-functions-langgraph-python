"""Real-Azure Durable async-run e2e: lifecycle, contention, cancel, host restart (#811).

OPTIONAL lane. It is NOT part of the release-gating ``tests/e2e`` suite and is never selected by
``pytest tests/e2e``. It needs a deployed ``examples/e2e_durable_app`` and is driven by
``.github/workflows/e2e-azure-durable.yml``::

    E2E_DURABLE_BASE_URL=https://<app>.azurewebsites.net \\
        pytest tests/e2e_durable -m e2e_durable -o addopts=""

Skips cleanly when ``E2E_DURABLE_BASE_URL`` is unset. The restart test additionally needs
``E2E_DURABLE_RESTART_CMD`` (a command that restarts the host, e.g.
``az functionapp restart --resource-group RG --name APP``) and skips without it.

What these tests assert is only what the package promises: normalized lifecycle statuses, lock
contention rejection, cancellation reaching ``cancelled``, a terminal outcome after a host
restart, and that the idempotency key suppresses duplicate effects. They deliberately do NOT
assert whether a restart redelivers the Activity or whether terminate aborts it: those are
RECORDED (number of attempts, when the Activity actually finished) into the evidence file named
by ``E2E_DURABLE_EVIDENCE`` and reported, not assumed.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from typing import Any
import uuid

import pytest
import requests

BASE_URL = os.environ.get("E2E_DURABLE_BASE_URL", "").rstrip("/")
RESTART_CMD = os.environ.get("E2E_DURABLE_RESTART_CMD", "")
EVIDENCE_PATH = os.environ.get("E2E_DURABLE_EVIDENCE", "")
GRAPH = "e2e_durable"
TERMINAL = {"success", "error", "cancelled"}

pytestmark = [
    pytest.mark.e2e,  # keeps the default `-m 'not e2e'` unit run from selecting it
    pytest.mark.e2e_durable,
    pytest.mark.skipif(
        not BASE_URL, reason="E2E_DURABLE_BASE_URL not set; skipping Durable real-Azure e2e"
    ),
]

_evidence: dict[str, Any] = {}


def _record(name: str, data: dict[str, Any]) -> None:
    """Keep sanitized observations (statuses, counts, short tags only); no URLs, keys or bodies."""
    _evidence[name] = data
    if EVIDENCE_PATH:
        with open(EVIDENCE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_evidence, fh, indent=2, sort_keys=True)


def _create(
    run_id: str, *, hold: float = 0, thread_id: str | None = None, fail: bool = False
) -> None:
    body: dict[str, Any] = {
        "run_id": run_id,
        "input": {"run_id": run_id, "hold_seconds": hold, "fail": fail},
    }
    if thread_id:
        body["thread_id"] = thread_id
    r = requests.post(f"{BASE_URL}/api/graphs/{GRAPH}/runs", json=body, timeout=60)
    assert r.status_code == 202, r.text
    assert r.json() == {"run_id": run_id, "status": "pending"}


def _get(run_id: str) -> dict[str, Any]:
    r = requests.get(f"{BASE_URL}/api/runs/{run_id}", timeout=60)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _evidence_for(run_id: str) -> dict[str, Any]:
    r = requests.get(f"{BASE_URL}/api/e2e/evidence/{run_id}", timeout=60)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _wait_terminal(run_id: str, timeout: float, seen: list[str] | None = None) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            last = _get(run_id)
        except (requests.RequestException, AssertionError):
            time.sleep(5)  # the host may be restarting
            continue
        if seen is not None and (not seen or seen[-1] != last["status"]):
            seen.append(last["status"])
        if last["status"] in TERMINAL:
            return last
        time.sleep(2)
    raise AssertionError(
        f"run {run_id} not terminal within {timeout}s (last status {last.get('status')})"
    )


def _wait_entered(run_id: str, timeout: float = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _evidence_for(run_id)["attempts"]:
                return
        except (requests.RequestException, AssertionError):
            pass
        time.sleep(2)
    raise AssertionError(f"Activity for {run_id} never started within {timeout}s")


@pytest.fixture(scope="module", autouse=True)
def warm_host() -> None:
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE_URL}/api/e2e/health", timeout=15).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(5)
    else:
        raise AssertionError("Durable e2e host never became healthy")
    runtime = requests.get(f"{BASE_URL}/api/e2e/runtime", timeout=30).json()
    _record("runtime", runtime)


def test_create_poll_success_with_idempotent_effect() -> None:
    run_id = f"d-ok-{uuid.uuid4().hex[:12]}"
    seen: list[str] = []
    _create(run_id, hold=3, thread_id=f"t-{run_id}")
    final = _wait_terminal(run_id, 300, seen)
    assert final["status"] == "success", final
    assert final["output"]["activity_status"] == "success"
    assert final["output"]["result"]["completed"] == run_id
    ev = _evidence_for(run_id)
    assert len(ev["attempts"]) == 1 and ev["effects_applied"] == 1
    _record("success", {"statuses": seen, "attempts": len(ev["attempts"])})


def test_failure_is_reported_as_error() -> None:
    run_id = f"d-fail-{uuid.uuid4().hex[:12]}"
    _create(run_id, fail=True)
    final = _wait_terminal(run_id, 300)
    assert final["status"] == "error", final
    assert final["output"]["activity_status"] == "error"
    assert final["output"]["error"]["type"] == "RuntimeError"


def test_same_thread_contention_rejects_second_run() -> None:
    thread = f"t-contend-{uuid.uuid4().hex[:12]}"
    first, second = f"d-c1-{uuid.uuid4().hex[:10]}", f"d-c2-{uuid.uuid4().hex[:10]}"
    _create(first, hold=45, thread_id=thread)
    _wait_entered(first)  # the first run is inside the locked section
    _create(second, hold=1, thread_id=thread)
    rejected = _wait_terminal(second, 300)
    assert rejected["status"] == "error", rejected
    assert rejected["output"]["activity_status"] == "rejected"
    assert _wait_terminal(first, 300)["status"] == "success"
    ev2 = _evidence_for(second)
    assert ev2["attempts"] == [] and ev2["effects_applied"] == 0, ev2
    _record("contention", {"second_activity_status": "rejected"})


def test_cancel_during_work_records_when_activity_finishes() -> None:
    run_id = f"d-cancel-{uuid.uuid4().hex[:12]}"
    hold = 60
    _create(run_id, hold=hold)
    _wait_entered(run_id)
    t0 = time.time()
    r = requests.post(f"{BASE_URL}/api/runs/{run_id}/cancel", timeout=60)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "cancel_requested"
    final = _wait_terminal(run_id, 120)
    assert final["status"] == "cancelled", final
    cancelled_after = time.time() - t0
    # Observe, do not assume: does the already-running Activity still finish after terminate?
    deadline = time.time() + hold + 90
    completions = 0
    while time.time() < deadline:
        completions = len(_evidence_for(run_id)["completions"])
        if completions:
            break
        time.sleep(5)
    ev = _evidence_for(run_id)
    _record(
        "cancel",
        {
            "cancelled_after_seconds": round(cancelled_after, 1),
            "activity_completed_after_terminate": bool(completions),
            "attempts": len(ev["attempts"]),
            "effects_applied": ev["effects_applied"],
        },
    )


@pytest.mark.skipif(
    not RESTART_CMD, reason="E2E_DURABLE_RESTART_CMD not set; skipping host restart"
)
def test_host_restart_during_activity() -> None:
    run_id = f"d-restart-{uuid.uuid4().hex[:12]}"
    thread = f"t-{run_id}"
    seen: list[str] = []
    _create(run_id, hold=90, thread_id=thread)
    _wait_entered(run_id)
    before = _get(run_id)["status"]
    subprocess.run(shlex.split(RESTART_CMD), check=True, timeout=300)  # noqa: S603
    final = _wait_terminal(run_id, 900, seen)
    ev = _evidence_for(run_id)
    _record(
        "host_restart",
        {
            "status_before_restart": before,
            "statuses_after_restart": seen,
            "final_status": final["status"],
            "final_activity_status": (final.get("output") or {}).get("activity_status"),
            "activity_attempts": len(ev["attempts"]),
            "attempt_instance_tags": sorted(set(ev["attempts"])),
            "activity_completions": len(ev["completions"]),
            "effect_calls": len(ev["effect_calls"]),
            "effects_applied": ev["effects_applied"],
        },
    )
    # Package-level promises only: a terminal normalized status, and the idempotency key holding.
    assert final["status"] in TERMINAL
    assert ev["effects_applied"] <= 1, "idempotency key failed to suppress a duplicate effect"
