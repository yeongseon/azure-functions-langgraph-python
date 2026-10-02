from __future__ import annotations

import json
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp


class _Graph:
    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return input

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> list[dict[str, str]]:
        return [{"value": "too large"}]

    async def ainvoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return input

    async def astream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Any:
        yield {"value": "too large"}


def _request() -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="/api/graphs/agent/stream",
        body=json.dumps({"input": {}}).encode(),
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize("async_mode", [False, True])
async def test_overflow_stream_has_one_error_and_terminal_event(async_mode: bool) -> None:
    # Given
    app = LangGraphApp(max_stream_response_bytes=1)
    app.register(graph=_Graph(), name="agent", async_mode=async_mode)

    # When
    if async_mode:
        response = await app._handle_stream_async(_request(), app._registrations["agent"])
    else:
        response = app._handle_stream(_request(), app._registrations["agent"])

    # Then
    body = response.get_body().decode()
    assert body.count("event: error") == 1
    assert body.endswith("event: end\ndata: {}\n\n")
