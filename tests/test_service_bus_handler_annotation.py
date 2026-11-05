"""The generated Service Bus trigger handlers carry an annotation the Python worker accepts (#796).

The worker refuses to load a function whose trigger parameter annotation does not match the binding
type, and a load failure takes the whole function app down. The offline broker doubles call the
processing functions directly, so nothing else exercises the annotation.
"""

from __future__ import annotations

from collections.abc import Callable
import typing
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from tests.test_service_bus import AsyncRecordingGraph, RecordingGraph


def _handler(app: LangGraphApp, name: str) -> Callable[..., Any]:
    return next(
        f.get_user_function()
        for f in app.function_app.get_functions()
        if f.get_function_name() == name
    )


@pytest.mark.parametrize("topic", [False, True], ids=["queue", "topic"])
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_trigger_handler_annotation_matches_the_binding(use_async: bool, topic: bool) -> None:
    # Given: a Service Bus registration of each shape
    app = LangGraphApp()
    graph: Any = AsyncRecordingGraph() if use_async else RecordingGraph()
    if topic:
        app.register_service_bus(
            graph, "jobs", connection="SB", topic_name="t", subscription_name="s"
        )
    else:
        app.register_service_bus(graph, "jobs", connection="SB", queue_name="q")

    # When: the worker-visible annotation of the trigger parameter is resolved
    handler = _handler(app, "aflg_sb_jobs")
    hints = typing.get_type_hints(handler)

    # Then: it is the real message type, not Any (which fails FunctionLoadError on a real host)
    assert hints["msg"] is func.ServiceBusMessage
