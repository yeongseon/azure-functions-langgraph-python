from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

from tools.verification_environment import clean, doctor
from tools.verification_evidence import Status, Summary, collect, publish, report, save
from tools.verification_lanes import execute, fresh
from tools.verification_process import run


def aggregate(lane: str, outcomes: list[Summary]) -> int:
    status: Status = "PASS"
    if any(s.status == "FAIL" for s in outcomes):
        status = "FAIL"
    elif not outcomes or any(s.status != "PASS" for s in outcomes):
        status = "INCOMPLETE"
    directory = Path("test-results") / lane
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "lanes.json").write_text(json.dumps([asdict(s) for s in outcomes], indent=2))
    summary = Summary(
        1,
        lane,
        ("make", lane),
        sum(s.selected for s in outcomes),
        sum(s.passed for s in outcomes),
        sum(s.failed for s in outcomes),
        sum(s.skipped for s in outcomes),
        status,
        int(status != "PASS"),
        scope="FULL_LOCAL" if lane == "verify" else "LOCAL_INTEGRATION",
        reason="; ".join(f"{s.lane}={s.status}" for s in outcomes),
    )
    publish(save(directory, summary))
    return summary.exit_code


def targets(lane: str, names: tuple[str, ...]) -> int:
    directory = Path("test-results") / lane
    fresh(directory)
    outcomes = []
    for target in names:
        path = Path("test-results") / target / "summary.json"
        path.unlink(missing_ok=True)
        code = run(["make", target], directory / "logs" / f"{target}.log", 1800)
        if path.exists():
            summary = Summary(**json.loads(path.read_text()))
            if code and summary.status == "PASS":
                summary = replace(summary, status="FAIL", exit_code=code)
        else:
            summary = Summary(
                1,
                target,
                ("make", target),
                int(target == "check"),
                int(target == "check" and code == 0),
                int(code != 0),
                0,
                ("FAIL" if code else "PASS") if target == "check" else "INCOMPLETE",
                code or int(target != "check"),
                reason="Required lane summary missing",
            )
        outcomes.append(summary)
    return aggregate(lane, outcomes)


def focused(lane: str, command: list[str]) -> int:
    directory = Path("test-results") / lane
    fresh(directory)
    code = run(command, directory / "logs/runner.log", 1200)
    summary = Summary(
        1,
        lane,
        tuple(command),
        1,
        int(code == 0),
        int(code != 0),
        0,
        "PASS" if code == 0 else "FAIL",
        code,
    )
    publish(save(directory, summary))
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "lane",
        choices=(
            "test",
            "table",
            "cosmos",
            "deterministic",
            "streaming-host",
            "integration",
            "verify",
            "azure-e2e",
            "report",
            "doctor",
            "clean",
            "focused",
            "record",
            "dependency",
        ),
    )
    parser.add_argument("--owned", action="store_true")
    args, extra = parser.parse_known_args()
    match args.lane:
        case "doctor":
            return doctor()
        case "clean":
            clean(Path.cwd())
            return 0
        case "report":
            return report(Path("test-results"))
        case "verify":
            return targets(
                "verify", ("check", "test", "integration", "wheel-smoke", "build", "docs")
            )
        case "integration":
            fresh(Path("test-results/integration"))
            outcomes = [execute(backend, [], owned=True) for backend in ("table", "cosmos")]
            for backend in ("table", "cosmos"):
                source = Path("test-results/integration") / backend / "junit.xml"
                if source.exists():
                    destination = Path("test-results/integration/junit") / f"{backend}-junit.xml"
                    destination.parent.mkdir(exist_ok=True)
                    destination.write_text(source.read_text())
            code = targets("local-runtime", ("deterministic", "host-smoke"))
            runtime = Summary(
                **json.loads(Path("test-results/local-runtime/summary.json").read_text())
            )
            outcomes.append(replace(runtime, exit_code=code))
            return aggregate("integration", outcomes)
        case "focused":
            return focused(extra[0], [sys.executable, *extra[1:]])
        case "record":
            directory = Path("test-results") / extra[0]
            summary = collect(directory, ["pytest", "installed-wheel"], int(extra[1]))
            publish(save(directory, summary))
            return summary.exit_code
        case _:
            return execute(args.lane, extra, owned=args.owned).exit_code


if __name__ == "__main__":
    raise SystemExit(main())
