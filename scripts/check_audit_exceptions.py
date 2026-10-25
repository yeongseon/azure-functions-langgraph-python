#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re

_ENTRY = re.compile(
    r"^(?P<identifier>CVE-\d{4}-\d{4,}|GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})"
    r"\s+#\s+(?P<reason>[^#]+?)\s+#\s+owner\s+[^#\s]+"
    r"\s+#\s+expires\s+(?P<expiry>\d{4}-\d{2}-\d{2})$"
)


@dataclass(frozen=True, slots=True)
class AuditExceptionError(Exception):
    line_number: int
    reason: str

    def __str__(self) -> str:
        return f"line {self.line_number}: {self.reason}"


def parse_exceptions(path: Path, *, today: date | None = None) -> tuple[str, ...]:
    current_date = today or date.today()
    identifiers: list[str] = []
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENTRY.fullmatch(line)
        if match is None:
            raise AuditExceptionError(
                line_number=line_number,
                reason="expected 'ID # reason # owner NAME # expires YYYY-MM-DD'",
            )
        try:
            expiry = date.fromisoformat(match.group("expiry"))
        except ValueError as error:
            raise AuditExceptionError(line_number, "invalid expiry date") from error
        if expiry <= current_date:
            raise AuditExceptionError(
                line_number=line_number,
                reason=f"exception expired on {expiry.isoformat()}",
            )
        if (expiry - current_date).days > 90:
            raise AuditExceptionError(line_number, "expiry exceeds 90 days")
        if match.group("identifier") in identifiers:
            raise AuditExceptionError(line_number, "duplicate advisory")
        identifiers.append(match.group("identifier"))
    return tuple(identifiers)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--format", choices=("lines", "pip-audit"), default="lines")
    args = parser.parse_args()
    identifiers = parse_exceptions(args.path)
    if args.format == "pip-audit":
        print(" ".join(f"--ignore-vuln {identifier}" for identifier in identifiers))
    else:
        print("\n".join(identifiers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
