"""Bound the opt-in certifier and allow nested host runners to unwind on timeout."""

from contextlib import suppress
import os
import signal
import subprocess


def run_certification(command: list[str], timeout: float) -> None:
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
    ) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                # Inherited pipes stay open until the nested runner finishes its cleanup,
                # even if the direct certifier has already exited on SIGTERM.
                process.communicate(timeout=30)
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
        print(output.decode(errors="replace"))
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, output=output)
