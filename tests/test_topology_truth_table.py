"""Executable truth table for build-time topology validation (#880)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import warnings

import azure.functions as func
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from azure_functions_langgraph import LangGraphApp, StreamingLangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from tests.topology_truth_table import END, ROWS, RULE_PATTERNS, START, Row, render_table

_ENV = (
    "AZFUNC_LANGGRAPH_LOCK_BACKEND",
    "AZFUNC_LANGGRAPH_THREAD_STORE",
    "WEBSITE_INSTANCE_ID",
    "WEBSITE_SITE_NAME",
)
_DOC = Path(__file__).resolve().parents[1] / "docs" / "production-guide.md"


class SharedSaver(BaseCheckpointSaver[str]):
    """A checkpointer that is not process-local (stands in for a Blob saver)."""


class Graph:
    def __init__(self, checkpointer: Any) -> None:
        self.checkpointer = checkpointer
        self.calls = 0

    def invoke(self, input: Any, config: Any = None) -> Any:
        self.calls += 1
        return {}

    def stream(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        yield {}


class DistributedLock:
    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        return "token"

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        pass


def _checkpointer(kind: str) -> Any:
    return {"none": None, "shared": SharedSaver(), "process-local": InMemorySaver()}[kind]


def _classify(message: str) -> str:
    matches = [rule for rule, pattern in RULE_PATTERNS.items() if pattern.search(message)]
    assert len(matches) == 1, (message, matches)
    return matches[0]


def _apply_env(row: Row, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    if row.hosting == "hosted":
        monkeypatch.setenv("WEBSITE_INSTANCE_ID", "instance-1")
    if row.declaration == "lock=distributed":
        monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "distributed")
    elif row.declaration == "store=distributed":
        monkeypatch.setenv("AZFUNC_LANGGRAPH_THREAD_STORE", "distributed")
    elif row.declaration == "inprocess":
        monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "inprocess")


def _build(row: Row, graph: Graph) -> tuple[Any, Any]:
    """Construct, register and build a row's app; returns ``(app, function_app)``."""
    lock: Any = DistributedLock() if row.lock == "distributed" else InProcessThreadLock()
    graph_auth = func.AuthLevel.FUNCTION if row.auth == "mixed" else None
    app_auth = func.AuthLevel.ANONYMOUS if row.auth == "mixed" else func.AuthLevel.FUNCTION
    app: Any
    if row.surface == "streaming":
        app = StreamingLangGraphApp(thread_lock=lock)
        app.register(graph, "g")
        return app, app.function_app
    app = LangGraphApp(
        auth_level=app_auth,
        platform_compat=row.surface == "platform",
        async_runs="durable" if row.surface == "durable" else False,
        thread_lock=lock,
    )
    if row.store == "distributed":
        app.thread_store = object()
    if row.surface == "service_bus":
        app.register_service_bus(graph, "g", connection="SB", queue_name="q")
    else:
        app.register(graph, "g", auth_level=graph_auth)
    return app, app.function_app


@pytest.mark.parametrize(
    "row",
    ROWS,
    ids=lambda r: (
        f"{r.surface}-{r.hosting}-{r.declaration}-{r.checkpointer}-{r.lock}-{r.store}-{r.auth}"
    ),
)
def test_truth_table_row(row: Row, monkeypatch: pytest.MonkeyPatch) -> None:
    if row.surface == "durable":
        pytest.importorskip("azure.durable_functions")
    if row.surface == "streaming":
        pytest.importorskip("azurefunctions.extensions.http.fastapi")
    _apply_env(row, monkeypatch)
    graph = Graph(_checkpointer(row.checkpointer))
    app_holder: list[Any] = []
    real_init = LangGraphApp.__init__

    def spy(self: Any, *a: Any, **k: Any) -> None:
        app_holder.append(self)
        real_init(self, *a, **k)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        monkeypatch.setattr(LangGraphApp, "__init__", spy)
        error: BaseException | None = None
        try:
            _build(row, graph)
        except (RuntimeError, ValueError) as exc:
            error = exc
    emitted = sorted(
        _classify(str(w.message)) for w in caught if issubclass(w.category, RuntimeWarning)
    )
    if row.errors:
        assert error is not None, "expected the configuration to be rejected"
        assert [_classify(str(error))] == list(row.errors)
        assert emitted == []  # an error preempts every warning
    else:
        assert error is None, error
        assert emitted == sorted(row.warnings)
    # A rejected configuration must never have executed the graph or exported routes.
    assert graph.calls == 0
    for app in app_holder:
        if row.errors:
            assert app._function_app is None
            assert app._optional_routes_exported is False


def test_valid_local_and_undeclared_uniform_auth_never_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for row in ROWS:
        if row.declaration == "none" and row.auth == "uniform":
            assert not row.errors, row


def test_doc_section_matches_truth_table() -> None:
    text = _DOC.read_text(encoding="utf-8")
    start, end = text.index(START), text.index(END) + len(END)
    assert text[start:end] == render_table()


def test_every_rule_is_documented_with_evidence() -> None:
    text = _DOC.read_text(encoding="utf-8")
    for rule in RULE_PATTERNS:
        assert f"`{rule}`" in text, rule
