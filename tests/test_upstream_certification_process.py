from pathlib import Path
import signal
import subprocess
import sys

import pytest

from tests.upstream_certification_process import run_certification


@pytest.mark.parametrize("stubborn", [False, True])
def test_certification_reaps_processes_when_timeout_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stubborn: bool
) -> None:
    # Given real processes synchronized by a readiness pipe, without host tools.
    cleaned = tmp_path / "nested-cleaned"
    nested = (
        "import signal, sys\n"
        "from pathlib import Path\n"
        f"def stop(*args):\n    Path({str(cleaned)!r}).touch()\n    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "print('ready', flush=True)\n"
        "signal.pause()\n"
    )
    script = (
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "signal.pause()\n"
        if stubborn
        else "import signal, subprocess, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"child = subprocess.Popen([sys.executable, '-c', {nested!r}])\n"
        "child.wait()\n"
    )
    communicate = subprocess.Popen.communicate
    processes: list[subprocess.Popen[bytes]] = []

    def expire_when_ready(
        process: subprocess.Popen[bytes], input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes | None]:
        if not processes:
            processes.append(process)
            assert process.stdout is not None
            assert process.stdout.readline() == b"ready\n"
            raise subprocess.TimeoutExpired(process.args, 300)
        return communicate(process, input=input, timeout=0 if stubborn and timeout else timeout)

    monkeypatch.setattr(subprocess.Popen, "communicate", expire_when_ready)
    # When the certifier times out after its runner is ready.
    with pytest.raises(subprocess.TimeoutExpired):
        run_certification([sys.executable, "-c", script], timeout=300)
    # Then cooperative descendants unwind, and a stubborn direct child is killed and reaped.
    assert processes[0].returncode == (-signal.SIGKILL if stubborn else 0)
    assert cleaned.exists() is (not stubborn)


@pytest.mark.parametrize("returncode", [0, 7])
def test_certification_preserves_command_result_when_it_finishes(returncode: int) -> None:
    # Given a command that completes without host tools.
    command = [sys.executable, "-c", f"raise SystemExit({returncode})"]
    # When running it, then success returns and failure retains its exit status.
    if returncode:
        with pytest.raises(subprocess.CalledProcessError) as error:
            run_certification(command, timeout=10)
        assert error.value.returncode == returncode
    else:
        run_certification(command, timeout=10)
