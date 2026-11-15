"""Deterministic human and machine views of recorded release evidence."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import json

from tools.release_audit_model import Snapshot, VersionAudit


def exit_code(audits: list[VersionAudit]) -> int:
    return int(any(a.verdict == "DEFECT" for a in audits))


def render(snapshot: Snapshot, audits: list[VersionAudit], format_name: str) -> str:
    counts = Counter(a.verdict for a in audits)
    summary = (
        " ".join(
            f"{name}={counts[name]}" for name in ("PUBLISHED", "EXCEPTED", "PENDING", "DEFECT")
        )
        + f" exit={exit_code(audits)}"
    )
    if format_name == "json":
        return json.dumps(
            {
                "collected_at": snapshot.collected_at,
                "versions": [asdict(a) for a in audits],
                "notes": snapshot.notes,
                "summary": summary,
            },
            sort_keys=True,
            indent=2,
        )
    markdown = format_name == "markdown"

    def literal(value: str) -> str:
        return (
            "".join("\\" + c if c in "\\`*_[]<>|#!~" else c for c in value) if markdown else value
        )

    def link(label: str, url: str) -> str:
        safe = url.startswith("https://") and not any(c.isspace() or c in ")<>" for c in url)
        if markdown and safe:
            return f"[{literal(label)}]({url.replace('|', '%7C')})"
        return literal(f"{label} {url}" if url else label)

    rows = [
        ["version", "GitHub Release", "PyPI files", "publish run", "verdict", "detail/findings"]
    ]
    for audit in audits:
        findings = []
        for finding in audit.findings:
            detail = literal(
                f"{finding.code}{' (covered)' if finding.covered else ''}: {finding.message}"
            )
            detail += " " + " ".join(link("evidence", url) for url in finding.links)
            findings.append(detail)
        findings.extend("note: " + literal(note) for note in audit.notes)
        rows.append(
            [
                literal(audit.version),
                link("exists" if audit.release_url else "absent", audit.release_url),
                link(str(len(audit.file_urls)), audit.file_urls[0] if audit.file_urls else ""),
                link(audit.run_conclusion, audit.run_url),
                literal(audit.verdict),
                "; ".join(findings) or "consistent",
            ]
        )
    lines = [f"collected_at: {snapshot.collected_at}"]
    if markdown:
        for i, row in enumerate(rows):
            lines.append("| " + " | ".join(cell.replace("|", "&#124;") for cell in row) + " |")
            if i == 0:
                lines.append("| " + " | ".join("---" for _ in row) + " |")
    else:
        widths = [max(len(row[i]) for row in rows) for i in range(5)]
        lines.extend(
            "  ".join(cell.ljust(widths[i]) if i < 5 else cell for i, cell in enumerate(row))
            for row in rows
        )
    lines.extend("note: " + literal(note) for note in sorted(snapshot.notes))
    lines.append(summary)
    return "\n".join(lines)
