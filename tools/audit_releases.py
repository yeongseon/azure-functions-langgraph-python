"""Read-only GitHub/PyPI release audit; python -m tools.audit_releases --help."""

from __future__ import annotations

import argparse
import binascii
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

from tools.release_audit_collect import collect
from tools.release_audit_evaluate import evaluate
from tools.release_audit_http import Fetcher, HTTPFetcher
from tools.release_audit_model import AuditError, timestamp
from tools.release_audit_parse import load
from tools.release_audit_registry import parse_registry
from tools.release_audit_render import exit_code, render
from tools.release_audit_snapshot import parse_snapshot

__all__ = ["collect", "evaluate", "main"]


def main(argv: Sequence[str] | None = None, *, fetcher: Fetcher | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".github/release-audit-exceptions.json",
    )
    parser.add_argument("--snapshot-in", type=Path, help="evaluate recorded facts without network")
    parser.add_argument("--snapshot-out", type=Path, help="save collected facts as JSON")
    parser.add_argument("--now", help="fixed timezone-aware ISO8601 evaluation clock")
    parser.add_argument("--format", choices=("table", "markdown", "json"), default="table")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--deadline", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        registry = parse_registry(load(args.registry.read_text(encoding="utf-8")))
        now = timestamp(args.now) if args.now else datetime.now(UTC)
        if args.snapshot_in:
            snapshot = parse_snapshot(load(args.snapshot_in.read_text(encoding="utf-8")))
        else:
            snapshot = collect(registry, fetcher or HTTPFetcher(args.timeout, args.deadline), now)
        if args.snapshot_out:
            args.snapshot_out.write_text(
                json.dumps(asdict(snapshot), sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
        audits = evaluate(snapshot, registry, now)
        print(render(snapshot, audits, args.format))
        return exit_code(audits)
    except (ValueError, KeyError, TypeError, OSError, binascii.Error) as exc:
        message = str(exc) if isinstance(exc, AuditError) else type(exc).__name__
        print(f"audit_releases: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
