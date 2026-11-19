"""Run the local Functions-host lanes on Azurite with run-owned process groups (#880 #882 #883).

Each lane starts real ``func start`` hosts on loopback ports over one shared Azurite:

* ``locks``     two ``examples/e2e_app`` hosts share one storage account, and the two-app e2e of
                ``tests/e2e/test_langgraph_lock_e2e.py`` must see one winner, a 409 loser and
                checkpoint state that round-trips between the hosts.
* ``platform``  ``tests/host_lanes/platform_app``, declared distributed (Blob lock and checkpointer,
                Table thread store), serves the SDK scenarios of
                ``tests/host_lanes/test_platform_sdk.py``.
* ``topology``  the same app with one piece replaced by its process-local default must be refused at
                indexing with the package's diagnostic and serve no probed route.

Local evidence only: Azurite is not Azure Storage, and ``func start`` is not a deployed host.
Run it as ``python -m tools.run_host_lanes [--lane NAME ...]`` (``make host-lanes``). It never
stops a host or emulator it did not start, and it reaps its own process groups on success,
failure or interrupt.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import tempfile
import time
from typing import NamedTuple, Protocol

import requests

from tools.check_junit import JUnitError, check
from tools.run_host_smoke import (
    ROOT,
    STORAGE_KEY,
    available_port,
    check_host_log,
    child,
    interrupted,
    wait_ready,
)

E2E_APP = ROOT / "examples" / "e2e_app"
PLATFORM_APP = ROOT / "tests" / "host_lanes" / "platform_app"
HOST_SETTINGS = {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "PYTHON_ENABLE_INIT_INDEXING": "1",
    "PYTHON_ISOLATE_WORKER_DEPENDENCIES": "1",
}
DISTRIBUTED = {
    "AZFUNC_LANGGRAPH_LOCK_BACKEND": "distributed",
    "AZFUNC_LANGGRAPH_THREAD_STORE": "distributed",
}
LOCK_HOLD_SECONDS = "10"
INDEXING_FAILURE = "Worker failed to index functions"
SWALLOWED_WIRING_FAILURE = "wiring failed"
STORAGE_TIMEOUT = 30
PYTEST_TIMEOUT = 300
REJECTION_TIMEOUT = 90
POLL_INTERVAL = 0.5
PROBES = (("GET", "/api/health"), ("POST", "/api/threads"))


class Rejection(NamedTuple):
    """A topology the host must refuse: its truth-table rule, the knob and the text it logs."""

    label: str
    rule: str
    settings: dict[str, str]
    expected: str


REJECTIONS: tuple[Rejection, ...] = (
    Rejection(
        "in-process lock",
        "lock-backend-declared",
        {"LANE_LOCK": "inprocess"},
        "requires a distributed thread_lock backend",
    ),
    Rejection(
        "in-memory thread store",
        "thread-store-declared",
        {"LANE_STORE": "memory"},
        "requires a distributed thread store",
    ),
    Rejection(
        "in-memory checkpointer",
        "process-local-checkpointer-declared",
        {"LANE_CHECKPOINTER": "memory"},
        "process-local InMemorySaver checkpointer, but AZFUNC",
    ),
)


class Polled(Protocol):
    """What the lane needs of a child process: whether it has exited."""

    def poll(self) -> int | None: ...


def connection_string(ports: dict[str, int]) -> str:
    endpoints = ";".join(
        f"{service.title()}Endpoint=http://127.0.0.1:{number}/devstoreaccount1"
        for service, number in ports.items()
    )
    account = f"AccountName=devstoreaccount1;AccountKey={STORAGE_KEY}"
    return f"DefaultEndpointsProtocol=http;{account};{endpoints}"


def azurite_command(data: Path, ports: dict[str, int]) -> list[str]:
    command = ["azurite", "--silent", "--skipApiVersionCheck", "--location", str(data)]
    for service, number in ports.items():
        command.extend([f"--{service}Host", "127.0.0.1", f"--{service}Port", str(number)])
    return command


def host_command(port: int) -> list[str]:
    return ["func", "start", "--python", "--port", str(port)]


def write_app(directory: Path, *sources: Path, connection: str, settings: dict[str, str]) -> Path:
    """Copy the Python files and host.json of the sources and write the app's local settings."""
    directory.mkdir()
    for source in sources:
        for file in source.iterdir():
            is_app_file = file.suffix == ".py" and file.name != "__init__.py"
            if file.is_file() and (is_app_file or file.name == "host.json"):
                shutil.copyfile(file, directory / file.name)
    values = {"AzureWebJobsStorage": connection, **HOST_SETTINGS, **settings}
    (directory / "local.settings.json").write_text(
        json.dumps({"IsEncrypted": False, "Values": values})
    )
    return directory


