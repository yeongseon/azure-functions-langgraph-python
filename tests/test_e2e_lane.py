"""Regression tests for the ``-m e2e`` side of the real-Azure e2e lane (#686).

An e2e module skips itself when its ``E2E_*`` input is unset, so a run that was never pointed at a
deployment used to end with every test skipped and exit status 0. Under the dedicated ``-m e2e``
selector ``tests/e2e/conftest.py`` fails that run instead. The streaming function key must also
travel in a header, so that it never lands in a URL.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "tests" / "e2e"
INPUTS = ("E2E_BASE_URL", "E2E_BASE_URL_B", "E2E_STREAM_BASE_URL", "E2E_STREAM_CODE")
MODULES = ["test_langgraph_e2e", "test_langgraph_lock_e2e", "test_true_streaming_e2e"]
ALL_SET = dict.fromkeys(INPUTS[:3], "https://example.test")


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lane = _load("e2e_lane_conftest", E2E / "conftest.py")


def test_every_e2e_module_is_listed_with_inputs_it_reads() -> None:
    assert set(lane.REQUIRED_INPUTS) == {path.stem for path in E2E.glob("test_*.py")}
    for module, inputs in lane.REQUIRED_INPUTS.items():
        source = (E2E / f"{module}.py").read_text()
        assert all(f'"{name}"' in source for name in inputs)


def test_nothing_is_unmet_when_every_input_is_set() -> None:
    # The function key is optional: a local Functions host takes none, and Azure answers 401.
    assert lane.unmet_inputs(MODULES, ALL_SET) == []


@pytest.mark.parametrize("blank", ["", "/", "//"])
def test_an_input_the_modules_would_read_as_empty_counts_as_unset(blank: str) -> None:
    unmet = lane.unmet_inputs(MODULES, {**ALL_SET, "E2E_BASE_URL_B": blank})

    assert unmet == ["test_langgraph_lock_e2e: E2E_BASE_URL_B"]


def test_a_module_the_guard_does_not_know_fails_closed() -> None:
    assert lane.unmet_inputs(["test_new_e2e"], ALL_SET) == [
        "test_new_e2e: not listed in REQUIRED_INPUTS in tests/e2e/conftest.py"
    ]


def _pytest(*arguments: str, **inputs: str) -> subprocess.CompletedProcess[str]:
    """Run pytest in the repository with every E2E_* input unset except ``inputs``."""
    environment = {key: value for key, value in os.environ.items() if key not in INPUTS}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        cwd=ROOT,
        env={**environment, **inputs},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_selector_with_no_input_fails_instead_of_skipping_every_test() -> None:
    result = _pytest("-m", "e2e", "tests/e2e", "-o", "addopts=")

    output = result.stdout + result.stderr
    assert result.returncode == 1
    for line in (
        "test_langgraph_e2e: E2E_BASE_URL",
        "test_langgraph_lock_e2e: E2E_BASE_URL, E2E_BASE_URL_B",
        "test_true_streaming_e2e: E2E_STREAM_BASE_URL",
    ):
        assert line in output
    assert re.search(r"\d+ skipped", output) is None


def test_selector_asks_only_for_the_inputs_of_the_modules_it_selects() -> None:
    result = _pytest(
        "-m",
        "e2e",
        "tests/e2e/test_true_streaming_e2e.py",
        "-o",
        "addopts=",
        E2E_BASE_URL="https://a",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "test_true_streaming_e2e: E2E_STREAM_BASE_URL" in output
    assert "E2E_BASE_URL" not in output


def test_selector_does_not_ask_for_the_inputs_of_modules_it_deselects() -> None:
    result = _pytest(
        "-m",
        "e2e",
        "tests/e2e",
        "-o",
        "addopts=",
        "-k",
        "not true_streaming",
        E2E_BASE_URL="https://a",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "test_langgraph_lock_e2e: E2E_BASE_URL_B" in output
    assert "E2E_STREAM_BASE_URL" not in output


def test_collection_needs_no_input() -> None:
    result = _pytest("-m", "e2e", "tests/e2e", "-o", "addopts=", "--collect-only", "-q")

    assert result.returncode == 0, result.stderr
    assert re.search(r"\d+ tests collected", result.stdout)


def test_a_run_that_collects_e2e_tests_without_the_selector_still_skips_them() -> None:
    # The minimum-dependency lane runs `pytest tests/ -o addopts=''`, which collects the e2e tests
    # unselected; only the dedicated `-m e2e` selector may turn their missing inputs into a failure.
    result = _pytest("tests/e2e", "-o", "addopts=", "-q")

    assert result.returncode == 0, result.stdout
    assert re.search(r"\d+ skipped", result.stdout)


def test_hatch_scripts_run_the_dedicated_selector_without_coverage() -> None:
    # Coverage (fail_under = 95) fails any run that does not import src/, so even an all-green
    # e2e run would exit non-zero without --no-cov.
    hatch = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]
    scripts = hatch["envs"]["default"]["scripts"]
    for name, path in (
        ("e2e-azure", "tests/e2e/"),
        ("e2e-local", "tests/e2e/test_langgraph_e2e.py"),
    ):
        words = shlex.split(scripts[name])
        assert words[0] == "pytest"
        assert words[words.index("-m") + 1] == "e2e"
        assert {"--no-cov", path} <= set(words)


def _stream_call(monkeypatch: pytest.MonkeyPatch, key: str) -> tuple[str, dict[str, Any]]:
    module = _load("e2e_true_streaming", E2E / "test_true_streaming_e2e.py")
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(module, "BASE_URL", "https://stream.example.test")
    monkeypatch.setattr(module, "FUNCTION_KEY", key)
    monkeypatch.setattr(module.requests, "post", lambda url, **kw: calls.append((url, kw)))
    module._stream(thread_id="thread-1")
    return calls[0]


def test_function_key_travels_in_a_header_never_in_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, request = _stream_call(monkeypatch, "s3cret-key")

    assert request["headers"] == {"x-functions-key": "s3cret-key"}
    assert "s3cret-key" not in url
    assert "params" not in request


def test_no_key_header_is_sent_when_there_is_no_function_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, request = _stream_call(monkeypatch, "")

    assert request["headers"] == {}
    assert "params" not in request
