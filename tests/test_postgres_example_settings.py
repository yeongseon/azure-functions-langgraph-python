"""The Postgres example rejects a missing or empty connection setting before it connects (#926)."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
import pytest

import azure_functions_langgraph
from azure_functions_langgraph.checkpointers import postgres as postgres_module
from tests.example_imports import load_example_file

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "postgres_checkpoint_production" / "function_app.py"
SETTING = "LANGGRAPH_POSTGRES_CONNECTION_STRING"
SETUP_SETTING = "LANGGRAPH_POSTGRES_SETUP"
FAKE_DSN = "postgresql://fake-user:fake-secret@db.invalid:5432/fake"
EXPECTED_MESSAGE = (
    "LANGGRAPH_POSTGRES_CONNECTION_STRING is not set or is empty. Set it to a "
    "psycopg-compatible connection string in local.settings.json (local) or in the "
    "Function App settings (Azure); see README.md in this example."
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.calls: list[tuple[str, bool]] = []


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    real_app = azure_functions_langgraph.LangGraphApp

    def spy_factory(conn_string: str, *, setup: bool = True) -> InMemorySaver:
        rec.events.append("factory")
        rec.calls.append((conn_string, setup))
        return InMemorySaver()

    def spy_app(*args: Any, **kwargs: Any) -> Any:
        rec.events.append("app")
        return real_app(*args, **kwargs)

    monkeypatch.setattr(postgres_module, "create_postgres_checkpointer", spy_factory)
    monkeypatch.setattr(azure_functions_langgraph, "LangGraphApp", spy_app)
    monkeypatch.delenv(SETTING, raising=False)
    monkeypatch.delenv(SETUP_SETTING, raising=False)
    return rec


def _load() -> ModuleType:
    return load_example_file(EXAMPLE, "postgres_example_function_app")


@pytest.mark.parametrize("value", [None, "", "   \t"])
def test_missing_empty_or_blank_setting_is_rejected_when_loaded(
    value: str | None, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the setting is missing, empty or whitespace-only
    if value is not None:
        monkeypatch.setenv(SETTING, value)

    # When: the example is imported
    with pytest.raises(RuntimeError) as info:
        _load()

    # Then: the fixed message is raised, no value is echoed and the factory never ran
    assert str(info.value) == EXPECTED_MESSAGE
    assert recorder.calls == []
    assert recorder.events == []


@pytest.mark.parametrize(
    ("setup_env", "expected_setup"), [(None, True), ("false", False), ("true", True)]
)
def test_valid_setting_reaches_factory_unchanged_before_app_is_built(
    setup_env: str | None,
    expected_setup: bool,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a valid-looking setting with surrounding whitespace, and an optional setup flag
    padded = f"  {FAKE_DSN}  "
    monkeypatch.setenv(SETTING, padded)
    if setup_env is not None:
        monkeypatch.setenv(SETUP_SETTING, setup_env)

    # When: the example is imported
    module = _load()

    # Then: the exact original string and setup flag reach the factory, before LangGraphApp
    assert recorder.calls == [(padded, expected_setup)]
    assert recorder.events == ["factory", "app"]
    assert hasattr(module, "app")
