from contextlib import nullcontext
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("host_runner", ROOT / "tools/run_host_smoke.py")
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.mark.parametrize("fail", [False, True])
def test_owned_process_is_reaped_after_success_or_failure(tmp_path: Path, fail: bool) -> None:
    # Given: a child owned by this test, with no shared service or port.
    guard = pytest.raises(RuntimeError) if fail else nullcontext()
    # When: the protected operation returns or raises.
    with guard:
        with runner.child(
            [sys.executable, "-c", "import signal; signal.pause()"],
            tmp_path / "child.log",
            tmp_path,
        ) as process:
            if fail:
                raise RuntimeError("deliberate failure")
    # Then: the process is reaped in either case.
    assert process.poll() is not None


def test_interrupt_reaps_owned_process(tmp_path: Path) -> None:
    # Given: an owned child and the runner's signal handler.
    previous = signal.signal(signal.SIGTERM, runner.interrupted)
    try:
        # When: SIGTERM interrupts a running operation.
        with pytest.raises(KeyboardInterrupt):
            with runner.child(
                [sys.executable, "-c", "import signal; signal.pause()"],
                tmp_path / "interrupt.log",
                tmp_path,
            ) as process:
                os.kill(os.getpid(), signal.SIGTERM)
        # Then: cleanup finishes before interruption propagates.
        assert process.poll() is not None
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.parametrize(
    "diagnostic",
    [
        "0 functions",
        "Worker failed",
        "Host startup operation has been canceled",
        "Error indexing method",
    ],
)
def test_existing_host_failure_patterns_remain_fatal(diagnostic: str) -> None:
    # Given / When: a startup/indexing failure in an owned host log.
    failure = runner.HOST_FAILURE.search(diagnostic)
    # Then: successful HTTP traffic cannot hide that failure.
    assert failure is not None


def test_missing_host_prerequisite_fails_without_starting_services(tmp_path: Path) -> None:
    # Given: no host or emulator executable on PATH.
    # When: the real CLI runs.
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/run_host_smoke.py")],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    # Then: it fails before acquiring resources.
    assert result.returncode == 1
    assert "Missing prerequisite: func" in result.stderr
