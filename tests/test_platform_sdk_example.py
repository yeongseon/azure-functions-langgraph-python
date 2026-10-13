from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

from azure_functions_langgraph import LangGraphApp
from tests.test_sdk_compat import _make_sdk_client

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "examples" / "platform_compat_sdk"
DYNAMIC_FIELDS = {
    "thread_id": "<thread_id>",
    "checkpoint_id": "<checkpoint_id>",
    "created_at": "<timestamp>",
    "updated_at": "<timestamp>",
}


def _load_example_graph() -> Any:
    spec = importlib.util.spec_from_file_location("_platform_sdk_graph", EXAMPLE_DIR / "graph.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.compiled_graph


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: DYNAMIC_FIELDS.get(key, _normalize(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def test_sdk_example_documents_live_thread_state() -> None:
    # Given: the shipped example behind the real SDK-to-Functions transport bridge
    app = LangGraphApp(platform_compat=True)
    app.register(graph=_load_example_graph(), name="echo_agent")
    client = _make_sdk_client(app.function_app)

    # When: the SDK creates a thread, runs the example, and reads its state
    thread = client.threads.create()
    result = client.runs.wait(
        thread["thread_id"],
        "echo_agent",
        input=json.loads('{"messages":[{"role":"human","content":"Hello, SDK!"}]}'),
    )
    state = client.threads.get_state(thread["thread_id"])
    transcript = (EXAMPLE_DIR / "README.md").read_text().split("## Captured output", 1)[1]

    # Then: normalized dynamic IDs leave every stable value tied to live output
    assert json.dumps(_normalize(thread), separators=(",", ":")) in transcript
    assert json.dumps(result, separators=(",", ":")) in transcript
    assert json.dumps(_normalize(state), separators=(",", ":")) in transcript
    assert "COMPATIBILITY.md" in transcript
