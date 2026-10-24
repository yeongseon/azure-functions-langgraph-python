#!/usr/bin/env python3
"""Fail unless a pytest JUnit report proves that the required tests ran.

    python tools/check_junit.py REPORT --area NAME=MARKER [--area NAME=MARKER ...]

Valid means: at least one test; no failure, error or skip; suite counters that match the recorded
testcases; and, for every area, at least one testcase whose classname or file contains MARKER.
Pytest's exit status alone does not prove that the required areas ran: an unavailable service turns
tests into skips, which still exit 0, and a selection that leaves an area out exits 0 as well.
Pytest itself only flags an empty selection, with exit 5. At least one area is required so that a
lane cannot drop its area check by omission.

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


def _count(cases: list[ET.Element], outcome: str) -> int:
    return sum(case.find(outcome) is not None for case in cases)


def check(report: Path, areas: Sequence[tuple[str, str]]) -> dict[str, int]:
    """Return the totals of a valid report; raise JUnitError describing the first problem.

    A malformed or unreadable report raises KeyError, ValueError, ET.ParseError or OSError instead.
    """
    # The report is written by the pytest run of this same CI job, not received from outside, and
    # the tool stays stdlib-only so it runs in lanes that install no extra XML package.
    # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise JUnitError("JUnit report contains no testsuite elements")
    for suite in suites:
        suite_cases = list(suite.iter("testcase"))
        declared_tests = int(suite.attrib["tests"])
        if declared_tests != len(suite_cases):
            raise JUnitError(
                "JUnit testsuite test count does not match testcase descendants: "
                f"reported={declared_tests}, actual={len(suite_cases)}"
            )
        for counter, outcome in OUTCOMES.items():
            declared = int(suite.attrib[counter])
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
        " ".join((case.attrib.get("classname", ""), case.attrib.get("file", ""))) for case in cases
    }
    missing = [name for name, marker in areas if not any(marker in item for item in identities)]
    if missing:
        raise JUnitError(f"missing expected integration areas: {missing}")
    return totals


def _area(spec: str) -> tuple[str, str]:
    name, _, marker = spec.partition("=")
    name, marker = name.strip(), marker.strip()
    if not name or not marker:
        raise argparse.ArgumentTypeError(f"invalid area {spec!r}: expected NAME=MARKER")
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
        metavar="NAME=MARKER",
        help="a testcase's classname or file must contain MARKER; repeat for each area",
    )
    args = parser.parse_args(argv)
    try:
        totals = check(args.report, args.areas)
    except JUnitError as exc:
        print(f"check_junit: {exc}", file=sys.stderr)
        return 1
    except (KeyError, ValueError, ET.ParseError, OSError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        print(f"check_junit: invalid or unreadable JUnit report: {reason}", file=sys.stderr)
        return 1
    print(f"Validated {totals['tests']} no-skip integration tests across all expected areas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
