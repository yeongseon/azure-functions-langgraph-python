from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import time

from azure_functions_langgraph.observability import NoOpRunObserver
from azure_functions_langgraph.streaming import StreamingLangGraphApp, _import_fastapi_extension

ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = ROOT / "examples" / "true_streaming_agent" / "graph.py"


class Request:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode()

    async def body(self) -> bytes:
        return self._body


async def test_example_emits_first_frame_before_completion() -> None:
    # Given: the real delayed example graph and streaming response iterator
    spec = importlib.util.spec_from_file_location("true_streaming_graph", GRAPH_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = StreamingLangGraphApp()
    app.register(graph=module.compiled_graph, name="true_streaming_agent")
    registration = app._registrations["true_streaming_agent"]
    assert app.thread_lock is not None
    extension = _import_fastapi_extension()
    response = await app._handle_stream(
        Request(
            {
                "input": {
                    "messages": [{"role": "human", "content": "hello world"}],
                    "tokens": [],
                    "fail_at_second_step": False,
                },
                "config": {"configurable": {"thread_id": "local-timing"}},
                "stream_mode": "updates",
            }
        ),
        registration,
        False,
        extension.JSONResponse,
        extension.StreamingResponse,
        NoOpRunObserver(),
        app.thread_lock,
    )

    # When: the real response body is consumed frame by frame
    started = time.perf_counter()
    iterator = response.body_iterator.__aiter__()
    first_frame = await iterator.__anext__()
    first_elapsed = time.perf_counter() - started
    remaining = [frame async for frame in iterator]
    completed_elapsed = time.perf_counter() - started

    # Then: a data frame arrives while later graph steps are still executing
    assert "event: data" in first_frame
    assert remaining[-1] == "event: end\ndata: {}\n\n"
    assert first_elapsed < completed_elapsed - 0.3
