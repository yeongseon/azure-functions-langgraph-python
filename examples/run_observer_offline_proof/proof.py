"""Offline proof that run telemetry is payload-free.

Drives the *real* registered ``invoke`` handler of a ``LangGraphApp`` wired with
the package's built-in ``LoggingRunObserver`` through three runs (success,
failure, rejection) using fixed fake request IDs, captures every log record the
observer emits, and asserts that safe correlation fields are present while a
dummy user-message canary is absent from all of them.

Everything runs in-process: no Azure Functions host, credentials, or network.

    python proof.py
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import azure.functions as func
from graph import FAIL_MARKER, compiled_graph

from azure_functions_langgraph import LangGraphApp, LoggingRunObserver
from azure_functions_langgraph.observability import RunObserver

GRAPH_NAME = "run_observer_offline_proof"
OBSERVER_LOGGER = "run_observer_offline_proof.observer"

# A dummy user message that must never appear in observer output.
CANARY = "CANARY-user-message-7f3a9c"

REQUEST_ID_OK = "fake-request-success"
REQUEST_ID_FAILED = "fake-request-failure"
REQUEST_ID_REJECTED = "fake-request-rejected"
THREAD_ID = "fake-thread-1"


class _Capture(logging.Handler):
    """Collect every record (and its rendered text) the observer logger emits."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _request(body: bytes, request_id: str) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url=f"/api/graphs/{GRAPH_NAME}/invoke",
        body=body,
        headers={"Content-Type": "application/json", "X-Request-ID": request_id},
        route_params={"name": GRAPH_NAME},
    )


def run_proof(observer: RunObserver | None = None) -> dict[str, Any]:
    """Run success / failure / rejected runs and return what the observer emitted.

    *observer* defaults to the built-in ``LoggingRunObserver``; tests pass a
    deliberately leaky one to prove the canary check can fail.
    """
    logger = logging.getLogger(OBSERVER_LOGGER)
    capture = _Capture()
    logger.addHandler(capture)
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        app = LangGraphApp(observer=observer or LoggingRunObserver(logger))
        app.register(graph=compiled_graph, name=GRAPH_NAME)
        # Bindings are not needed to call the registered handler in-process.
        app.function_app.functions_bindings = {}
        handler = next(
            fn.get_user_function()
            for fn in app.function_app.get_functions()
            if fn.get_function_name() == f"aflg_{GRAPH_NAME}_invoke"
        )

        def body(text: str) -> bytes:
            return json.dumps(
                {
                    "input": {"user_text": text},
                    "config": {"configurable": {"thread_id": THREAD_ID}},
                }
            ).encode()

        responses = {
            "success": handler(_request(body(CANARY), REQUEST_ID_OK)),
            "failure": handler(_request(body(CANARY + FAIL_MARKER), REQUEST_ID_FAILED)),
            # Malformed JSON that still embeds the canary: rejected before the graph runs.
            "rejected": handler(_request(CANARY.encode(), REQUEST_ID_REJECTED)),
        }
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    return {
        "responses": responses,
        "records": [r for r in capture.records if hasattr(r, "langgraph_run")],
        "all_records": capture.records,
    }


def render(result: dict[str, Any]) -> str:
    """Return the observer output as text: rendered messages plus structured fields."""
    lines: list[str] = []
    for record in result["all_records"]:
        lines.append(record.getMessage())
        lines.append(json.dumps(getattr(record, "langgraph_run", {}), sort_keys=True, default=str))
    return "\n".join(lines)


def main() -> int:
    result = run_proof()
    output = render(result)
    print(output)
    assert_payload_free(result)
    print(f"\nOK: {len(result['records'])} observer records, canary absent from all of them.")
    return 0


def assert_payload_free(result: dict[str, Any]) -> None:
    output = render(result)
    # The canary really flowed through the app (it is echoed in the success response)...
    assert CANARY in result["responses"]["success"].get_body().decode()
    # ...yet none of the observer's output contains it.
    assert CANARY not in output, "payload leaked into observer output"


if __name__ == "__main__":
    sys.exit(main())
