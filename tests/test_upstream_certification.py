from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from tests.example_imports import load_example_file

ROOT = Path(__file__).resolve().parents[1]


def _proof() -> Any:
    return load_example_file(
        ROOT / "scripts/certify_upstream_examples.py", "certify_upstream_examples"
    )


def _host() -> Any:
    _proof()
    return load_example_file(ROOT / "scripts/certify_upstream_host.py", "_upstream_host")


@pytest.mark.parametrize("example", ["official_langgraph_basic", "official_langgraph_conversation"])
def test_offline_checker_executes_baseline_and_registered_handler(example: str) -> None:
    # Given the installed adapter or source-tree test tier and real deterministic graphs.
    proof = _proof()
    # When checking without subprocesses, storage or network.
    result = proof.run(example)
    # Then real handler outputs match independent pristine graph execution.
    assert result["versions"]["azure-functions-langgraph"]
    assert len(result["baseline_and_handler_contents"]) == (1 if example.endswith("basic") else 3)


@pytest.mark.parametrize("name", ["official_basic", "official_conversation"])
def test_host_probe_checks_key_rejection_continuity_and_isolation(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a wire-level fake with deterministic native responses.
    host = _host()
    calls: list[tuple[str, str, str]] = []

    def request(
        port: int, path: str, payload: dict[str, Any], key: str = ""
    ) -> tuple[int, dict[str, Any]]:
        thread = payload["config"]["configurable"]["thread_id"]
        text = payload["input"]["messages"][0]["content"]
        calls.append((thread, text, key))
        if not key:
            return 401, {}
        contents = (
            [text, "Hello!"]
            if name == "official_basic"
            else {
                "Hi": ["Hi", "observed 1 human turn(s)"],
                "Continue": [
                    "Hi",
                    "observed 1 human turn(s)",
                    "Continue",
                    "observed 2 human turn(s)",
                ],
                "Separate": ["Separate", "observed 1 human turn(s)"],
            }[text]
        )
        return 200, {"output": {"messages": [{"content": c} for c in contents], "extra_field": 10}}

    monkeypatch.setattr(host, "request", request)
    # When executing the same probe used by the real host runner.
    result = host.verify_calls(7571, name, "ephemeral-test-value")
    # Then keyless rejection precedes authorized calls; stateful calls use two distinct threads.
    assert result["keyless_status"] == 401
    assert calls[:2] == [("a", "Hi", ""), ("a", "Hi", "ephemeral-test-value")]
    assert len(calls) == (2 if name == "official_basic" else 4)
    assert "ephemeral-test-value" not in str(result)


@pytest.mark.parametrize("failure", ["auth-bypass", "wrong-key", "lost-state", "leaked-state"])
def test_host_probe_rejects_false_certification(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given failures at each certification boundary.
    host = _host()

    def request(
        port: int, path: str, payload: dict[str, Any], key: str = ""
    ) -> tuple[int, dict[str, Any]]:
        text = payload["input"]["messages"][0]["content"]
        if not key:
            return (200 if failure == "auth-bypass" else 401), {}
        if failure == "wrong-key":
            return 401, {}
        contents = {
            "Hi": ["Hi", "observed 1 human turn(s)"],
            "Continue": ["Hi", "observed 1 human turn(s)", "Continue", "observed 2 human turn(s)"],
            "Separate": ["Separate", "observed 1 human turn(s)"],
        }[text]
        if failure == "lost-state" and text == "Continue":
            contents = ["Continue", "observed 1 human turn(s)"]
        if failure == "leaked-state" and text == "Separate":
            contents = ["Hi", *contents]
        return 200, {"output": {"messages": [{"content": c} for c in contents]}}

    monkeypatch.setattr(host, "request", request)
    # When a fake host violates authorization or checkpoint semantics.
    # Then the runner fails rather than producing certification evidence.
    with pytest.raises(AssertionError):
        host.verify_calls(7571, "official_conversation", "ephemeral-test-value")


def test_published_wheel_local_host_opt_in(tmp_path: Path) -> None:
    # Given an offline unit run by default; PyPI and real processes need explicit permission.
    if os.environ.get("RUN_UPSTREAM_WHEEL_CERTIFICATION") != "1":
        pytest.skip(
            "opt-in: requires PyPI, fresh venvs, func and Azurite; "
            "set RUN_UPSTREAM_WHEEL_CERTIFICATION=1"
        )
    # When explicitly enabled, run the complete checker as an external command.
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/certify_primary_examples.py"),
            "published",
            "--examples",
            "official_langgraph_basic",
            "official_langgraph_conversation",
            "--local-host",
            "--output-dir",
            str(tmp_path),
        ],
        check=True,
        timeout=300,
    )
    # Then both sanitized host records exist.
    assert all(
        (tmp_path / example / "local-host.json").exists()
        for example in (
            "official_langgraph_basic",
            "official_langgraph_conversation",
        )
    )
