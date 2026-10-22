"""Audit a resolved candidate wheel profile in a disposable, isolated environment."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
from importlib.metadata import distributions
from pathlib import Path
import subprocess
import sys
import tempfile

from scripts.check_audit_exceptions import AuditExceptionError, parse_exceptions
from tools.runtime_audit import classify_audit
from tools.security_report import Check, Status, write_report

PROFILES = (
    "base",
    "azure-blob",
    "azure-table",
    "azure-identity",
    "postgres",
    "sqlite",
    "cosmos",
    "otel",
    "streaming",
    "durable",
    "openapi",
)


def audit(wheel: Path, profile: str, temporary: Path) -> Check:
    result = Check("runtime-audit", (profile,), Status.TOOL_ERROR, None)
    try:
        ignores = parse_exceptions(Path(".github/pip-audit-ignore.txt"))
        environment = temporary / "runtime"
        subprocess.run(
            [sys.executable, "-m", "venv", str(environment)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        python = environment / "bin/python"
        target = environment / (
            f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        )
        spec = str(wheel.resolve()) + (f"[{profile}]" if profile != "base" else "")
        install = subprocess.run(
            [str(python), "-m", "pip", "install", spec],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if install.returncode:
            return replace(
                result,
                exit_status=install.returncode,
                message="Candidate profile installation failed; diagnostics discarded.",
            )
        packages = sorted(
            f"{dist.metadata['Name']}=={dist.version}"
            for dist in distributions(path=[str(target)])
            if dist.metadata["Name"].lower().replace("_", "-")
            not in {"azure-functions-langgraph", "pip", "setuptools"}
        )
        if not packages:
            return replace(result, status=Status.INCOMPLETE, message="Zero runtime dependencies.")
        requirements = temporary / "requirements.txt"
        requirements.write_text("\n".join(packages) + "\n", encoding="utf-8")
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "-r",
                str(requirements),
                "--no-deps",
                "--disable-pip",
                "--format",
                "json",
                "--progress-spinner",
                "off",
                *(arg for advisory in ignores for arg in ("--ignore-vuln", advisory)),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        check = classify_audit(process.stdout, process.returncode)
        return replace(
            check,
            scope=(profile,),
            exceptions_applied=ignores,
            message="Candidate SHA256: " + hashlib.sha256(wheel.read_bytes()).hexdigest(),
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        AuditExceptionError,
        ValueError,
    ):
        return replace(result, message="Audit tool, installation or exception policy failed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--output", type=Path, default=Path("security-results/runtime-audit"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="candidate-audit-") as temporary:
        check = audit(args.wheel, args.profile, Path(temporary))
    return write_report([check], args.output / args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
