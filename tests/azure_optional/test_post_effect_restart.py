"""Opt-in post-effect/pre-checkpoint restart; requires the fixture's 240 s effect hold.

Run only with E2E_DURABLE_POST_EFFECT_PROBE=1 and E2E_POST_EFFECT_HOLD_SECONDS=240
on the owned deployed app. The normal dispatch lane does not select this probe.
"""

import os
import time
import uuid

import pytest

from tests.e2e_durable._restart import classify, command_observation
from tests.e2e_durable.test_durable_e2e import (
    BASE_URL,
    RESTART_CMD,
    _create,
    _evidence_for,
    _get,
    _record,
    warm_host,  # noqa: F401
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.e2e_durable,
    pytest.mark.skipif(
        not BASE_URL or not RESTART_CMD or os.environ.get("E2E_DURABLE_POST_EFFECT_PROBE") != "1",
        reason="post-effect Azure probe not explicitly configured",
    ),
]


def test_effect_is_suppressed_when_interrupted_activity_is_redelivered() -> None:
    # Given the effect is durable but the effect node has not returned to checkpoint it.
    run_id = f"d-effect-{uuid.uuid4().hex[:12]}"
    _create(run_id)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        before = _evidence_for(run_id)
        if before["effects_applied"] == 1 and not before["completions"]:
            break
        time.sleep(2)
    else:
        pytest.fail("post-effect interruption window was not observed")
    assert _get(run_id)["status"] == "running"
    origin = time.monotonic()
    origin_ms = int(time.time() * 1000)
    assert origin_ms - before["markers"]["effect-call"][-1]["time_ms"] < 30000
    _record("post_effect_before", before)

    # When the owned host is restarted during that window.
    control = command_observation(RESTART_CMD)
    _record("post_effect_control", control)
    assert control.get("returncode") == 0, "owned-app restart failed"
    statuses: list[str] = []
    deadline = origin + 1200
    while time.monotonic() < deadline:
        try:
            final = _get(run_id)
        except AssertionError:
            time.sleep(5)
            continue
        status = final["status"]
        if not statuses or statuses[-1] != status:
            statuses.append(status)
        if status in {"success", "error", "cancelled"}:
            break
        time.sleep(5)
    else:
        _record("post_effect_timeout", {"statuses": statuses})
        pytest.fail("post-effect run not terminal within 1200 seconds")

    # Then record actual redelivery before interpreting suppression, not command success.
    after = _evidence_for(run_id)
    _record(
        "post_effect_restart",
        {
            "statuses": statuses,
            "classification": classify(after, True),
            "seconds_to_terminal": round(time.monotonic() - origin, 3),
            "final_status": final["status"],
            "effect_applied_result": final.get("output", {})
            .get("result", {})
            .get("effect_applied"),
            "evidence": after,
        },
    )
    assert final["status"] == "success"
    assert after["effects_applied"] == 1
    # A graceful drain is inconclusive for this particular missing window, not a pass.
    assert classify(after, True) == "B", "different-process redelivery was not demonstrated"
    assert len(after["effect_calls"]) >= 2
    assert final["output"]["result"]["effect_applied"] is False
