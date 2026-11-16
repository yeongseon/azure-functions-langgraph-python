from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Any, Self

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("host_runner", ROOT / "tools/run_host_smoke.py")
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.mark.parametrize("fail", [False, True])
def test_owned_process_is_reaped_after_success_or_failure(tmp_path: Path, fail: bool) -> None:
    # Given: a child owned by this test, with no shared service or port.
    guard = pytest.raises(RuntimeError) if fail else nullcontext()
    # When: the protected operation returns or raises.
    with guard:
        with runner.child(
            [sys.executable, "-c", "import signal; signal.pause()"],
            tmp_path / "child.log",
            tmp_path,
        ) as process:
            if fail:
                raise RuntimeError("deliberate failure")
    # Then: the process is reaped in either case.
    assert process.poll() is not None


def test_interrupt_reaps_owned_process(tmp_path: Path) -> None:
    # Given: an owned child and the runner's signal handler.
    previous = signal.signal(signal.SIGTERM, runner.interrupted)
    try:
        # When: SIGTERM interrupts a running operation.
        with pytest.raises(KeyboardInterrupt):
            with runner.child(
                [sys.executable, "-c", "import signal; signal.pause()"],
                tmp_path / "interrupt.log",
                tmp_path,
            ) as process:
                os.kill(os.getpid(), signal.SIGTERM)
        # Then: cleanup finishes before interruption propagates.
        assert process.poll() is not None
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.parametrize(
    "diagnostic",
    [
        "0 functions",
        "Worker failed",
        "Host startup operation has been canceled",
        "Error indexing method",
    ],
)
def test_existing_host_failure_patterns_remain_fatal(diagnostic: str) -> None:
    # Given / When: a startup/indexing failure in an owned host log.
    failure = runner.HOST_FAILURE.search(diagnostic)
    # Then: successful HTTP traffic cannot hide that failure.
    assert failure is not None


def test_missing_host_prerequisite_fails_without_starting_services(tmp_path: Path) -> None:
    # Given: no host or emulator executable on PATH.
    # When: the real CLI runs.
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/run_host_smoke.py")],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    # Then: it fails before acquiring resources.
    assert result.returncode == 1
    assert "Missing prerequisite: func" in result.stderr


