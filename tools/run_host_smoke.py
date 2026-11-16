"""Run tutorial, native and streaming HTTP proof with run-owned process groups."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
HOST_FAILURE = re.compile(
    r"0 functions|Worker failed|Host startup operation has been canceled|Error indexing method"
)
STORAGE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)
CONFIG_CANARY = "config-admission-canary"
REJECTED_CONFIGS: tuple[dict[str, object], ...] = (
    {"configurable": {"checkpoint_ns": CONFIG_CANARY}},
    {"configurable": {"checkpoint_id": CONFIG_CANARY}},
    {"recursion_limit": 999},
    {"callbacks": []},
    {"configurable": {"unknown": CONFIG_CANARY}},
    {"configurable": {"__internal": CONFIG_CANARY}},
)
# Mirrors docs/tutorial-5-min.md; tests/test_host_smoke_lifecycle.py fails if the two drift apart.
TUTORIAL_DIR = ROOT / "examples" / "simple_agent"
TUTORIAL_GRAPH = "simple_agent"
TUTORIAL_ROUTES = ("health", "graphs/simple_agent/invoke", "graphs/simple_agent/stream")
TUTORIAL_PAYLOAD = {"input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""}}
TUTORIAL_OUTPUT = {
    "messages": [
        {"role": "human", "content": "World"},
        {"role": "assistant", "content": "Hello, World! Goodbye!"},
    ],
    "greeting": "Hello, World!",
}


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def child(command: list[str], log: Path, cwd: Path) -> Iterator[subprocess.Popen[bytes]]:
    with log.open("wb") as output:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            yield process
        finally:
            # Kill the group even if its leader exited: workers can outlive func.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def wait_ready(process: subprocess.Popen[bytes], url: str) -> None:
    deadline = time.monotonic() + 100
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with requests.get(url + "/api/health", timeout=2) as response:
                if response.status_code == 200:
                    return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError("owned Functions host failed readiness")


def check_host_log(name: str, log: Path) -> None:
    text = log.read_text(errors="replace")
    if HOST_FAILURE.search(text):
        raise RuntimeError(f"{name} host emitted a startup/indexing failure")
    if "host-credential-canary" in text:
        raise RuntimeError("host leaked authorization diagnostics")
    if CONFIG_CANARY in text:
        raise RuntimeError("host leaked configuration diagnostics")


def reject_invalid_configuration(
    url: str, graph_name: str, endpoints: tuple[str, ...], headers: dict[str, str]
) -> None:
    for endpoint in endpoints:
        for config in REJECTED_CONFIGS:
            with requests.post(
                url + f"/api/graphs/{graph_name}/{endpoint}",
                json={"input": {}, "config": config},
                headers=headers,
                timeout=30,
            ) as response:
                if response.status_code != 400 or CONFIG_CANARY in response.text:
                    raise RuntimeError("native configuration admission failed")


def missing_routes(log_text: str, routes: tuple[str, ...]) -> list[str]:
    # /api/health must not be satisfied by the protected /api/health/details route.
    return [
        route
        for route in routes
        if re.search(re.escape(f"/api/{route}") + r"(?![\w/{-])", log_text) is None
    ]


def copy_tutorial_app(source: Path, destination: Path) -> None:
    destination.mkdir()
    for file in source.iterdir():
        if file.suffix == ".py" or file.name in ("host.json", "local.settings.json"):
            shutil.copyfile(file, destination / file.name)


def check_tutorial_calls(url: str) -> None:
    graph = f"{url}/api/graphs/{TUTORIAL_GRAPH}"
    answer = TUTORIAL_OUTPUT["messages"][-1]["content"]
    with requests.get(url + "/api/health", timeout=30) as response:
        response.raise_for_status()
        if response.json() != {"status": "ok"}:
            raise RuntimeError("tutorial health probe differs from the documented response")
    with requests.post(graph + "/invoke", json=TUTORIAL_PAYLOAD, timeout=30) as response:
        response.raise_for_status()
        if response.json().get("output") != TUTORIAL_OUTPUT:
            raise RuntimeError("tutorial invoke differs from the documented response")
    with requests.post(
        graph + "/stream",
        json=TUTORIAL_PAYLOAD,
        headers={"Accept": "text/event-stream"},
        timeout=30,
    ) as response:
        response.raise_for_status()
        if "text/event-stream" not in response.headers.get("content-type", "") or (
            answer not in response.text
        ):
            raise RuntimeError(
                "tutorial stream is not an SSE response carrying the documented answer"
            )
    reject_invalid_configuration(url, TUTORIAL_GRAPH, ("invoke", "stream"), {})


def run_tutorial_host(temporary: Path) -> None:
    app = temporary / "tutorial"
    copy_tutorial_app(TUTORIAL_DIR, app)
    port = available_port()
    url = f"http://127.0.0.1:{port}"
    log = temporary / "tutorial.log"
    with child(["func", "start", "--python", "--port", str(port)], log, app) as host:
        wait_ready(host, url)
        deadline = time.monotonic() + 10
        missing = missing_routes(log.read_text(errors="replace"), TUTORIAL_ROUTES)
        while missing and time.monotonic() < deadline:
            time.sleep(0.5)
            missing = missing_routes(log.read_text(errors="replace"), TUTORIAL_ROUTES)
        if missing:
            raise RuntimeError(f"tutorial host did not list the documented routes: {missing}")
        check_tutorial_calls(url)
    check_host_log("tutorial", log)


def run_hosts(temporary: Path) -> None:
    ports = {service: available_port() for service in ("blob", "queue", "table")}
    connection = (
        f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey={STORAGE_KEY};"
        + ";".join(
            f"{service.title()}Endpoint=http://127.0.0.1:{number}/devstoreaccount1"
            for service, number in ports.items()
        )
    )
    command = [
        "azurite",
        "--silent",
        "--skipApiVersionCheck",
        "--location",
        str(temporary / "data"),
    ]
    for service, number in ports.items():
        command.extend([f"--{service}Host", "127.0.0.1", f"--{service}Port", str(number)])
    with child(command, temporary / "azurite.log", temporary):
        for name, source in (
            ("native", ROOT / "tests/host_smoke"),
            ("streaming", ROOT / "examples/true_streaming_agent"),
        ):
            app = temporary / name
            app.mkdir()
            for file in source.iterdir():
                if file.suffix == ".py" or file.name == "host.json":
                    shutil.copyfile(file, app / file.name)
            (app / "local.settings.json").write_text(
                json.dumps(
                    {
                        "IsEncrypted": False,
                        "Values": {
                            "AzureWebJobsStorage": connection,
                            "FUNCTIONS_WORKER_RUNTIME": "python",
                            "PYTHON_ENABLE_INIT_INDEXING": "1",
                            "PYTHON_ISOLATE_WORKER_DEPENDENCIES": "1",
                        },
                    }
                )
            )
            port = available_port()
            url = f"http://127.0.0.1:{port}"
            log = temporary / f"{name}.log"
            with child(["func", "start", "--python", "--port", str(port)], log, app) as host:
                wait_ready(host, url)
                graph_name = "smoke" if name == "native" else "true_streaming_agent"
                endpoints = ("invoke",) if name == "native" else ("invoke", "stream")
                reject_invalid_configuration(
                    url, graph_name, endpoints, {"x-smoke-principal": "owner"}
                )
                if name == "native":
                    with requests.post(
                        url + "/api/graphs/smoke/invoke",
                        json={"input": {"messages": [{"role": "user", "content": "ping"}]}},
                        headers={"x-smoke-principal": "owner"},
                        timeout=30,
                    ) as response:
                        response.raise_for_status()
                        msg = response.json()["output"]["messages"][-1]
                        if (msg["type"], msg["data"]["content"]) != ("ai", "host smoke passed"):
                            raise RuntimeError("native invoke returned the wrong graph output")
                    with requests.get(
                        url + "/api/graphs/smoke/threads/host-smoke/state", timeout=30
                    ) as response:
                        if response.status_code != 401 or "host-credential-canary" in response.text:
                            raise RuntimeError("native state callback denial failed")
                else:
                    report = temporary / "streaming.xml"
                    subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "pytest",
                            "-m",
                            "e2e",
                            "--no-cov",
                            "tests/e2e/test_true_streaming_e2e.py",
                            f"--junitxml={report}",
                        ],
                        cwd=ROOT,
                        env={**os.environ, "E2E_STREAM_BASE_URL": url, "E2E_STREAM_CODE": ""},
                        check=True,
                        timeout=100,
                    )
                    subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "tools/check_junit.py"),
                            str(report),
                            "--area",
                            "streaming=test_true_streaming_e2e",
                        ],
                        check=True,
                    )
            check_host_log(name, log)


def interrupted(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def main() -> int:
    for executable in ("func", "azurite"):
        if shutil.which(executable) is None:
            print(f"Missing prerequisite: {executable}", file=sys.stderr)
            return 1
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    with tempfile.TemporaryDirectory(prefix="lg-host-686-") as directory:
        temporary = Path(directory)
        run_tutorial_host(temporary)
        run_hosts(temporary)
    print("Tutorial, native invoke and true-streaming host smoke passed; owned resources removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
