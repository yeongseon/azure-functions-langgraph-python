"""Run native and streaming HTTP proof with run-owned process groups."""

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
                if name == "native":
                    with requests.post(
                        url + "/api/graphs/smoke/invoke",
                        json={"input": {"messages": [{"role": "user", "content": "ping"}]}},
                        headers={"x-smoke-principal": "owner"},
                        timeout=30,
                    ) as response:
                        response.raise_for_status()
                        message = response.json()["output"]["messages"][-1]
                        if message["type"] != "ai" or message["data"]["content"] != "host smoke passed":
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
            if HOST_FAILURE.search(log.read_text()):
                raise RuntimeError(f"{name} host emitted a startup/indexing failure")
            if "host-credential-canary" in log.read_text():
                raise RuntimeError("host leaked authorization diagnostics")


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
        run_hosts(Path(directory))
    print("Native invoke and true-streaming host smoke passed; owned resources removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
