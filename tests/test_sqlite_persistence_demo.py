from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.example_imports import load_example_file

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "sqlite_checkpoint_local"


@pytest.fixture
def demo() -> Any:
    pytest.importorskip(
        "langgraph.checkpoint.sqlite",
        reason="SQLite extra is unavailable: pip install azure-functions-langgraph[sqlite]",
    )
    return load_example_file(EXAMPLE_DIR / "persistence_demo.py", "_sqlite_persistence_demo")


def _turn(output: dict[str, Any]) -> int:
    turn = output["output"]["turn"]
    assert isinstance(turn, int)
    return turn


def test_state_survives_closing_and_rebuilding_app_and_checkpointer(
    demo: Any, tmp_path: Path
) -> None:
    # Given: one temporary SQLite file shared by two freshly built apps
    # When: the demo invokes the same thread twice, closing the connection in between
    (first_out, first_state), (second_out, second_state) = demo.run_demo(tmp_path / "db.sqlite")

    # Then: the second app, built from nothing, continues the first app's thread
    assert _turn(first_out) == 1
    assert _turn(second_out) == 2
    assert first_state["values"]["turn"] == 1
    assert second_state["values"]["turn"] == 2
    assert second_state["values"]["messages"][-1]["content"] == "[turn 2] Echo: second"
    assert second_state["metadata"]["step"] > first_state["metadata"]["step"]


def test_separate_database_files_do_not_share_state(demo: Any, tmp_path: Path) -> None:
    # Given: two runs that use different SQLite files (the control for the test above)
    first_out, _ = demo._run_once(tmp_path / "a.sqlite", "first")
    other_out, _ = demo._run_once(tmp_path / "b.sqlite", "second")

    # Then: nothing carries over, so the persistence assertion above can actually fail
    assert _turn(first_out) == 1
    assert _turn(other_out) == 1


def test_readme_documents_demo_and_local_only_scope() -> None:
    readme = (EXAMPLE_DIR / "README.md").read_text()
    section = readme.split("## Persistence demo (offline)", 1)[1]

    assert "python persistence_demo.py" in section
    assert "local-development" in section
    assert "multi-instance" in section
    assert "does not add any consistency guarantee" in section
