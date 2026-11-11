"""Pure request/response contract helpers for the Durable real-Azure e2e (#811).

Deliberately free of ``requests`` and any Azure import so the offline suite can exercise them
against the real create/get handlers (``tests/test_e2e_durable_contracts.py``).
"""

from __future__ import annotations

from typing import Any

#: The public, sanitized error ``GET /runs/{id}`` returns for a failed Activity
#: (``get_run_impl`` never exposes the raw exception type or message).
PUBLIC_RUN_ERROR = {"code": "run_failed", "message": "The run failed."}


def create_body(
    run_id: str, *, hold: float = 0, thread_id: str | None = None, fail: bool = False
) -> dict[str, Any]:
    """Create-run body. Every run gets a unique default ``thread_id``.

    The e2e graph is compiled with a blob checkpointer, which requires a thread id; a run that
    omits it would fail before entering any node. Explicit ids are kept for contention tests.
    """
    return {
        "run_id": run_id,
        "thread_id": thread_id or f"t-{run_id}",
        "input": {"run_id": run_id, "hold_seconds": hold, "fail": fail},
    }


def check_create_response(body: dict[str, Any], run_id: str) -> None:
    """Required ``run_id``/``status``; the HTTP layer may add a ``request_id`` correlation field."""
    assert body.get("run_id") == run_id, body
    assert body.get("status") == "pending", body


def check_public_error(final: dict[str, Any]) -> None:
    """Assert the sanitized public error contract of a failed run."""
    assert final["status"] == "error", final
    assert final["output"]["activity_status"] == "error", final
    assert final["output"]["error"] == PUBLIC_RUN_ERROR, final


def check_success_evidence(ev: dict[str, Any]) -> int:
    """At-least-once delivery: >=1 attempt and exactly one applied effect. Returns attempts."""
    attempts = len(ev["attempts"])
    assert attempts >= 1, ev
    assert ev["effects_applied"] == 1, ev
    return attempts
