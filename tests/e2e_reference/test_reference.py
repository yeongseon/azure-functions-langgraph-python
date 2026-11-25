"""Opt-in real Azure native reference; never collected by the release tests/e2e path."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
from threading import Barrier
import time
import uuid

import pytest
import requests

from ._contracts import (
    Conversation,
    InvokeResponse,
    Observation,
    RestartResponse,
    Runtime,
    RuntimeResponse,
    StateResponse,
    accepted_indexes,
    check_runtime_access,
)

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_reference]
GRAPH = "production_persistent_agent"


class Reference:
    """Accumulates only typed evidence; credentials and response bodies are never recorded."""

    def __init__(self) -> None:
        self.base = os.environ["REF882_BASE_URL"].rstrip("/")
        self.key = os.environ["REF882_FUNCTION_KEY"]
        self.observations: list[Observation] = []
        self.runtime: Runtime | None = None

    def request(self, method: str, path: str) -> requests.Response:
        try:
            return requests.request(
                method,
                self.base + path,
                headers={"x-functions-key": self.key},
                timeout=(10, 120),
                allow_redirects=False,
            )
        except requests.RequestException:
            pytest.fail("reference HTTP transport failed; details suppressed", pytrace=False)

    def invoke(self, thread: str, turn: str) -> requests.Response:
        try:
            return requests.post(
                f"{self.base}/api/graphs/{GRAPH}/invoke",
                headers={"x-functions-key": self.key},
                json={
                    "input": {"messages": [{"role": "human", "content": turn}]},
                    "config": {"configurable": {"thread_id": thread}},
                },
                timeout=(10, 120),
                allow_redirects=False,
            )
        except requests.RequestException:
            pytest.fail("reference invoke transport failed; details suppressed", pytrace=False)

    def state(self, thread: str) -> Conversation:
        response = self.request("GET", f"/api/graphs/{GRAPH}/threads/{thread}/state")
        assert response.status_code == 200, "state status differs"
        return StateResponse.model_validate_json(response.content).values

    def record(self, observation: Observation) -> None:
        self.observations.append(observation)
        self.save()

    def save(self) -> None:
        destination = os.environ.get("REF882_EVIDENCE")
        if destination:
            Path(destination).write_text(
                json.dumps(
                    {
                        "observations": [
                            item.model_dump(exclude_none=True) for item in self.observations
                        ],
                        "runtime": self.runtime.model_dump() if self.runtime else None,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )


@pytest.fixture(scope="module")
def reference() -> Iterator[Reference]:
    if not os.environ.get("REF882_BASE_URL") or not os.environ.get("REF882_FUNCTION_KEY"):
        pytest.skip("REF882_BASE_URL and REF882_FUNCTION_KEY required")
    target = Reference()
    yield target
    target.save()


def test_liveness_and_function_key(reference: Reference) -> None:
    start = time.monotonic()
    try:
        health = requests.get(reference.base + "/api/health", timeout=(10, 120))
        keyless = requests.post(
            f"{reference.base}/api/graphs/{GRAPH}/invoke", json={"input": {}}, timeout=(10, 120)
        )
    except requests.RequestException:
        pytest.fail("anonymous HTTP transport failed; details suppressed", pytrace=False)
    keyed = reference.invoke("auth-" + uuid.uuid4().hex, "auth")
    statuses = (health.status_code, keyless.status_code, keyed.status_code)
    reference.record(Observation(tag="auth", statuses=statuses, seconds=time.monotonic() - start))
    assert statuses == (200, 401, 200), "authentication status differs"
    assert health.json() == {"status": "ok"}, "liveness payload differs"
    InvokeResponse.model_validate_json(keyed.content).output.check_turns(["auth"])


def test_deployed_runtime(reference: Reference) -> None:
    start = time.monotonic()
    try:
        keyless = requests.get(
            reference.base + "/api/reference/runtime", timeout=(10, 120), allow_redirects=False
        )
    except requests.RequestException:
        pytest.fail("anonymous runtime transport failed; details suppressed", pytrace=False)
    response = reference.request("GET", "/api/reference/runtime")
    statuses = (keyless.status_code, response.status_code)
    reference.record(
        Observation(tag="runtime_auth", statuses=statuses, seconds=time.monotonic() - start)
    )
    check_runtime_access(statuses)
    deployed = RuntimeResponse.model_validate_json(response.content)
    reference.runtime = Runtime.model_validate(deployed.model_dump())
    reference.save()
    assert reference.runtime.adapter == "0.14.2", "adapter is not the selected published wheel"
    assert reference.runtime.python.startswith("3.11."), "unexpected deployed Python"
    assert reference.runtime.functions.startswith("1."), "unexpected Functions library line"


def test_persistence_and_isolation(reference: Reference) -> None:
    start = time.monotonic()
    thread = "memory-" + uuid.uuid4().hex
    other = "other-" + uuid.uuid4().hex
    for turn in ("first", "second"):
        assert reference.invoke(thread, turn).status_code == 200, "invoke failed"
    reference.state(thread).check_turns(["first", "second"])
    reference.state(other).check_turns([])
    assert reference.invoke(other, "isolated").status_code == 200, "isolated invoke failed"
    reference.state(other).check_turns(["isolated"])
    reference.state(thread).check_turns(["first", "second"])
    reference.record(
        Observation(tag="persistence_isolation", count=3, seconds=time.monotonic() - start)
    )


def test_parallel_same_thread(reference: Reference) -> None:
    start = time.monotonic()
    thread = "parallel-" + uuid.uuid4().hex
    barrier = Barrier(4)
    turns = [f"turn-{index}" for index in range(4)]

    def submit(turn: str) -> int:
        barrier.wait(timeout=30)
        return reference.invoke(thread, turn).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = tuple(pool.map(submit, turns))
    reference.record(
        Observation(tag="contention", statuses=statuses, seconds=time.monotonic() - start)
    )
    accepted = accepted_indexes(statuses)
    reference.state(thread).check_turns([turns[index] for index in accepted])
    assert reference.invoke(thread, "after").status_code == 200, "lock not released"
    reference.state(thread).check_turns([*[turns[index] for index in accepted], "after"])


def test_persistence_across_restart(reference: Reference) -> None:
    group, app = os.environ.get("REF882_RESOURCE_GROUP"), os.environ.get("REF882_APP")
    if not group or not app:
        pytest.skip("REF882_RESOURCE_GROUP and REF882_APP required for restart")
    start = time.monotonic()
    thread = "restart-" + uuid.uuid4().hex
    probe = "/api/reference/runtime?restart_thread=" + thread
    written = reference.request("POST", probe)
    assert written.status_code == 200, "pre-restart write status differs"
    before = RestartResponse.model_validate_json(written.content)
    before.values.check_turns(["before"])
    result = subprocess.run(
        ["az", "functionapp", "restart", "--resource-group", group, "--name", app],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("Azure restart failed; output suppressed", pytrace=False)
    deadline = time.monotonic() + 180
    http_available = False
    while time.monotonic() < deadline:
        try:
            ready = requests.request(
                "GET",
                reference.base + probe,
                headers={"x-functions-key": reference.key},
                timeout=(5, 15),
                allow_redirects=False,
            )
            if ready.status_code == 200:
                http_available = True
                recovered = RestartResponse.model_validate_json(ready.content)
                if recovered.process_token != before.process_token:
                    recovered.values.check_turns(["before"])
                    break
        except requests.RequestException:
            time.sleep(2)
            continue
        time.sleep(2)
    else:
        reference.record(
            Observation(
                tag="restart",
                seconds=time.monotonic() - start,
                restart_accepted=True,
                http_available=http_available,
                turnover_confirmed=False,
            )
        )
        pytest.fail("process turnover unverified within restart budget", pytrace=False)
    assert reference.invoke(thread, "after").status_code == 200, "post-restart invoke failed"
    reference.state(thread).check_turns(["before", "after"])
    reference.record(
        Observation(
            tag="restart",
            count=2,
            seconds=time.monotonic() - start,
            restart_accepted=True,
            http_available=True,
            turnover_confirmed=True,
        )
    )
