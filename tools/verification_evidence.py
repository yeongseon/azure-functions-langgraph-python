from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from typing import Literal
import xml.etree.ElementTree as ET

Status = Literal["PASS", "FAIL", "INCOMPLETE", "NOT_APPLICABLE"]


@dataclass(frozen=True, slots=True)
class Summary:
    schema_version: int
    lane: str
    command: tuple[str, ...]
    selected: int
    passed: int
    failed: int
    skipped: int
    status: Status
    exit_code: int
    required: bool = True
    scope: str = "PARTIAL"
    supporting_files: tuple[str, ...] = ()
    reason: str = ""
    runtime_profile: str = "local; not Azure certification"
    candidate_version: str = ""
    artifact_sha256: str = ""
    candidate_commit: str = ""
    deployed_artifact_identity: Status = "NOT_APPLICABLE"
    dependency_profile: str = "resolved-default"


def redact(text: str) -> str:
    text = re.sub(r"(?im)(authorization[\"']?\s*[:=]\s*)[^\r\n<\"]+", r"\1[REDACTED]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)(DefaultEndpointsProtocol=)[^\s<\"']+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)(?<![\w-])((?:[\w-]*(?:token|password|secret|connection[_-]?string|"
        r"cosmos[_-]?(?:emulator[_-]?)?key)|AccountKey|SharedAccessSignature|"
        r"AzureWebJobsStorage|x-functions-key|code|sig|sas|api[_-]?key)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s;&<\"']+",
        r"\1[REDACTED]",
        text,
    )
    for key, value in os.environ.items():
        if len(value) >= 6 and re.search(r"(?i)secret|token|password|connection|key|sas", key):
            text = text.replace(value, "[REDACTED]")
    return text.replace(
        "C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw==",
        "[REDACTED]",
    )


def environment() -> dict[str, str]:
    metadata = {"python": platform.python_version(), "platform": platform.platform()}
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    metadata["source_commit"] = result.stdout.strip()
    for package in (
        "azure-functions",
        "azure-functions-langgraph",
        "langgraph",
        "langgraph-sdk",
        "azure-data-tables",
        "azure-storage-blob",
        "langgraph-checkpoint-cosmosdb",
    ):
        try:
            metadata[package] = version(package)
        except PackageNotFoundError:
            metadata[package] = "not installed"
    return metadata


def collect(
    directory: Path, command: list[str], exit_code: int, *, expected: tuple[str, ...] = ()
) -> Summary:
    cases: list[ET.Element] = []
    for path in directory.rglob("*junit*.xml"):
        root = ET.parse(path).getroot()
        suites = list(root.iter("testsuite"))
        if not suites:
            raise ValueError("JUnit contains no testsuite")
        for suite in suites:
            actual_cases = list(suite.iter("testcase"))
            for counter, outcome in (
                ("tests", None),
                ("failures", "failure"),
                ("errors", "error"),
                ("skipped", "skipped"),
            ):
                actual = (
                    len(actual_cases)
                    if outcome is None
                    else sum(case.find(outcome) is not None for case in actual_cases)
                )
                if int(suite.attrib[counter]) != actual:
                    detail = (
                        "JUnit testsuite test count does not match testcase descendants"
                        if counter == "tests"
                        else "JUnit summary does not match testcase outcomes"
                    )
                    raise ValueError(f"{detail}; counter mismatch: {counter}")
        cases.extend(root.iter("testcase"))
    failed = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
    skipped = sum(c.find("skipped") is not None for c in cases)
    passed = len(cases) - failed - skipped
    unexpected_skips = skipped
    if directory.name in {"test", "wheel-suite", "dependency"}:
        unexpected_skips -= sum(
            (skip := case.find("skipped")) is not None
            and case.get("name") == "test_detects_missing_runtime_tier"
            and skip.get("message") == "minimal family has no required runtime tier"
            for case in cases
        )
    if (
        directory.name in {"test", "dependency"}
        and os.environ.get("VERIFICATION_DEPENDENCY_PROFILE") == "minimum"
    ):
        unexpected_skips -= sum(
            (skip := case.find("skipped")) is not None
            and skip.get("message")
            in {
                "langgraph<1.1 does not support version='v2'",
                "requires the optional 'openapi' extra: "
                "pip install azure-functions-langgraph[openapi]",
            }
            for case in cases
        )
    identities = [" ".join((c.get("classname", ""), c.get("file", ""))) for c in cases]
    missing = [area for area in expected if not any(area in identity for identity in identities)]
    status: Status = "PASS"
    reason = ""
    if failed or exit_code not in (0, 5):
        status = "FAIL"
    elif not passed or unexpected_skips or missing:
        status, reason = (
            "INCOMPLETE",
            "Missing execution, unexpected skips or areas: " + ", ".join(missing),
        )
    return Summary(
        1,
        directory.name,
        tuple(command),
        len(cases),
        passed,
        failed,
        skipped,
        status,
        exit_code or int(status != "PASS"),
        reason=reason,
    )


def publish(summary: Summary) -> None:
    text = redact(
        f"{summary.lane}: {summary.status} | scope={summary.scope} selected={summary.selected} "
        f"passed={summary.passed} failed={summary.failed} skipped={summary.skipped} "
        f"exit={summary.exit_code} | {summary.runtime_profile} | {summary.reason}"
    )
    print(text)
    if destination := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(destination).open("a", encoding="utf-8") as stream:
            stream.write("\n- " + text + "\n")


def save(directory: Path, summary: Summary) -> Summary:
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.rglob("*"):
        if path.is_file():
            path.write_text(redact(path.read_text(encoding="utf-8")), encoding="utf-8")
    (directory / "environment.json").write_text(json.dumps(environment(), indent=2) + "\n")
    summary = replace(
        summary,
        dependency_profile=os.environ.get("VERIFICATION_DEPENDENCY_PROFILE", "resolved-default"),
        command=tuple(redact(p) for p in summary.command),
        reason=redact(summary.reason),
        supporting_files=tuple(
            str(p) for p in sorted(directory.rglob("*")) if p.is_file() and p.name != "summary.json"
        ),
    )
    serialized = redact(json.dumps(asdict(summary), indent=2))
    (directory / "summary.json").write_text(serialized + "\n")
    return Summary(**json.loads(serialized))


def report(root: Path) -> int:
    paths = sorted(root.rglob("summary.json"))
    for path in paths:
        publish(Summary(**json.loads(path.read_text())))
    if not paths:
        print("No saved verification results")
    return int(not paths)
