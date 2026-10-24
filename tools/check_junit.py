#!/usr/bin/env python3
"""Fail unless a pytest JUnit report proves that the required tests ran.

    python tools/check_junit.py REPORT --area NAME[=MARKER] [--area NAME[=MARKER] ...]

Valid means: at least one test; no failure, error or skip; suite counters that match the recorded
testcases; and, for every area, at least one testcase whose classname or file contains MARKER
(case-insensitive, defaults to NAME). Pytest's own exit code proves none of this: an unavailable
service turns tests into skips, and an empty or mis-scoped selection also exits 0. At least one
area is required so that a lane cannot drop its area check by omission.

Exit code 0 = valid, 1 = invalid or unreadable, 2 = usage error. Stdlib-only like the other
``tools/`` checks, and explicit checks instead of ``assert`` so ``python -O`` cannot disable it.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

OUTCOMES = {"failures": "failure", "errors": "error", "skipped": "skipped"}


class JUnitError(Exception):
    """The report does not prove that the required tests ran."""


def _declared(suite: ET.Element, counter: str) -> int:
    try:
        return int(suite.attrib[counter])
    except (KeyError, ValueError):
        raise JUnitError(f"JUnit testsuite has no integer {counter!r} counter") from None


def _count(cases: list[ET.Element], outcome: str) -> int:
    return sum(case.find(outcome) is not None for case in cases)


def check(report: Path, areas: dict[str, str]) -> dict[str, int]:
    """Return the totals of a valid report; raise JUnitError describing the first problem."""
    # The report is written by the pytest run of this same CI job, not received from outside, and
    # the tool stays stdlib-only so it runs in lanes that install no extra XML package.
    # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise JUnitError("JUnit report contains no testsuite elements")
    for suite in suites:
        suite_cases = list(suite.iter("testcase"))
        declared_tests = _declared(suite, "tests")
        if declared_tests != len(suite_cases):
            raise JUnitError(
                "JUnit testsuite test count does not match testcase descendants: "
                f"reported={declared_tests}, actual={len(suite_cases)}"
            )
        for counter, outcome in OUTCOMES.items():
            declared = _declared(suite, counter)
            actual = _count(suite_cases, outcome)
            if declared != actual:
                raise JUnitError(
                    "JUnit summary does not match testcase outcomes: "
                    f"counter={counter}, reported={declared}, actual={actual}"
                )

    cases = list(root.iter("testcase"))
    totals = {"tests": len(cases)}
    totals.update({counter: _count(cases, outcome) for counter, outcome in OUTCOMES.items()})
    if totals["tests"] == 0:
        raise JUnitError(f"no tests ran: {totals}")
    if totals["failures"] or totals["errors"] or totals["skipped"]:
        raise JUnitError(f"tests failed, errored or were skipped: {totals}")

    identities = {
        " ".join((case.attrib.get("classname", ""), case.attrib.get("file", ""))).lower()
        for case in cases
    }
    missing = sorted(
        name for name, marker in areas.items() if not any(marker in item for item in identities)
    )
    if missing:
        raise JUnitError(f"missing expected integration areas: {missing}")
    return totals


def _area(spec: str) -> tuple[str, str]:
    name, separator, marker = spec.partition("=")
    name, marker = name.strip(), (marker if separator else name).strip().lower()
    if not name or not marker:
        raise argparse.ArgumentTypeError(f"invalid area {spec!r}: expected NAME or NAME=MARKER")
    return name, marker


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail unless a pytest JUnit report proves that the required tests ran."
    )
    parser.add_argument("report", type=Path, help="JUnit XML written by pytest --junitxml")
    parser.add_argument(
        "--area",
        dest="areas",
        action="append",
        required=True,
        type=_area,
        metavar="NAME[=MARKER]",
        help="expected area; repeat for each. MARKER defaults to NAME",
    )
    args = parser.parse_args(argv)
    try:
        totals = check(args.report, dict(args.areas))
    except JUnitError as exc:
        print(f"check_junit: {exc}", file=sys.stderr)
        return 1
    except (ET.ParseError, OSError) as exc:
        print(
            f"check_junit: cannot read {args.report}: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 1
    print(f"Validated {totals['tests']} no-skip integration tests across all expected areas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
