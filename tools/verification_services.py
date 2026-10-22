from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

from tools.verification_process import child, run


class MissingPrerequisite(RuntimeError):
    pass


@contextmanager
def configured(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def require_free(ports: tuple[int, ...]) -> None:
    for number in ports:
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", number))
        except OSError as error:
            raise MissingPrerequisite(
                f"Port {number} is occupied; refusing shared services"
            ) from error


def ready(ports: tuple[int, ...], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            for number in ports:
                with socket.create_connection(("127.0.0.1", number), timeout=1):
                    continue
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise MissingPrerequisite("Emulator readiness timed out") from error
            time.sleep(0.2)


@contextmanager
def compose(backend: str, directory: Path) -> Iterator[None]:
    if shutil.which("docker") is None:
        raise MissingPrerequisite("Docker Compose is required for " + backend)
    project = "lg-verify-" + uuid.uuid4().hex
    base = ["docker", "compose", "-p", project, "-f", f"docker-compose.{backend}.yml"]
    existing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        capture_output=True,
        text=True,
        check=True,
    )
    if existing.stdout.strip():
        raise MissingPrerequisite("Generated project already exists; refusing adoption")
    ports = (
        (
            int(os.environ.get("COSMOS_PORT", "8081")),
            *(int(os.environ.get(f"COSMOS_PORT_{p}", str(p))) for p in range(10250, 10256)),
        )
        if backend == "cosmos"
        else tuple(
            int(os.environ.get(f"AZURITE_{service}_PORT", str(default)))
            for service, default in (("BLOB", 10000), ("QUEUE", 10001), ("TABLE", 10002))
        )
    )
    require_free(ports)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "service.json").write_text(
        '{"project": "' + project + '", "backend": "' + backend + '"}'
    )
    try:
        code = run(
            [*base, "up", "-d", "--wait", "--wait-timeout", "180"],
            directory / "logs/startup.log",
            240,
        )
        if code:
            raise MissingPrerequisite(f"{backend} emulator cannot start (exit {code})")
        ready(ports, 30)
        run([*base, "ps", "--format", "json"], directory / "logs/health.log", 15)
        run([*base, "images"], directory / "logs/version.log", 15)
        yield
    finally:
        run([*base, "logs", "--no-color"], directory / "logs/service.log", 15)
        if run([*base, "down", "--volumes", "--timeout", "10"], directory / "logs/cleanup.log", 30):
            raise MissingPrerequisite("Run-owned Compose cleanup failed: " + project)


@contextmanager
def azurite(directory: Path) -> Iterator[None]:
    if shutil.which("azurite") is None:
        with compose("azurite", directory):
            yield
        return
    ports = tuple(
        int(os.environ.get(f"AZURITE_{service}_PORT", str(port())))
        for service in ("BLOB", "QUEUE", "TABLE")
    )
    require_free(ports)
    values = {
        f"AZURITE_{service}_PORT": str(number)
        for service, number in zip(("BLOB", "QUEUE", "TABLE"), ports, strict=True)
    }
    with configured(values), tempfile.TemporaryDirectory(prefix="lg-verify-azurite-") as temporary:
        command = ["azurite", "--silent", "--skipApiVersionCheck", "--location", temporary]
        for service, number in zip(("blob", "queue", "table"), ports, strict=True):
            command += [f"--{service}Host", "127.0.0.1", f"--{service}Port", str(number)]
        with child(command, directory / "logs/azurite.log"):
            ready(ports)
            run(["azurite", "--version"], directory / "logs/version.log", 10)
            (directory / "health.json").write_text('{"tcp_ready": true, "binding": "127.0.0.1"}')
            yield
