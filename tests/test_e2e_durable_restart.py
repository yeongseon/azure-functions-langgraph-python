"""Exercise evidence persistence and bounded control failures without Azure."""

from collections.abc import Callable
from copy import deepcopy
import subprocess
import time
from typing import Any

import pytest

from tests.e2e_durable import _restart


@pytest.mark.parametrize("mode", ["restart", "stop_start"])
@pytest.mark.parametrize("stuck", [False, True])
def test_restart_records_markers_and_stuck_evidence_before_asserting(
    monkeypatch: pytest.MonkeyPatch, mode: str, stuck: bool
) -> None:
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "time", lambda: 1000 + now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    operations: list[str] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        operations.append(args[0])
        return subprocess.CompletedProcess(args, 0, stdout="Stopped\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    ev = {
        "attempts": ["old", "new"],
        "started": ["old", "new"],
        "rejected": [],
        "completions": [] if stuck else ["new"],
        "node_entries": ["old", "new"],
        "hold_finished": [],
        "effect_calls": [] if stuck else ["new"],
        "effects_applied": 0 if stuck else 1,
        "markers": {
            "attempt": [
                {"time_ms": 999000, "process_tag": "old"},
                {"time_ms": 1300000, "process_tag": "new"},
            ],
            "started": [
                {"time_ms": 999000, "process_tag": "old"},
                {"time_ms": 1300000, "process_tag": "new"},
            ],
            "hold-started": [{"time_ms": 999000, "process_tag": "old"}],
            "hold-finished": [],
            "activity-finished": [] if stuck else [{"time_ms": 1540000, "process_tag": "new"}],
        },
    }
    saved: list[dict[str, Any]] = []
    reads = [0]

    def get() -> dict[str, Any]:
        reads[0] += 1
        return {"status": "running" if reads[0] == 1 or stuck else "success"}

    callbacks: dict[str, Callable[..., Any]] = {
        "get": get,
        "evidence": lambda: ev,
        "record": lambda data: saved.append(deepcopy(data)),
    }
    commands = {name: name for name in ("restart", "stop", "start", "state")}
    if stuck:
        with pytest.raises(AssertionError, match="stuck"):
            _restart.restart_probe(mode, commands, callbacks)
    else:
        _restart.restart_probe(mode, commands, callbacks)
    assert saved[-1]["classification"] == ("C" if stuck else "B")
    assert saved[-1]["effects_applied"] == (0 if stuck else 1)
    assert saved[-1]["markers"]["started"][0] == {
        "process_tag": "old",
        "seconds_from_restart": -1.0,
    }
    assert operations == (["restart"] if mode == "restart" else ["stop", "state", "start"])
    if mode == "stop_start":
        assert saved[-1]["stopped_confirmed"] is True
    assert saved[-1]["statuses"]


def test_command_timeout_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("secret command", 90, output="secret output")

    monkeypatch.setattr(subprocess, "run", run)
    observed = _restart.command_observation("stop")
    assert observed["timed_out"] is True
    assert set(observed) == {"timed_out", "duration_seconds"}
