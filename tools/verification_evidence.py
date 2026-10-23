from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import subprocess
from typing import Literal
import xml.etree.ElementTree as ET

from tools.verification_redaction import (
    _TEXT_SUFFIXES,
    _redact_file_text,
    dump_json,
)
from tools.verification_redaction import (
    redact as redact,
)
from tools.verification_redaction import (
    redact_json as redact_json,
)
from tools.verification_skips import expected_skip

Status = Literal["PASS", "FAIL", "INCOMPLETE", "NOT_APPLICABLE"]


def read_xml_root(path: Path) -> ET.Element:
    """Parse a locally generated report, refusing entity declarations."""
    data = path.read_bytes()
    if b"<!ENTITY" in data.upper():
        raise ValueError("XML reports must not declare entities")
    return ET.fromstring(data)


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
        root = read_xml_root(path)
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
    unexpected = [
        case
        for case in cases
        if case.find("skipped") is not None and not expected_skip(case, directory.name)
    ]
    identities = [" ".join((c.get("classname", ""), c.get("file", ""))) for c in cases]
    missing = [area for area in expected if not any(area in identity for identity in identities)]
    status: Status = "PASS"
    reason = ""
    if failed or exit_code not in (0, 5):
        status = "FAIL"
    elif not passed or unexpected or missing:
        status, reason = (
            "INCOMPLETE",
            "Missing execution, unexpected skips or areas: "
            + ", ".join(
                [*missing, *(f"{c.get('classname')}::{c.get('name')}" for c in unexpected)]
            ),
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
        if path.is_symlink() or not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        redacted = _redact_file_text(path.suffix, original)
        if redacted != original:
            path.write_text(redacted, encoding="utf-8")
    (directory / "environment.json").write_text(dump_json(environment()))
    summary = replace(
        summary,
        dependency_profile=os.environ.get("VERIFICATION_DEPENDENCY_PROFILE", "resolved-default"),
        command=tuple(redact(p) for p in summary.command),
        reason=redact(summary.reason),
        supporting_files=tuple(
            str(p)
            for p in sorted(directory.rglob("*"))
            if not p.is_symlink() and p.is_file() and p.name != "summary.json"
        ),
    )
    serialized = dump_json(asdict(summary))
    (directory / "summary.json").write_text(serialized)
    return Summary(**json.loads(serialized))


def report(root: Path) -> int:
    paths = sorted(root.rglob("summary.json"))
    found = 0
    required = {
        "schema_version",
        "lane",
        "command",
        "selected",
        "passed",
        "failed",
        "skipped",
        "status",
        "exit_code",
    }
    for path in paths:
        if path.is_symlink():
            continue
        data = json.loads(path.read_text())
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != 1
            or not required <= data.keys()
        ):
            print("Skipping foreign summary: " + redact(str(path)))
            continue
        publish(Summary(**data))
        found += 1
    if not found:
        print("No saved verification results")
    return int(not found)
