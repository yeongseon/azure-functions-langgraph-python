"""Auth documentation section matches recorded observations (#952)."""

from __future__ import annotations

from pathlib import Path
import re
from typing import TypedDict

import azure.functions as func
from azure.functions.decorators.http import HttpTrigger
from langgraph.graph import END, START, StateGraph
import pytest

from azure_functions_langgraph import LangGraphApp
from tests.test_docs_navigation import _anchors

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONFIGURATION = DOCS / "configuration.md"
HEADING = "### Native endpoint auth levels"
ANCHOR = "native-endpoint-auth-levels"

OBSERVED_FRAGMENTS = (
    "Core Tools",
    "does not enforce function keys locally",
    "Documentation metadata is descriptive, not enforcing",
    "not per-user authorization",
    "func.AuthLevel.FUNCTION",
    "func.AuthLevel.ANONYMOUS",
    "?code=<FUNCTION_KEY>",
    "func start --enableAuth",
)

NOT_IN_SCOPE = (
    "Live Azure provisioning",
    "cloud key generation",
    "expansion of experimental Durable/Platform contracts",
)

LINKERS = (
    (DOCS / "security.md", "configuration.md#" + ANCHOR),
    (DOCS / "installation.md", "configuration.md#" + ANCHOR),
    (ROOT / "README.md", "docs/configuration.md#" + ANCHOR),
)


def _section() -> str:
    text = CONFIGURATION.read_text()
    start = text.index(HEADING)
    nxt = re.search(r"^###? ", text[start + len(HEADING) :], re.MULTILINE)
    end = start + len(HEADING) + nxt.start() if nxt else len(text)
    return text[start:end]


def _normalized_section() -> str:
    return re.sub(r"\s+", " ", _section())


def test_section_exists_with_exact_heading_and_anchor() -> None:
    assert HEADING + "\n" in CONFIGURATION.read_text()
    assert ANCHOR in _anchors(CONFIGURATION)


@pytest.mark.parametrize("fragment", OBSERVED_FRAGMENTS)
def test_section_quotes_each_observed_fragment(fragment: str) -> None:
    assert fragment in _normalized_section()


def test_section_documents_scope_limits_and_validation() -> None:
    section = _normalized_section()
    for item in NOT_IN_SCOPE:
        assert item in section
    assert "Intentionally not run" in section


@pytest.mark.parametrize(("page", "link"), LINKERS)
def test_pages_link_to_the_section(page: Path, link: str) -> None:
    assert link in page.read_text()


def test_section_avoids_hardcoded_secrets() -> None:
    section = _section()
    assert "code=secret" not in section
    assert "x-functions-key: secret" not in section
    assert "$FUNCTION_KEY" in section


class _State(TypedDict, total=False):
    x: str


def _compiled_graph() -> object:
    builder = StateGraph(_State)
    builder.add_node("n", lambda state: {"x": "ok"})
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return builder.compile()


def _trigger_levels(app: LangGraphApp) -> dict[str, func.AuthLevel | None]:
    levels: dict[str, func.AuthLevel | None] = {}
    for function in app.function_app.get_functions():
        trigger = function.get_trigger()
        name = function.get_function_name()
        assert isinstance(trigger, HttpTrigger) and name is not None
        levels[name] = trigger.auth_level
    return levels


def test_documented_configuration_sets_the_trigger_levels_it_describes() -> None:
    # Given: the configuration shown on the page, with real compiled graphs
    graph = _compiled_graph()
    with pytest.warns(UserWarning, match="overrides auth_level to ANONYMOUS"):
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(graph=graph, name="agent")
        app.register(graph=graph, name="demo", auth_level=func.AuthLevel.ANONYMOUS)

    # When: reading each route's trigger binding, as the page says a test can
    levels = _trigger_levels(app)

    # Then: the app default and the per-graph override apply, and the health route stays anonymous
    assert levels["aflg_agent_invoke"] == func.AuthLevel.FUNCTION
    assert levels["aflg_demo_invoke"] == func.AuthLevel.ANONYMOUS
    assert levels["aflg_health"] == func.AuthLevel.ANONYMOUS
