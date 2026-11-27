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
    if example.endswith("basic"):
        pytest.importorskip(
            "langchain", reason="Basic baseline requires the example-only langchain package"
        )
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


@pytest.mark.parametrize(
    "failure",
    [
        "auth-bypass",
        "wrong-key",
        "lost-state",
        "leaked-state",
        "first-contents",
        "continuation-status",
        "isolated-status",
        "basic-contents",
        "basic-extra-field",
    ],
)
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
        if failure == "continuation-status" and text == "Continue":
            return 500, {}
        if failure == "isolated-status" and text == "Separate":
            return 500, {}
        contents = {
            "Hi": ["Hi", "observed 1 human turn(s)"],
            "Continue": ["Hi", "observed 1 human turn(s)", "Continue", "observed 2 human turn(s)"],
            "Separate": ["Separate", "observed 1 human turn(s)"],
        }[text]
        if failure == "lost-state" and text == "Continue":
            contents = ["Continue", "observed 1 human turn(s)"]
        if failure == "leaked-state" and text == "Separate":
            contents = ["Hi", *contents]
        if failure == "first-contents" and text == "Hi":
            contents = ["unexpected"]
        if failure == "basic-extra-field":
            contents = ["Hi", "Hello!"]
        return 200, {"output": {"messages": [{"content": c} for c in contents], "extra_field": 0}}

    monkeypatch.setattr(host, "request", request)
    # When a fake host violates authorization or checkpoint semantics.
    # Then the runner fails rather than producing certification evidence.
    with pytest.raises(RuntimeError):
        host.verify_calls(
            7571,
            "official_basic" if failure.startswith("basic-") else "official_conversation",
            "ephemeral-test-value",
        )


def test_loader_reports_an_unsupported_source(tmp_path: Path) -> None:
    # Given a path for which importlib cannot select a Python loader.
    proof = _proof()
    # When loading it, then certification reports a runtime failure.
    with pytest.raises(RuntimeError):
        proof.load_file(tmp_path / "source.unknown", "_unsupported_baseline")


@pytest.mark.parametrize("failure", ["status", "contents", "extra-field"])
def test_offline_checker_rejects_invalid_handler_output(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip(
        "langchain", reason="Basic baseline requires the example-only langchain package"
    )
    proof = _proof()
    original_app = proof.LangGraphApp

    class InvalidApp:
        def __init__(self) -> None:
            self.app = original_app()
            self.function_app = self

        def register(self, **kwargs: Any) -> None:
            self.app.register(**kwargs)

        def get_functions(self) -> list[Any]:
            functions: list[Any] = self.app.function_app.get_functions()
            for function in functions:
                if function.get_function_name() == "aflg_official_basic_invoke":
                    handler = function.get_user_function()

                    def invalid(request: Any) -> Any:
                        import json

                        import azure.functions as func

                        response = handler(request)
                        body = json.loads(response.get_body())
                        if failure == "contents":
                            body["output"]["messages"] = []
                        if failure == "extra-field":
                            body["output"]["extra_field"] = 0
                        return func.HttpResponse(
                            json.dumps(body), status_code=500 if failure == "status" else 200
                        )

                    monkeypatch.setattr(function, "get_user_function", lambda: invalid)
            return functions

    monkeypatch.setattr(proof, "LangGraphApp", InvalidApp)
    with pytest.raises(RuntimeError):
        proof.run("official_langgraph_basic")


def test_host_requires_installed_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        host.executable("func")


def test_host_resolves_executable_to_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _host()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(host.shutil, "which", lambda name: "bin/func")
    assert host.executable("func") == str(tmp_path / "bin/func")


def test_checker_rejects_source_tree_adapter() -> None:
    proof = _proof()
    with pytest.raises(RuntimeError):
        proof.require_installed_adapter(ROOT / "src/azure_functions_langgraph/__init__.py")


def test_checker_accepts_installed_adapter(tmp_path: Path) -> None:
    proof = _proof()
    proof.require_installed_adapter(
        tmp_path / "site-packages/azure_functions_langgraph/__init__.py"
    )


def test_checker_accepts_an_installed_adapter_below_the_checkout() -> None:
    proof = _proof()
    proof.require_installed_adapter(
        ROOT / "output/example/venv/lib/site-packages/azure_functions_langgraph/__init__.py"
    )


def test_host_runner_unwinds_on_termination_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _host()
    registered: dict[int, Any] = {}
    monkeypatch.setattr(host.signal, "signal", registered.__setitem__)
    host.install_termination_handlers()
    assert set(registered) == {host.signal.SIGTERM, host.signal.SIGHUP}
    with pytest.raises(KeyboardInterrupt):
        registered[host.signal.SIGTERM](host.signal.SIGTERM, None)


def test_offline_checker_rejects_shared_baseline_and_handler_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = _proof()
    original_load = proof.load_file

    def load(path: Path, name: str) -> Any:
        module = original_load(path, name)
        if path.name == "model.py":
            from langchain_core.messages import AIMessage

            monkeypatch.setattr(module.model, "invoke", lambda messages: AIMessage("incorrect"))
        return module

    monkeypatch.setattr(proof, "load_file", load)
    with pytest.raises(RuntimeError):
        proof.run("official_langgraph_conversation")


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
