from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import requests

from tools.verification_evidence import Summary, collect, publish, save
from tools.verification_lanes import execute, fresh
from tools.verification_process import child, run
from tools.verification_services import MissingPrerequisite, azurite, configured, port

STORAGE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)


def main() -> int:
    root = Path.cwd()
    directory = root / "test-results/host-smoke"
    fresh(directory)
    code, reason = 0, ""
    try:
        if shutil.which("func") is None or shutil.which("azurite") is None:
            raise MissingPrerequisite("func and azurite on PATH are required")
        with ExitStack() as stack:
            stack.enter_context(azurite(directory))
            temporary = Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="lg-verify-host-"))
            )
            connection = (
                "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
                f"AccountKey={STORAGE_KEY};"
            )
            for service in ("Blob", "Queue", "Table"):
                storage_port = os.environ[f"AZURITE_{service.upper()}_PORT"]
                connection += f"{service}Endpoint=http://127.0.0.1:{storage_port}/devstoreaccount1;"
            environment = {
                "AzureWebJobsStorage": connection,
                "FUNCTIONS_WORKER_RUNTIME": "python",
                "PYTHON_ENABLE_INIT_INDEXING": "1",
                "PYTHON_ISOLATE_WORKER_DEPENDENCIES": "1",
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                "PYTHONPATH": str(root / "src") + os.pathsep + str(root),
            }
            stack.enter_context(configured(environment))
            for streaming in (False, True):
                name = "streaming" if streaming else "native"
                app = temporary / name
                source = root / (
                    "examples/true_streaming_agent" if streaming else "tests/host_smoke"
                )
                shutil.copytree(
                    source,
                    app,
                    ignore=shutil.ignore_patterns(".venv", "__pycache__", ".python_packages"),
                )
                (app / "local.settings.json").write_text(
                    json.dumps({"IsEncrypted": False, "Values": environment})
                )
                number = port()
                with child(
                    ["func", "start", "--python", "--port", str(number)],
                    directory / f"logs/{name}.log",
                    app,
                ):
                    url = f"http://127.0.0.1:{number}"
                    deadline = time.monotonic() + 100
                    while True:
                        try:
                            response = requests.get(url + "/api/health", timeout=2)
                            if response.status_code == 200:
                                break
                        except requests.RequestException:
                            if time.monotonic() >= deadline:
                                raise MissingPrerequisite(
                                    name + " host readiness timed out"
                                ) from None
                        if time.monotonic() >= deadline:
                            raise MissingPrerequisite(name + " host readiness timed out")
                        time.sleep(0.5)
                    if streaming:
                        with configured(
                            {
                                "E2E_STREAM_BASE_URL": url,
                                "E2E_STREAM_CODE": "",
                                "STREAM_TIMING_EVIDENCE": str(directory / "streaming-timing.json"),
                            }
                        ):
                            code = execute("streaming-host", []).exit_code
                        if code:
                            raise MissingPrerequisite("Streaming host tests did not pass")
                        shutil.copyfile(
                            root / "test-results/streaming-host/junit.xml",
                            directory / "streaming-junit.xml",
                        )
                    else:
                        response = requests.post(
                            url + "/api/graphs/smoke/invoke",
                            json={"input": {"messages": [{"role": "user", "content": "ping"}]}},
                            timeout=30,
                        )
                        response.raise_for_status()
                        assert response.json()["output"]["messages"][-1]["type"] == "ai"
                        assert (
                            response.json()["output"]["messages"][-1]["content"]
                            == "host smoke passed"
                        )
                        suite = ET.Element(
                            "testsuite", tests="1", failures="0", errors="0", skipped="0"
                        )
                        ET.SubElement(suite, "testcase", classname="native_host", name="invoke")
                        ET.ElementTree(suite).write(directory / "native-junit.xml")
            run(["func", "--version"], directory / "logs/version.log", 10)
    except (MissingPrerequisite, OSError, requests.RequestException, AssertionError) as error:
        code, reason = 2, str(error)
    except KeyboardInterrupt:
        code, reason = 130, "Interrupted; run-owned hosts terminated"
    summary = collect(directory, ["func", "start", "native+streaming"], code)
    if reason:
        summary = Summary(
            1,
            "host-smoke",
            ("func", "start"),
            summary.selected,
            summary.passed,
            summary.failed,
            summary.skipped,
            "INCOMPLETE",
            code,
            reason=reason,
        )
    publish(save(directory, summary))
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
