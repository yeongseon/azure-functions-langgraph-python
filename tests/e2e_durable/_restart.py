"""Bounded restart observations; command output and HTTP bodies never enter evidence."""

from __future__ import annotations

from collections.abc import Callable
import json
import shlex
import subprocess
import time
from typing import Any

import requests

HOLD_SECONDS = 240
TERMINAL_WAIT_SECONDS = 1200
TERMINAL = {"success", "error", "cancelled"}


def classify(ev: dict[str, Any], terminal: bool) -> str:
    """Classify marker observations, not the mere return of a management command."""
    markers = ev["markers"]
    starts = markers["started"]
    finishes = markers["activity-finished"]
    if not starts:
        return "D"
    first = starts[0]["process_tag"]
    completed = {event["process_tag"] for event in finishes}
    if len(starts) == 1 and completed == {first}:
        return "A"
    if (
        len(ev["attempts"]) >= 2
        and first not in completed
        and any(event["process_tag"] != first for event in starts[1:])
        and completed & {event["process_tag"] for event in starts[1:]}
    ):
        return "B" if ev["effects_applied"] == 1 else "D"
    if not terminal and not finishes:
        return "C"
    return "D"


def command_observation(command: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(  # noqa: S603
            shlex.split(command), capture_output=True, text=True, timeout=90, check=False
        )
        return {
            "returncode": result.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired:
        return {"timed_out": True, "duration_seconds": round(time.monotonic() - started, 3)}


def restart_probe(
    mode: str,
    commands: dict[str, str],
    callbacks: dict[str, Callable[..., Any]],
) -> None:
    """Persist evidence before assertions, including stuck and failed-control outcomes."""
    origin = time.monotonic()
    origin_ms = int(time.time() * 1000)
    data: dict[str, Any] = {
        "mode": mode,
        "hold_seconds": HOLD_SECONDS,
        "terminal_wait_seconds": TERMINAL_WAIT_SECONDS,
        "restart_command_time_ms": origin_ms,
        "status_before_restart": callbacks["get"]()["status"],
        "commands": [],
        "statuses": [],
        "markers_before_restart": callbacks["evidence"]()["markers"],
    }

    def persist() -> None:
        callbacks["record"](data)

    def elapsed() -> float:
        return round(time.monotonic() - origin, 3)

    def control(name: str) -> None:
        item = {"operation": name, "at_seconds": elapsed()}
        data["commands"].append(item)
        persist()
        item.update(command_observation(commands[name]))
        item["returned_at_seconds"] = elapsed()
        persist()

    persist()
    hold_events = data["markers_before_restart"]["hold-started"]
    assert hold_events and not data["markers_before_restart"]["hold-finished"]
    assert origin_ms - hold_events[-1]["time_ms"] < 30000, "Hold observation too old"
    if mode == "restart":
        control("restart")
    else:
        try:
            control("stop")
            deadline = time.monotonic() + 90
            data["app_states"] = []
            while time.monotonic() < deadline:
                try:
                    result = subprocess.run(  # noqa: S603
                        shlex.split(commands["state"]),
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=False,
                    )
                    state = result.stdout.strip() if result.returncode == 0 else "unavailable"
                except subprocess.TimeoutExpired:
                    state = "unavailable"
                state = state if state in {"Running", "Stopped"} else "unavailable"
                data["app_states"].append({"at_seconds": elapsed(), "state": state})
                persist()
                if state == "Stopped":
                    data["stopped_confirmed"] = True
                    break
                time.sleep(3)
        finally:
            control("start")

    last: dict[str, Any] = {}
    deadline = origin + TERMINAL_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            last = callbacks["get"]()
            status = last["status"]
        except (requests.RequestException, AssertionError):
            status = "unavailable"
        data["statuses"].append({"at_seconds": elapsed(), "status": status})
        persist()
        if status in TERMINAL:
            data["time_to_terminal_seconds"] = elapsed()
            break
        time.sleep(5)
    terminal = last.get("status") in TERMINAL
    data["final_status"] = last.get("status", "unavailable")
    data["final_activity_status"] = (last.get("output") or {}).get("activity_status")
    data["observation_seconds"] = elapsed()
    try:
        ev = callbacks["evidence"]()
    except (requests.RequestException, AssertionError):
        data["classification"] = "D"
        data["evidence_unavailable"] = True
        persist()
        raise AssertionError("Restart evidence endpoint unavailable; inspect cleanup") from None
    data.update(
        {
            "classification": classify(ev, terminal),
            "activity_attempts": len(ev["attempts"]),
            "activity_started": len(ev["started"]),
            "activity_rejected": len(ev["rejected"]),
            "activity_completions": len(ev["completions"]),
            "graph_node_entries": len(ev["node_entries"]),
            "hold_finished": len(ev["hold_finished"]),
            "effect_calls": len(ev["effect_calls"]),
            "effects_applied": ev["effects_applied"],
            "markers": {
                kind: [
                    {
                        "process_tag": event["process_tag"],
                        "seconds_from_restart": round((event["time_ms"] - origin_ms) / 1000, 3),
                    }
                    for event in events
                ]
                for kind, events in ev["markers"].items()
            },
        }
    )
    persist()
    assert terminal, "Orchestration stuck beyond finite restart observation bound"
    assert ev["effects_applied"] <= 1, "idempotency key failed to suppress a duplicate effect"
    assert "deliberate e2e failure" not in json.dumps(last)
    error = (last.get("output") or {}).get("error")
    if error and data["final_activity_status"] == "error":
        assert error == {"code": "run_failed", "message": "The run failed."}
    assert all(item.get("returncode") == 0 for item in data["commands"]), "Control command failed"
    if mode == "stop_start":
        assert data.get("stopped_confirmed"), "App never reached Stopped before start"