def tail(log: Path, lines: int = 40) -> str:
    return "\n".join(log.read_text(errors="replace").splitlines()[-lines:])


def wait_port(port: int, timeout: float = STORAGE_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"Azurite did not open port {port} within {timeout:.0f}s")


def wait_for_exit(
    process: Polled, timeout: float, what: str, interval: float = POLL_INTERVAL
) -> int:
    """Poll until *process* exits and return its status; a hang raises instead of blocking."""
    deadline = time.monotonic() + timeout
    while True:
        code = process.poll()
        if code is not None:
            return code
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{what} did not exit within {timeout:.0f}s")
        time.sleep(interval)


@contextmanager
def storage(temporary: Path) -> Iterator[str]:
    ports = {service: available_port() for service in ("blob", "queue", "table")}
    with child(azurite_command(temporary / "data", ports), temporary / "azurite.log", temporary):
        wait_port(ports["blob"])
        wait_port(ports["table"])
        yield connection_string(ports)


@contextmanager
def environment(**values: str) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_pytest(
    temporary: Path,
    name: str,
    target: str,
    areas: Sequence[tuple[str, str]],
    env: dict[str, str],
) -> None:
    """Run one e2e module against the lane's hosts and prove that it ran without a skip."""
    report = temporary / f"{name}.xml"
    log = temporary / f"{name}-pytest.log"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-m",
        "e2e",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
        target,
    ]
    with environment(**env), child(command, log, ROOT) as process:
        code = wait_for_exit(process, PYTEST_TIMEOUT, f"{name}: pytest")
    if code != 0:
        raise RuntimeError(f"{name}: pytest exited with status {code}\n{tail(log)}")
    try:
        totals = check(report, areas)
    except (JUnitError, KeyError, ValueError, SyntaxError, OSError) as error:
        raise RuntimeError(
            f"{name}: the JUnit report does not prove the lane ran: {error}"
        ) from error
    print(f"{name}: {totals['tests']} tests ran, none failed, errored or skipped")


def answers(method: str, url: str) -> bool:
    """Whether *url* is served: a status below 400 counts, a refusal or no answer does not."""
    try:
        with requests.request(
            method, url, json={} if method == "POST" else None, timeout=5
        ) as response:
            return response.status_code < 400
    except requests.RequestException:
        return False


def serves_any_route(base_url: str) -> bool:
    return any(answers(method, base_url + path) for method, path in PROBES)


def expect_rejection(
    rejection: Rejection, command: list[str], cwd: Path, log: Path, url: str
) -> None:
    """The host must refuse the app at indexing, log the diagnostic and serve no probed route."""
    with child(command, log, cwd) as host:
        deadline = time.monotonic() + REJECTION_TIMEOUT
        while time.monotonic() < deadline:
            text = log.read_text(errors="replace")
            if INDEXING_FAILURE in text and rejection.expected in text:
                break
            if host.poll() is not None or serves_any_route(url):
                break
            time.sleep(POLL_INTERVAL)
        text = log.read_text(errors="replace")
        if INDEXING_FAILURE not in text:
            raise RuntimeError(f"{rejection.label}: the host logged no indexing failure for it")
        if rejection.expected not in text:
            raise RuntimeError(
                f"{rejection.label}: the host refused it without logging {rejection.expected!r}"
            )
        if serves_any_route(url):
            raise RuntimeError(f"{rejection.label}: a refused topology still serves a route")