TUTORIAL_DOC = ROOT / "docs/tutorial-5-min.md"
TUTORIAL_EXAMPLE = ROOT / "examples/simple_agent"
HOST_LOG = (
    "Functions:\n\n"
    "\taflg_health: [GET] http://localhost:7071/api/health\n\n"
    "\taflg_health_details: [GET] http://localhost:7071/api/health/details\n\n"
    "\taflg_simple_agent_invoke: [POST] http://localhost:7071/api/graphs/simple_agent/invoke\n\n"
    "\taflg_simple_agent_stream: [POST] http://localhost:7071/api/graphs/simple_agent/stream\n"
)


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        *,
        text: str | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if text is None else text
        self.headers = {"content-type": content_type}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHost:
    """Answers the tutorial's documented calls; one override at a time makes it drift."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, **overrides: FakeResponse) -> None:
        self.responses: dict[str, FakeResponse] = {
            "health": FakeResponse(body={"status": "ok"}),
            "invoke": FakeResponse(body={"output": runner.TUTORIAL_OUTPUT}),
            "stream": FakeResponse(
                text=f"event: data\ndata: {json.dumps(runner.TUTORIAL_OUTPUT)}\n\n",
                content_type="text/event-stream",
            ),
            "rejected": FakeResponse(400, {"error": "invalid configuration"}),
            **overrides,
        }
        self.rejected_calls = 0
        self.rejected_headers: list[dict[str, str]] = []
        monkeypatch.setattr(runner.requests, "get", self.get)
        monkeypatch.setattr(runner.requests, "post", self.post)

    def get(self, url: str, **_: Any) -> FakeResponse:
        assert url.endswith("/api/health"), url
        return self.responses["health"]

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        body = kwargs.get("json") or {}
        if "config" in body:
            self.rejected_calls += 1
            self.rejected_headers.append(kwargs.get("headers") or {})
            return self.responses["rejected"]
        return self.responses[url.rsplit("/", 1)[-1]]


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in re.finditer(r"```(\w*)\n(.*?)```", text, re.S)]


def _documented_invoke() -> tuple[Any, Any]:
    blocks = _fenced_blocks(TUTORIAL_DOC.read_text(encoding="utf-8"))
    for index, (language, body) in enumerate(blocks):
        if language == "bash" and "graphs/simple_agent/invoke" in body:
            request = re.search(r"-d '(\{.*\})'", body)
            assert request is not None, "the documented invoke curl has no -d payload"
            assert blocks[index + 1][0] == "json", "the invoke curl is not followed by its response"
            return json.loads(request.group(1)), json.loads(blocks[index + 1][1])
    raise AssertionError("the tutorial no longer documents the invoke request")


def test_tutorial_expectations_match_the_documented_invoke_request_and_response() -> None:
    # Given: the invoke curl and its JSON response as the tutorial prints them.
    payload, response = _documented_invoke()
    # When / Then: the host check asserts exactly those documented values.
    assert runner.TUTORIAL_PAYLOAD == payload
    assert {"output": runner.TUTORIAL_OUTPUT} == response


def test_tutorial_documents_the_routes_and_the_empty_storage_setting_the_check_relies_on() -> None:
    # Given: the tutorial text and the example's own settings file.
    text = TUTORIAL_DOC.read_text(encoding="utf-8")
    settings = json.loads((TUTORIAL_EXAMPLE / "local.settings.json").read_text(encoding="utf-8"))
    # When / Then: the routes the check expects are the routes the tutorial names, and the claim
    # that no storage emulator is needed is backed by an empty AzureWebJobsStorage.
    for route in runner.TUTORIAL_ROUTES:
        assert f"`{route}`" in text, route
    assert "sets `AzureWebJobsStorage` to an empty" in text
    assert "No storage emulator is needed" in text
    assert settings["Values"]["AzureWebJobsStorage"] == ""


def test_tutorial_copy_keeps_the_examples_own_files_and_adds_nothing(tmp_path: Path) -> None:
    # Given / When: the example is copied for a host run.
    destination = tmp_path / "tutorial"
    runner.copy_tutorial_app(TUTORIAL_EXAMPLE, destination)
    copied = {path.name for path in destination.iterdir()}
    # Then: the files func reads are byte-identical, and nothing else is written or copied.
    assert {"function_app.py", "graph.py", "host.json", "local.settings.json"} <= copied
    assert not copied & {"requirements.txt", "README.md", "__pycache__"}
    for name in copied:
        assert (destination / name).read_bytes() == (TUTORIAL_EXAMPLE / name).read_bytes(), name


def test_missing_routes_names_only_the_routes_the_host_did_not_list() -> None:
    # Given: a host log that lists every documented route.
    assert runner.missing_routes(HOST_LOG, runner.TUTORIAL_ROUTES) == []
    # When: the stream route is registered under another graph name.
    drifted = HOST_LOG.replace("/api/graphs/simple_agent/stream", "/api/graphs/other/stream")
    # Then: only that route is reported.
    assert runner.missing_routes(drifted, runner.TUTORIAL_ROUTES) == ["graphs/simple_agent/stream"]


def test_the_health_details_route_does_not_stand_in_for_the_health_probe() -> None:
    # Given: a log that lists only the protected inventory route.
    log = "\taflg_health_details: [GET] http://localhost:7071/api/health/details\n"
    # When / Then: the anonymous probe is still reported missing.
    assert runner.missing_routes(log, ("health",)) == ["health"]


@pytest.mark.parametrize(
    "text",
    ["Host started\n0 functions found", "Worker failed to start", "Error indexing method 'x'"],
)
def test_a_failing_tutorial_host_log_is_fatal(tmp_path: Path, text: str) -> None:
    # Given: a host log with a startup or indexing failure.
    log = tmp_path / "tutorial.log"
    log.write_text(text, encoding="utf-8")
    # When / Then: the run fails and names the host.
    with pytest.raises(RuntimeError, match="tutorial host emitted a startup/indexing failure"):
        runner.check_host_log("tutorial", log)


@pytest.mark.parametrize("canary", ["host-credential-canary", "config-admission-canary"])
def test_a_host_log_that_leaks_a_canary_is_fatal(tmp_path: Path, canary: str) -> None:
    # Given: a host log that echoes a value the requests asked it to protect.
    log = tmp_path / "native.log"
    log.write_text(f"request failed: {canary}", encoding="utf-8")
    # When / Then: the run fails.
    with pytest.raises(RuntimeError, match="leaked"):
        runner.check_host_log("native", log)


def test_a_clean_host_log_passes(tmp_path: Path) -> None:
    log = tmp_path / "tutorial.log"
    log.write_text(HOST_LOG, encoding="utf-8")
    runner.check_host_log("tutorial", log)


def test_documented_tutorial_calls_pass_and_every_invalid_configuration_is_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a host that answers exactly as the tutorial documents.
    host = FakeHost(monkeypatch)
    # When: the tutorial checks run.
    runner.check_tutorial_calls("http://127.0.0.1:7071")
    # Then: both routes reject every invalid configuration, with no app-specific header.
    assert host.rejected_calls == 2 * len(runner.REJECTED_CONFIGS)
    assert all(headers == {} for headers in host.rejected_headers)


@pytest.mark.parametrize(
    "drift",
    [
        "health-status",
        "invoke-greeting",
        "stream-not-sse",
        "stream-without-answer",
        "configuration-accepted",
        "configuration-echoed",
    ],
)
def test_a_tutorial_response_that_drifts_from_the_documentation_fails(
    monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    # Given: a host that differs from the documentation in exactly one response.
    drifted_output = {**runner.TUTORIAL_OUTPUT, "greeting": "Hi"}
    drifts: dict[str, tuple[dict[str, FakeResponse], str]] = {
        "health-status": ({"health": FakeResponse(body={"status": "degraded"})}, "health"),
        "invoke-greeting": ({"invoke": FakeResponse(body={"output": drifted_output})}, "invoke"),
        "stream-not-sse": (
            {
                "stream": FakeResponse(
                    text="Hello, World! Goodbye!", content_type="application/json"
                )
            },
            "stream",
        ),
        "stream-without-answer": (
            {
                "stream": FakeResponse(
                    text="event: data\ndata: {}\n\n", content_type="text/event-stream"
                )
            },
            "stream",
        ),
        "configuration-accepted": (
            {"rejected": FakeResponse(200, {"output": {}})},
            "configuration admission",
        ),
        "configuration-echoed": (
            {"rejected": FakeResponse(400, text="bad config-admission-canary")},
            "configuration admission",
        ),
    }
    override, message = drifts[drift]
    FakeHost(monkeypatch, **override)
    # When / Then: the check fails and says which call drifted.
    with pytest.raises(RuntimeError, match=message):
        runner.check_tutorial_calls("http://127.0.0.1:7071")


def test_tutorial_host_runs_before_any_emulator_backed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: both prerequisites present and the two host runs replaced by recorders.
    order: list[str] = []
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(runner, "run_tutorial_host", lambda temporary: order.append("tutorial"))
    monkeypatch.setattr(runner, "run_hosts", lambda temporary: order.append("emulator hosts"))
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        # When: the runner's entry point runs.
        assert runner.main() == 0
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    # Then: the tutorial app starts first, while no Azurite process exists.
    assert order == ["tutorial", "emulator hosts"]
