from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile

from tools.verification_evidence import collect
from tools.verification_lanes import TABLE_AREAS


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lg-junit-policy-") as temporary:
        directory = Path(temporary)
        shutil.copyfile(sys.argv[1], directory / "junit.xml")
        summary = collect(directory, ["pytest"], 0, expected=TABLE_AREAS)
        if summary.status != "PASS":
            totals = {
                "tests": summary.selected,
                "failures": summary.failed,
                "errors": 0,
                "skipped": summary.skipped,
            }
            raise ValueError(str(totals) + "; " + summary.reason)
        print(f"Validated {summary.selected} no-skip integration tests across all expected areas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