def lane_locks(temporary: Path, connection: str) -> None:
    names = ("locks-a", "locks-b")
    ports = [available_port() for _ in names]
    urls = [f"http://127.0.0.1:{port}" for port in ports]
    with ExitStack() as stack:
        hosts = []
        for name, port in zip(names, ports, strict=True):
            app = write_app(
                temporary / name,
                E2E_APP,
                connection=connection,
                settings={"E2E_LOCK_HOLD_SECONDS": LOCK_HOLD_SECONDS},
            )
            hosts.append(
                stack.enter_context(child(host_command(port), temporary / f"{name}.log", app))
            )
        for host, url in zip(hosts, urls, strict=True):
            wait_ready(host, url)
        run_pytest(
            temporary,
            "locks",
            "tests/e2e/test_langgraph_lock_e2e.py",
            [("locks", "test_langgraph_lock_e2e")],
            {"E2E_BASE_URL": urls[0], "E2E_BASE_URL_B": urls[1]},
        )
    for name in names:
        log = temporary / f"{name}.log"
        check_host_log(name, log)
        if SWALLOWED_WIRING_FAILURE in log.read_text(errors="replace"):
            raise RuntimeError(f"{name}: the e2e app logged a swallowed lock wiring failure")


def lane_platform(temporary: Path, connection: str) -> None:
    port = available_port()
    url = f"http://127.0.0.1:{port}"
    log = temporary / "platform.log"
    app = write_app(
        temporary / "platform", PLATFORM_APP, connection=connection, settings=DISTRIBUTED
    )
    with child(host_command(port), log, app) as host:
        wait_ready(host, url)
        run_pytest(
            temporary,
            "platform",
            "tests/host_lanes/test_platform_sdk.py",
            [("platform sdk", "test_platform_sdk")],
            {"LANE_PLATFORM_BASE_URL": url},
        )
    check_host_log("platform", log)


def lane_topology(temporary: Path, connection: str) -> None:
    for index, rejection in enumerate(REJECTIONS):
        name = f"topology-{index}"
        port = available_port()
        app = write_app(
            temporary / name,
            PLATFORM_APP,
            connection=connection,
            settings={**DISTRIBUTED, **rejection.settings},
        )
        expect_rejection(
            rejection,
            host_command(port),
            app,
            temporary / f"{name}.log",
            f"http://127.0.0.1:{port}",
        )


LANES: dict[str, Callable[[Path, str], None]] = {
    "locks": lane_locks,
    "platform": lane_platform,
    "topology": lane_topology,
}


def print_logs(temporary: Path) -> None:
    for log in sorted(temporary.glob("*.log")):
        print(f"--- {log.name} (last lines) ---", file=sys.stderr)
        print(tail(log), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "--lane", action="append", choices=sorted(LANES), help="repeatable; default all"
    )
    arguments = parser.parse_args(argv)
    for executable in ("func", "azurite"):
        if shutil.which(executable) is None:
            print(f"Missing prerequisite: {executable}", file=sys.stderr)
            return 1
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    with tempfile.TemporaryDirectory(prefix="lg-host-lanes-") as directory:
        temporary = Path(directory)
        try:
            with storage(temporary) as connection:
                for name in arguments.lane or list(LANES):
                    started = time.monotonic()
                    LANES[name](temporary, connection)
                    print(f"lane {name}: passed in {time.monotonic() - started:.1f}s")
        except RuntimeError as error:
            print_logs(temporary)
            print(f"error: {error}", file=sys.stderr)
            return 1
        except BaseException:
            print_logs(temporary)
            raise
    print("Local host lanes passed; owned resources removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
