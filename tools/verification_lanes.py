from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from importlib.util import find_spec
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from tools.verification_cosmos import trust_certificate
from tools.verification_evidence import Summary, collect, publish, redact, save
from tools.verification_process import run
from tools.verification_services import MissingPrerequisite, azurite, compose, configured

TABLE_TESTS = (
    "tests/integration/test_table_store_integration.py",
    "tests/integration/test_checkpoint_conformance.py",
    "tests/test_production_persistent_agent_example.py",
)
TABLE_AREAS = ("table_store_integration", "checkpoint_conformance", "production_persistent_agent")


def fresh(directory: Path) -> None:
    if directory.exists():
        shutil.rmtree(directory)
    (directory / "logs").mkdir(parents=True)


def pytest_command(lane: str, extra: list[str]) -> list[str]:
    command = [sys.executable, "-m", "pytest"]
    match lane:
        case "test":
            command += [
                "tests/",
                "-m",
                "not e2e and not integration",
                "--ignore=tests/e2e",
                "--ignore=tests/integration",
                "--ignore=tests/host_smoke",
                "--ignore=tests/test_production_persistent_agent_example.py",
                "--cov-report=xml:test-results/test/coverage.xml",
            ]
        case "table":
            command += [*TABLE_TESTS, "-m", "integration", "--no-cov", "--strict-markers"]
        case "cosmos":
            command += [
                "tests/integration/test_cosmos_integration.py",
                "-m",
                "integration",
                "--no-cov",
            ]
        case "deterministic":
            command += [
                "tests/test_checkpointers_azure_blob.py",
                "tests/test_stores_azure_table.py",
                "tests/test_locks_azure_blob.py",
                "tests/test_locks_inprocess.py",
                "--no-cov",
            ]
        case "streaming-host":
            command += ["tests/e2e/test_true_streaming_e2e.py", "-m", "e2e", "--no-cov"]
        case "dependency":
            command += [
                "tests/",
                "-m",
                "not integration and not e2e",
                "--no-cov",
                "--ignore=tests/e2e",
                "--ignore=tests/integration",
                "--ignore=tests/test_production_persistent_agent_example.py",
            ]
        case _:
            command += ["tests/e2e/", "-m", "e2e", "--no-cov"]
    return [*command, *extra]


def azure_preflight() -> str:
    required = [
        "E2E_BASE_URL",
        "E2E_EXPECTED_VERSION",
        "E2E_CANDIDATE_SHA256",
        "E2E_RUNTIME_PROFILE",
        "E2E_CANDIDATE_COMMIT",
    ]
    if os.environ.get("E2E_RUNTIME_PROFILE") != "flex-sdk2":
        required += ["E2E_BASE_URL_B", "E2E_STREAM_BASE_URL", "E2E_STREAM_CODE"]
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    if missing:
        return "Missing Azure candidate prerequisites: " + ", ".join(missing)
    if (
        len(os.environ["E2E_CANDIDATE_SHA256"]) != 64
        or len(os.environ["E2E_CANDIDATE_COMMIT"]) != 40
    ):
        return "Candidate digest/commit must be full SHA-256/Git identities"
    for key in ("E2E_BASE_URL", "E2E_BASE_URL_B", "E2E_STREAM_BASE_URL"):
        if not os.environ.get(key):
            continue
        url = urlsplit(os.environ[key])
        if (
            url.scheme != "https"
            or not (url.hostname or "").endswith(".azurewebsites.net")
            or url.username
            or url.query
        ):
            return key + " must be a deployed Azure HTTPS URL without credentials/query"
    return ""


def execute(lane: str, extra: list[str], *, owned: bool = False) -> Summary:
    directory = Path("test-results") / ("integration" if lane in {"table", "cosmos"} else "") / lane
    fresh(directory)
    command = pytest_command(lane, extra)
    expected = (
        TABLE_AREAS if lane == "table" else (("cosmos_integration",) if lane == "cosmos" else ())
    )
    reason, code, blocked = "", 0, False
    try:
        if lane == "azure-e2e":
            if reason := azure_preflight():
                raise MissingPrerequisite(reason)
            if os.environ.get("E2E_RUNTIME_PROFILE") == "flex-sdk2":
                command += [
                    "--ignore=tests/e2e/test_true_streaming_e2e.py",
                    "-k",
                    "not app_b and not two_app",
                ]
        if lane in {"table", "cosmos"}:
            modules = (
                ("azure.data.tables", "azure.storage.blob", "langgraph.checkpoint.conformance")
                if lane == "table"
                else ("azure.cosmos", "langgraph_checkpoint_cosmosdb")
            )
            for module in modules:
                try:
                    installed = find_spec(module) is not None
                except ModuleNotFoundError:
                    installed = False
                if not installed:
                    raise MissingPrerequisite("Missing integration package: " + module)
        service: AbstractContextManager[None] = nullcontext()
        if owned:
            service = azurite(directory) if lane == "table" else compose("cosmos", directory)
        values = {"AZURITE_REQUIRED": "true"} if lane == "table" else {}
        with (
            service,
            configured(values),
            configured(
                {
                    key: os.environ.get(key, "")
                    for key in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "COSMOS_EMULATOR_ENDPOINT")
                }
            ),
            tempfile.TemporaryDirectory(prefix="lg-verify-junit-") as temporary,
        ):
            if owned and lane == "cosmos":
                trust_certificate(Path(temporary), directory)
            raw = Path(temporary) / "junit.xml"
            code = run([*command, "--junitxml=" + str(raw)], directory / "logs/runner.log", 1200)
            if raw.exists():
                (directory / "junit.xml").write_text(redact(raw.read_text()))
    except MissingPrerequisite as error:
        blocked, code, reason = True, 2, str(error)
    except (OSError, ValueError, ET.ParseError, subprocess.CalledProcessError) as error:
        code, reason = 2, str(error)
    except KeyboardInterrupt:
        code, reason = 130, "Interrupted; only owned processes/services cleaned up"
    try:
        summary = collect(directory, command, code, expected=expected)
    except (ValueError, KeyError, ET.ParseError) as error:
        summary = Summary(
            1,
            lane,
            tuple(command),
            0,
            0,
            0,
            0,
            "INCOMPLETE",
            code or 2,
            reason="Invalid/missing JUnit evidence: " + str(error),
        )
    summary = replace(
        summary,
        lane=lane,
        status="INCOMPLETE" if blocked else summary.status,
        reason=reason or summary.reason,
        required=not (
            lane == "cosmos" and os.environ.get("VERIFICATION_OPTIONAL") == "nightly-cosmos"
        ),
        runtime_profile=os.environ.get("E2E_RUNTIME_PROFILE", "local; not Azure certification"),
        candidate_version=os.environ.get("E2E_EXPECTED_VERSION", ""),
        artifact_sha256=os.environ.get("E2E_CANDIDATE_SHA256", ""),
        candidate_commit=os.environ.get("E2E_CANDIDATE_COMMIT", ""),
        deployed_artifact_identity="INCOMPLETE" if lane == "azure-e2e" else "NOT_APPLICABLE",
        scope="AZURE_PROFILE" if lane == "azure-e2e" else "PARTIAL",
    )
    publish(save(directory, summary))
    return summary
