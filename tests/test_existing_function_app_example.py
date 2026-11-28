from __future__ import annotations

import json
from pathlib import Path

import azure.functions as func
from langchain_core.messages import message_to_dict

from tests.example_imports import load_example_file
from tests.test_official_langgraph_basic_example import _pristine_upstream_graph

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "existing_function_app"


def test_existing_app_preserves_triggers_and_upstream_result() -> None:
    # Given the runnable example and an independent pinned upstream baseline.
    module = load_example_file(EXAMPLE / "function_app.py", "_existing_function_app")
    payload = {"messages": [{"role": "human", "content": "Hi"}]}
    baseline = _pristine_upstream_graph().invoke(payload)
    expected = {
        "messages": [payload["messages"][0], message_to_dict(baseline["messages"][1])],
        "extra_field": baseline["extra_field"],
    }

    # When Azure indexes the composed app and the HTTP handlers are called.
    functions = {f.get_function_name(): f for f in module.app.get_functions()}
    request = func.HttpRequest("POST", "/", body=json.dumps({"input": payload}).encode())
    response = functions["aflg_official_basic_invoke"].get_user_function()(request)
    existing = functions["existing_http"].get_user_function()(request)

    # Then all seven triggers coexist and graph output equals the upstream graph.
    assert set(functions) == {
        "existing_http",
        "existing_timer",
        "aflg_health",
        "aflg_health_details",
        "aflg_official_basic_invoke",
        "aflg_official_basic_stream",
        "aflg_official_basic_state",
    }
    timer = functions["existing_timer"].get_trigger().get_dict_repr()
    expected_timer = {
        "type": "timerTrigger",
        "name": "timer",
        "schedule": "0 */5 * * * *",
        "runOnStartup": False,
        "useMonitor": True,
    }
    assert {key: timer[key] for key in expected_timer} == expected_timer
    assert existing.status_code == 200
    assert existing.get_body() == b"existing HTTP still works"
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"output": expected}
