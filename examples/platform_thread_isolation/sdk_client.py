"""Show same-thread continuity vs distinct-thread isolation with langgraph-sdk.

Run alongside ``func start`` in this directory:

    pip install langgraph-sdk
    python sdk_client.py

``run_demo`` takes any ``langgraph-sdk`` sync client, so the test suite drives
it through an in-process transport with no host and no network.
"""

from __future__ import annotations

from typing import Any

ASSISTANT_ID = "notebook_agent"


def _run(client: Any, thread_id: str, note: str) -> dict[str, Any]:
    result: dict[str, Any] = client.runs.wait(thread_id, ASSISTANT_ID, input={"note": note})
    return result


def run_demo(client: Any) -> dict[str, dict[str, Any]]:
    """Two runs on thread A, one run on thread B; return each thread's final values."""
    thread_a = client.threads.create()["thread_id"]
    thread_b = client.threads.create()["thread_id"]
    _run(client, thread_a, "alpha-1")
    _run(client, thread_b, "beta-1")
    _run(client, thread_a, "alpha-2")  # repeat thread A
    return {
        "thread_a": client.threads.get_state(thread_a)["values"],
        "thread_b": client.threads.get_state(thread_b)["values"],
    }


def main() -> None:
    from langgraph_sdk import get_sync_client

    values = run_demo(get_sync_client(url="http://localhost:7071/api"))
    for label, state in values.items():
        print(f"{label}: notes={state['notes']} turns={state['turns']}")


if __name__ == "__main__":
    main()
