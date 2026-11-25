"""Opt-in LOCAL host proof; temporary secrets/logs are destroyed, never evidence."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.client import HTTPConnection
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from typing import Any

from certify_primary_examples import _run
from certify_upstream_examples import message_contents

ROOT = Path(__file__).resolve().parents[1]
STORAGE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)


@contextmanager
def child(
    command: list[str], directory: Path, env: dict[str, str]
) -> Iterator[subprocess.Popen[bytes]]:
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            command,
            cwd=directory,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                process.wait()
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def request(
    port: int, path: str, payload: dict[str, Any] | None = None, key: str = ""
) -> tuple[int, dict[str, Any]]:
    connection = HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["x-functions-key"] = key
        connection.request(
            "POST" if payload is not None else "GET",
            path,
            json.dumps(payload) if payload is not None else None,
            headers,
        )
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else {}
    finally:
        connection.close()


def verify_calls(port: int, name: str, key: str) -> dict[str, Any]:
    path = f"/api/graphs/{name}/invoke"
    payload: dict[str, Any] = {
        "input": {"messages": [{"role": "human", "content": "Hi"}]},
        "config": {"configurable": {"thread_id": "a"}},
    }
    status, _ = request(port, path, payload)
    if status != 401:
        raise RuntimeError("keyless invoke must be rejected")
    status, output = request(port, path, payload, key)
    if status != 200:
        raise RuntimeError("authorized first invoke failed")
    first = message_contents(output["output"])
    if name == "official_basic":
        if first != ["Hi", "Hello!"] or output["output"]["extra_field"] != 10:
            raise RuntimeError("basic host output differs from upstream baseline")
        return {"keyless_status": 401, "authorized_status": 200, "contents": [first]}
    payload["input"]["messages"][0]["content"] = "Continue"
    status, output = request(port, path, payload, key)
    if status != 200:
        raise RuntimeError("authorized continuation invoke failed")
    second = message_contents(output["output"])
    payload["config"]["configurable"]["thread_id"] = "b"
    payload["input"]["messages"][0]["content"] = "Separate"
    status, output = request(port, path, payload, key)
    if status != 200:
        raise RuntimeError("authorized isolated invoke failed")
    isolated = message_contents(output["output"])
    if first != ["Hi", "observed 1 human turn(s)"]:
        raise RuntimeError("first invoke differs from upstream baseline")
    if second != ["Hi", "observed 1 human turn(s)", "Continue", "observed 2 human turn(s)"]:
        raise RuntimeError("continuation lost conversation state")
    if isolated != ["Separate", "observed 1 human turn(s)"]:
        raise RuntimeError("isolated thread contains unexpected conversation state")
    return {
        "keyless_status": 401,
        "authorized_status": 200,
        "contents": [first, second, isolated],
        "continuity": True,
        "isolation": True,
    }


def executable(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(f"local certification requires {name} on PATH")
    return str(Path(resolved).resolve())


def run(args: argparse.Namespace) -> None:
    func = executable("func")
    azurite_executable = executable("azurite")
    for port in (args.host_port, *range(args.storage_port, args.storage_port + 3)):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    env = {**os.environ}
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="upstream-host-") as temporary:
        root = Path(temporary)
        app = root / "app"
        app.mkdir()
        for source in (ROOT / "examples" / args.example).iterdir():
            if source.suffix == ".py" or source.name == "host.json":
                shutil.copyfile(source, app / source.name)
        secret_dir = root / "secrets/functions/secrets"
        secret_dir.mkdir(mode=0o700, parents=True)
        key = secrets.token_urlsafe(32)
        (secret_dir / "host.json").write_text(
            json.dumps(
                {
                    "masterKey": {
                        "name": "master",
                        "value": secrets.token_urlsafe(32),
                        "encrypted": False,
                    },
                    "functionKeys": [{"name": "default", "value": key, "encrypted": False}],
                    "systemKeys": [],
                }
            )
        )
        connection = (
            f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey={STORAGE_KEY};"
            + ";".join(
                f"{service.title()}Endpoint=http://127.0.0.1:"
                f"{args.storage_port + offset}/devstoreaccount1"
                for offset, service in enumerate(("blob", "queue", "table"))
            )
        )
        env.update(
            {
                "TMPDIR": str(root),
                "FUNCTIONS_WORKER_RUNTIME": "python",
                "AzureWebJobsStorage": connection,
                "AZURE_STORAGE_CONNECTION_STRING": connection,
                "AzureWebJobsSecretStorageType": "files",
                "PYTHON_ISOLATE_WORKER_DEPENDENCIES": "1",
            }
        )
        azurite = [
            azurite_executable,
            "--silent",
            "--skipApiVersionCheck",
            "--location",
            str(root / "data"),
        ]
        for offset, service in enumerate(("blob", "queue", "table")):
            azurite.extend(
                [
                    f"--{service}Host",
                    "127.0.0.1",
                    f"--{service}Port",
                    str(args.storage_port + offset),
                ]
            )
        with child(azurite, root, env):
            with child(
                [func, "start", "--python", "--port", str(args.host_port), "--enableAuth"],
                app,
                env,
            ) as host:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and host.poll() is None:
                    try:
                        if request(args.host_port, "/api/health")[0] == 200:
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError("local Functions host readiness failed; raw logs discarded")
                name = (
                    "official_basic"
                    if args.example == "official_langgraph_basic"
                    else "official_conversation"
                )
                result = verify_calls(args.host_port, name, key)
        result.update(
            {
                "scope": "local host only; no Azure plan exercised",
                "python": platform.python_version(),
                "date": datetime.now(UTC).date().isoformat(),
                "versions": {
                    d: version(d)
                    for d in ("azure-functions-langgraph", "langgraph", "langchain-core")
                },
                "core_tools": _run([func, "--version"]).strip(),
                "azurite": _run([azurite_executable, "--version"]).strip(),
            }
        )
        args.evidence.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "example", choices=("official_langgraph_basic", "official_langgraph_conversation")
    )
    parser.add_argument("--host-port", type=int, default=7571)
    parser.add_argument("--storage-port", type=int, default=15000)
    parser.add_argument("--evidence", type=Path, required=True)
    run(parser.parse_args())
