"""Classify pip-audit 2.10.1 output without downgrading the all-advisory release gate."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from tools.security_report import (
    Check,
    Finding,
    Status,
    identifier,
    load,
    mapping,
    sequence,
    write_report,
)


def classify_audit(raw: str, exit_status: int) -> Check:
    result = Check("runtime-audit", ("candidate-wheel",), Status.TOOL_ERROR, exit_status)
    try:
        data = load(raw)
        dependencies = sequence(data["dependencies"])
        if exit_status not in {0, 1}:
            return result
        if not dependencies:
            return replace(result, status=Status.INCOMPLETE, message="Zero audited packages.")
        findings: list[Finding] = []
        skipped = 0
        ignored = 0
        for entry in dependencies:
            package = mapping(entry)
            name = identifier(package["name"])
            if "skip_reason" in package:
                skipped += 1
                continue
            identifier(package["version"])
            for entry in sequence(package["vulns"]):
                vulnerability = mapping(entry)
                if vulnerability.get("ignored") is True:
                    ignored += 1
                    continue
                findings.append(Finding(identifier(vulnerability["id"]), package=name))
        result = replace(
            result,
            counts={
                "packages": len(dependencies),
                "skipped": skipped,
                "advisories": len(findings),
                "excepted": ignored,
            },
            findings=tuple(findings),
            tool_versions={"pip-audit": "2.10.1"},
        )
        if skipped:
            return replace(result, status=Status.INCOMPLETE, message="Some packages not audited.")
        if bool(findings) != (exit_status == 1):
            return replace(result, message="Scanner exit and findings disagree.")
        return replace(result, status=Status.FAIL if findings else Status.PASS)
    except (ValueError, KeyError, TypeError):
        return replace(result, message="Missing or invalid audit JSON; raw diagnostics discarded.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--exit-status", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = args.report.read_text(encoding="utf-8")
        check = classify_audit(raw, args.exit_status)
    except OSError:
        check = Check(
            "runtime-audit",
            ("candidate-wheel",),
            Status.TOOL_ERROR,
            args.exit_status,
            message="Audit report missing.",
        )
    return write_report([check], args.output)


if __name__ == "__main__":
    raise SystemExit(main())
