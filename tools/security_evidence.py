"""Run an explicit Bandit inventory, including exact reviewed inline suppressions."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date
from importlib.metadata import version
import json
from pathlib import Path
import re
import subprocess
import sys
import tokenize

from tools.security_exceptions import parse_exceptions
from tools.security_report import (
    Check,
    EvidenceError,
    Finding,
    Status,
    identifier,
    integer,
    load,
    mapping,
    sequence,
    text,
    write_report,
)

SCOPE = ("src", "examples", "scripts", "tools", "tests/host_smoke")
FILE_FLOOR = 100


def classify_bandit(raw: str, exit_status: int, *, floor: int = FILE_FLOOR) -> Check:
    result = Check("bandit", SCOPE, Status.TOOL_ERROR, exit_status)
    try:
        data = load(raw)
        metrics = mapping(data["metrics"])
        totals = mapping(metrics["_totals"])
        files = tuple(identifier(f.removeprefix("./")) for f in metrics if f != "_totals")
        counts = {
            "files": len(files),
            "loc": integer(totals.get("loc", 0)),
            "errors": len(sequence(data["errors"])),
        }
        findings: list[Finding] = []
        for entry in sequence(data["results"]):
            item = mapping(entry)
            severity, confidence = text(item["issue_severity"]), text(item["issue_confidence"])
            if severity not in {"LOW", "MEDIUM", "HIGH"} or confidence not in {
                "LOW",
                "MEDIUM",
                "HIGH",
            }:
                raise EvidenceError("unknown classification")
            findings.append(
                Finding(
                    identifier(item["test_id"]),
                    identifier(text(item["filename"]).removeprefix("./")),
                    integer(item["line_number"]),
                    severity,
                    confidence,
                )
            )
            for key in (f"severity.{severity}", f"confidence.{confidence}"):
                counts[key] = counts.get(key, 0) + 1
        result = replace(result, counts=counts, findings=tuple(findings))
        if exit_status not in {0, 1} or counts["errors"]:
            return replace(result, message="Scanner failure; scan unusable.")
        if len(files) < floor or not counts["loc"]:
            return replace(result, status=Status.INCOMPLETE, message="Below scan file/LOC floor.")
        if bool(findings) != (exit_status == 1):
            return replace(result, message="Scanner exit and findings disagree.")
        return replace(result, status=Status.FAIL if findings else Status.PASS)
    except (ValueError, KeyError, TypeError):
        return replace(result, message="Invalid Bandit JSON; raw diagnostics discarded.")


def run_bandit() -> Check:
    try:
        process = subprocess.run(
            [sys.executable, "-m", "bandit", "-q", "-r", *SCOPE, "-f", "json", "--ignore-nosec"],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        result = classify_bandit(process.stdout, process.returncode)
        if result.status == Status.TOOL_ERROR:
            return result
        expected = {p.as_posix() for surface in SCOPE for p in Path(surface).rglob("*.py")}
        scanned = set(mapping(load(process.stdout)["metrics"])) - {"_totals"}
        if {p.removeprefix("./") for p in scanned} != expected or any(
            not Path(surface).is_dir() for surface in SCOPE
        ):
            return replace(result, status=Status.INCOMPLETE, message="Inventory mismatch.")
        policy = load(Path("security/bandit-policy.json").read_text(encoding="utf-8"))
        rules = parse_exceptions(json.dumps(policy["exceptions"]), date.today())
        inline = parse_exceptions(json.dumps(policy["inline_suppressions"]), date.today())
        observed: set[tuple[str, str, int]] = set()
        for filename in expected:
            with tokenize.open(filename) as stream:
                for token in tokenize.generate_tokens(stream.readline):
                    if token.type == tokenize.COMMENT and "nosec" in token.string:
                        ids = re.findall(r"B\d{3}", token.string) or ["blanket"]
                        observed.update((rule, filename, token.start[0]) for rule in ids)
        if observed != {(r.id, r.file, r.line) for r in inline}:
            return replace(result, status=Status.INCOMPLETE, message="Inline policy mismatch.")
        allowed = {(r.id, r.file, r.line) for r in (*rules, *inline)}
        blocking = [f for f in result.findings if (f.id, f.file, f.line) not in allowed]
        applied = tuple(
            f"{f.id}:{f.file}:{f.line}"
            for f in result.findings
            if (f.id, f.file, f.line) in allowed
        )
        return replace(
            result,
            status=Status.FAIL if blocking else Status.PASS,
            suppressions=len(observed),
            exceptions_applied=applied,
            counts={**result.counts, "blocking": len(blocking)},
            tool_versions={"bandit": version("bandit")},
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
        ValueError,
        KeyError,
        TypeError,
        tokenize.TokenError,
        SyntaxError,
    ):
        return Check(
            "bandit",
            SCOPE,
            Status.TOOL_ERROR,
            None,
            message="Bandit execution or policy failed; raw diagnostics discarded.",
        )


def semgrep_evidence(outcome: str) -> Check:
    # The pinned action exposes no stable JSON output contract or analyzed-file count.
    return Check(
        "semgrep",
        ("p/python", *SCOPE),
        Status.TOOL_ERROR if outcome == "failure" else Status.INCOMPLETE,
        1 if outcome == "failure" else None,
        counts={"inventory_files": sum(len(list(Path(p).rglob("*.py"))) for p in SCOPE)},
        tool_versions={"action": "713efdd345f3035192eaa63f56867b88e63e4e5d"},
        message="Reporting only: action outcome="
        + (outcome if outcome in {"success", "failure", "skipped", "cancelled"} else "unknown")
        + "; analyzed files and findings unknown. Existing action remains blocking.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=("bandit", "semgrep"))
    parser.add_argument("--outcome", default="")
    parser.add_argument("--output", type=Path, default=Path("security-results"))
    args = parser.parse_args()
    check = run_bandit() if args.check == "bandit" else semgrep_evidence(args.outcome)
    code = write_report([check], args.output)
    return code if args.check == "bandit" else 0


if __name__ == "__main__":
    raise SystemExit(main())
