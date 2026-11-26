"""Offline restart orchestration; HTTP and CLI calls are replaced before execution."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import pytest
import requests

from tests.e2e_reference import test_reference as harness


@pytest.mark.parametrize("replacement", [False, True])
def test_restart_requires_turnover_after_old_worker_200(
    monkeypatch: pytest.MonkeyPatch, replacement: bool, tmp_path: Path
) -> None:
    # Given an old worker that keeps serving successful HTTP responses after restart.
    for name in ("RESOURCE_GROUP", "APP", "FUNCTION_KEY"):
        monkeypatch.setenv("REF882_" + name, "synthetic")
    monkeypatch.setenv("REF882_BASE_URL", "https://reference.invalid")
    evidence = tmp_path / "evidence.json"
    monkeypatch.setenv("REF882_EVIDENCE", str(evidence))
    calls: list[str] = []
    ticks = iter(range(0, 1000, 20))
    monkeypatch.setattr(time, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    def cli(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == [
            "az",
            "functionapp",
            "restart",
            "--resource-group",
            "synthetic",
            "--name",
            "synthetic",
        ]
        assert kwargs["capture_output"] is True
        calls.append("restart")
        return subprocess.CompletedProcess([], 0)

    def http(method: str, url: str, **kwargs: object) -> requests.Response:
        if "restart" not in calls or (method == "GET" and calls.count("POST") < 2):
            assert "/api/reference/runtime?restart_thread=restart-" in url
            assert kwargs["headers"] == {"x-functions-key": "synthetic"}
        calls.append(method)
        token = "a" * 32
        if replacement and calls.count("GET") == 2:
            raise requests.ConnectionError("synthetic transport details")
        if replacement and calls.count("GET") >= 4:
            token = "b" * 32
        messages = [
            {"type": "human", "content": "before"},
            {"type": "ai", "content": "[fake model] observed 1 human turn(s)"},
        ]
        if calls.count("POST") >= 2:
            messages.extend(
                [
                    {"type": "human", "content": "after"},
                    {"type": "ai", "content": "[fake model] observed 2 human turn(s)"},
                ]
            )
        response = requests.Response()
        response.status_code = 200
        if replacement and calls.count("GET") == 3:
            response.status_code = 503
        response._content = json.dumps(
            {"process_token": token, "values": {"messages": messages}}
        ).encode()
        return response

    monkeypatch.setattr(subprocess, "run", cli)
    monkeypatch.setattr(requests, "request", http)
    monkeypatch.setattr(requests, "get", lambda *a, **k: http("GET", ""))
    monkeypatch.setattr(requests, "post", lambda *a, **k: http("POST", ""))
    target = harness.Reference()
    # When exercising the actual disruptive-test orchestration through fakes.
    if replacement:
        harness.test_persistence_across_restart(target)
        # Then it waits past the old worker and records only verified boolean evidence.
        assert calls.count("GET") >= 4
        assert target.observations[-1].model_dump()["turnover_confirmed"] is True
    else:
        with pytest.raises(pytest.fail.Exception, match="turnover unverified"):
            harness.test_persistence_across_restart(target)
        assert target.observations[-1].model_dump()["turnover_confirmed"] is False
    saved = evidence.read_text()
    assert "a" * 32 not in saved and "b" * 32 not in saved
    assert "process_token" not in saved
    observed = json.loads(saved)["observations"][-1]
    assert observed.pop("seconds") >= 0
    assert observed == {
        "tag": "restart",
        "count": 2 if replacement else 0,
        "statuses": [],
        "restart_accepted": True,
        "http_available": True,
        "turnover_confirmed": replacement,
    }


def test_restart_skips_without_expendable_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REF882_BASE_URL", "https://reference.invalid")
    monkeypatch.setenv("REF882_FUNCTION_KEY", "synthetic")
    monkeypatch.delenv("REF882_RESOURCE_GROUP", raising=False)
    monkeypatch.delenv("REF882_APP", raising=False)
    with pytest.raises(pytest.skip.Exception):
        harness.test_persistence_across_restart(harness.Reference())


def test_restart_rejects_failed_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("RESOURCE_GROUP", "APP", "FUNCTION_KEY"):
        monkeypatch.setenv("REF882_" + name, "synthetic")
    monkeypatch.setenv("REF882_BASE_URL", "https://reference.invalid")

    def written(*args: object, **kwargs: object) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {
                "process_token": "a" * 32,
                "values": {
                    "messages": [
                        {"type": "human", "content": "before"},
                        {"type": "ai", "content": "[fake model] observed 1 human turn(s)"},
                    ]
                },
            }
        ).encode()
        return response

    monkeypatch.setattr(requests, "request", written)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stderr=b"private")
    )
    with pytest.raises(
        pytest.fail.Exception, match="Azure restart failed; output suppressed"
    ) as error:
        harness.test_persistence_across_restart(harness.Reference())
    assert "private" not in str(error.value)
