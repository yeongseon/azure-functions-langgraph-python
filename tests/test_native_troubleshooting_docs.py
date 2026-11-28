"""Native invocation troubleshooting section matches recorded observations (#939)."""

from __future__ import annotations

import json
from pathlib import Path
import re
import types
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph._handlers import handle_invoke
from tests.test_docs_navigation import _anchors

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
TROUBLESHOOTING = DOCS / "troubleshooting.md"
HEADING = "## No-credentials native invocation troubleshooting"
ANCHOR = "no-credentials-native-invocation-troubleshooting"

OBSERVED_FRAGMENTS = (
    '"greeting": "Hello, World!"',
    "Hello, World! Goodbye!",
    '"loc": ["input"]',
    '"type": "missing"',
    "Only config.configurable is client configurable",
    "Unapproved config.configurable field",
    "Graph execution failed",
    "<request-id>",
    "32-character hex string",
    "examples/simple_agent/README.md#run-locally",
)

NOT_IN_SCOPE = (
    (
        "Platform (`/threads`, `/runs`, `/assistants`) and Durable"
        ' (`async_runs="durable"`) endpoints'
    ),
    "intentionally out of scope here",
)

VERSIONS = (
    "Azure Functions Core Tools 4.8.0",
    "`azure-functions-langgraph` 0.13.0",
    "`examples/simple_agent`",
)

LINKERS = (
    (DOCS / "getting-started.md", "troubleshooting.md#" + ANCHOR),
    (DOCS / "tutorial-5-min.md", "troubleshooting.md#" + ANCHOR),
    (
        ROOT / "examples" / "simple_agent" / "README.md",
        "../../docs/troubleshooting.md#" + ANCHOR,
    ),
)


def _section() -> str:
    text = TROUBLESHOOTING.read_text()
    start = text.index(HEADING)
    nxt = re.search(r"^## ", text[start + len(HEADING) :], re.MULTILINE)
    end = start + len(HEADING) + nxt.start() if nxt else len(text)
    return text[start:end]


def test_section_exists_with_exact_heading_and_anchor() -> None:
    assert HEADING + "\n" in TROUBLESHOOTING.read_text()
    assert ANCHOR in _anchors(TROUBLESHOOTING)


@pytest.mark.parametrize("fragment", OBSERVED_FRAGMENTS)
def test_section_quotes_each_observed_fragment(fragment: str) -> None:
    assert fragment in _section()


def test_section_documents_scope_limits_and_validation() -> None:
    section = _section()
    for item in NOT_IN_SCOPE:
        assert item in section
    for version in VERSIONS:
        assert version in section
    assert "Intentionally not run" in section


@pytest.mark.parametrize(("page", "link"), LINKERS)
def test_pages_link_to_the_section(page: Path, link: str) -> None:
    assert link in page.read_text()


class _DummyLock:
    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        return None

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        pass


class _DummyGraph:
    def invoke(self, graph_input: Any, config: Any = None) -> dict[str, bool]:
        return {"ok": True}


def _invoke_handler(body: dict[str, Any]) -> func.HttpResponse:
    reg = types.SimpleNamespace(
        name="simple_agent",
        graph=_DummyGraph(),
        stream_enabled=True,
        allowed_configurable_keys=frozenset({"locale"}),
    )
    req = func.HttpRequest(
        method="POST",
        url="http://localhost:7071/api/graphs/simple_agent/invoke",
        body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return handle_invoke(
        req,
        reg,
        thread_lock=_DummyLock(),
        max_request_body_bytes=1_000_000,
        max_input_depth=20,
        max_input_nodes=1_000,
    )


def test_native_handler_emits_quoted_422_shape() -> None:
    # Bare payload without "input" envelope (Step 2)
    res = _invoke_handler({"messages": [{"role": "human", "content": "World"}]})
    assert res.status_code == 422
    data = json.loads(res.get_body())
    assert data["error"] == "error"
    assert data["detail"] == [{"loc": ["input"], "type": "missing"}]
    assert re.fullmatch(r"[0-9a-f]{32}", data["request_id"])


def test_native_handler_emits_quoted_400_shapes() -> None:
    # 1. Unapproved execution control (recursion_limit)
    res_rec = _invoke_handler({"input": {}, "config": {"recursion_limit": 25}})
    assert res_rec.status_code == 400
    data_rec = json.loads(res_rec.get_body())
    assert data_rec["error"] == "error"
    assert data_rec["detail"] == "Only config.configurable is client configurable"
    assert re.fullmatch(r"[0-9a-f]{32}", data_rec["request_id"])

    # 2. Unapproved configurable key
    res_unapp = _invoke_handler({"input": {}, "config": {"configurable": {"unlisted": 1}}})
    assert res_unapp.status_code == 400
    data_unapp = json.loads(res_unapp.get_body())
    assert data_unapp["error"] == "error"
    assert data_unapp["detail"] == "Unapproved config.configurable field"
    assert re.fullmatch(r"[0-9a-f]{32}", data_unapp["request_id"])
