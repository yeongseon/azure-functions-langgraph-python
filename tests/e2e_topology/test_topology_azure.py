"""One deployed payload: safe, three indexing refusals, recovery, hosted warnings.

Only the opt-in workflow changes settings. This module only makes HTTP requests.
An unavailable target fails the selected lane through check_junit, never certifies it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

import pytest
import requests

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_topology]
PROBES = (
    ("GET", "/api/health"),
    ("GET", "/api/topology/evidence"),
    ("POST", "/api/graphs/agent/invoke"),
    ("POST", "/api/threads"),
)


def test_deployed_topology_phase() -> None:
    base = os.environ.get("TOPOLOGY_BASE_URL", "").rstrip("/")
    if not base:
        pytest.skip("TOPOLOGY_BASE_URL is required for the opt-in Azure topology lane")
    phase = os.environ["TOPOLOGY_PHASE"]
    started = time.monotonic()
    changed_at = float(os.environ["TOPOLOGY_CHANGED_AT"])
    observations: dict[str, list[int | str]] = {path: [] for _, path in PROBES}
    evidence_path = Path("cert-topology") / f"{phase}.json"
    with requests.Session() as client:
        client.headers["x-functions-key"] = os.environ["TOPOLOGY_CODE"]

        def probe(method: str, path: str) -> requests.Response | None:
            try:
                response = client.request(
                    method,
                    base + path,
                    json={"input": {"message": "topology-smoke"}},
                    timeout=15,
                    allow_redirects=False,
                )
            except requests.RequestException:
                observations[path].append("transport-error")
                return None
            observations[path].append(response.status_code)
            return response

        try:
            if phase in {"lock", "store", "checkpointer"}:
                # The workflow has completed an explicit restart and 60 s settling
                # period. No successful response is tolerated in this 90 s window.
                deadline = started + 90
                while time.monotonic() < deadline:
                    for method, path in PROBES:
                        response = probe(method, path)
                        if response is not None:
                            assert response.status_code in {404, 503}, (
                                phase,
                                path,
                                response.status_code,
                            )
                    time.sleep(3)
                for path, statuses in observations.items():
                    assert any(status in {404, 503} for status in statuses), (path, statuses)
            else:
                expected_mode = "warnings" if phase == "warnings" else "safe"
                deadline = started + 240
                body = None
                while time.monotonic() < deadline:
                    response = probe("GET", "/api/topology/evidence")
                    if response is not None and response.status_code == 200:
                        candidate = response.json()
                        if candidate["mode"] == expected_mode:
                            body = candidate
                            break
                    time.sleep(5)
                assert body is not None, f"{phase}: expected indexed app never served"
                assert body["candidate_sha"] == os.environ["TOPOLOGY_CANDIDATE_SHA"]
                assert body["env"]["azure_hosted"] is True
                assert any(body["host_markers"].values())
                assert body["python"].startswith("3.12.")
                assert body["azure_functions"].startswith("1.")
                expected_warnings = (
                    {
                        "thread-store-hosted",
                        "process-local-checkpointer-hosted",
                        "inprocess-lock-hosted",
                    }
                    if phase == "warnings"
                    else set()
                )
                assert {f["rule"] for f in body["findings"]} == expected_warnings
                assert all(f["severity"] == "warning" for f in body["findings"])
                health = probe("GET", "/api/health")
                assert health is not None and health.status_code == 200
                assert health.json() == {"status": "ok"}
                response = client.post(
                    base + "/api/graphs/agent/invoke",
                    json={
                        "input": {"message": "topology-smoke"},
                        "config": {"configurable": {"thread_id": uuid.uuid4().hex}},
                    },
                    timeout=60,
                )
                assert response.status_code == 200, response.status_code
                assert response.json()["output"]["message"] == "topology-smoke"
                evidence_path.with_name(f"{phase}-plan.json").write_text(json.dumps(body, indent=2))
        finally:
            evidence = {
                "phase": phase,
                "statuses": observations,
                "probe_seconds": round(time.monotonic() - started, 1),
                "seconds_since_settings_change": round(time.time() - changed_at, 1),
                "settling_seconds_before_refusal_probe": 60,
                "scope": "one Linux Y1 app; markers do not prove instance count or fencing",
            }
            evidence_path.write_text(json.dumps(evidence, indent=2))
            print(json.dumps(evidence))
