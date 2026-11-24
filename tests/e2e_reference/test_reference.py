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
    Runtime,
    StateResponse,
    accepted_indexes,
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
                        "observations": [item.model_dump() for item in self.observations],
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
    response = reference.request("GET", "/api/reference/runtime")
    assert response.status_code == 200, "runtime endpoint status differs"
    reference.runtime = Runtime.model_validate_json(response.content)
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
    assert reference.invoke(thread, "before").status_code == 200, "pre-restart invoke failed"
    reference.state(thread).check_turns(["before"])
    result = subprocess.run(
        ["az", "functionapp", "restart", "--resource-group", group, "--name", app],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, "Azure restart failed; output suppressed"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            ready = requests.get(reference.base + "/api/health", timeout=(5, 15))
            if ready.status_code == 200:
                break
        except requests.RequestException:
            time.sleep(2)
            continue
        time.sleep(2)
    else:
        pytest.fail("host did not recover within restart budget", pytrace=False)
    reference.state(thread).check_turns(["before"])
    assert reference.invoke(thread, "after").status_code == 200, "post-restart invoke failed"
    reference.state(thread).check_turns(["before", "after"])
    reference.record(Observation(tag="restart", count=2, seconds=time.monotonic() - start))
