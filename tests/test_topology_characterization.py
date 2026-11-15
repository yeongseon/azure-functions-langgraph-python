"""Characterization of the build-time guards that predate the topology plan (#880).

These pin the exact error texts, exception types, precedence and "nothing was
exported" behaviour of the scale-out and mixed-auth guards so the plan refactor
can prove it moved them without changing them.
"""

from __future__ import annotations

from typing import Any
import warnings

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp, StreamingLangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock

_ENV = (
    "AZFUNC_LANGGRAPH_LOCK_BACKEND",
    "AZFUNC_LANGGRAPH_THREAD_STORE",
    "WEBSITE_INSTANCE_ID",
    "WEBSITE_SITE_NAME",
)


class CountingGraph:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, input: Any, config: Any = None) -> Any:
        self.calls += 1
        return {"ok": True}

    def stream(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        yield {"ok": True}


class CustomLock:
    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        return "token"

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        pass


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


LOCK_TEXT = (
    "AZFUNC_LANGGRAPH_LOCK_BACKEND='distributed' requires a "
    "distributed thread_lock backend (for example "
    "AzureBlobLeaseThreadLock), but the app is still using the "
    "default InProcessThreadLock. Pass thread_lock=... explicitly "
    "to LangGraphApp() or unset the env var for local dev."
)
STORE_TEXT = (
    "The configured scale-out safety guard requires a "
    "distributed thread store (for example AzureTableThreadStore), "
    "but the final configured store is InMemoryThreadStore. Set "
    "app.thread_store = ... before accessing app.function_app, or "
    "unset AZFUNC_LANGGRAPH_THREAD_STORE and "
    "AZFUNC_LANGGRAPH_LOCK_BACKEND for local dev."
)


def test_lock_guard_text_and_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "distributed")
    with pytest.raises(RuntimeError) as exc:
        LangGraphApp()
    assert str(exc.value) == LOCK_TEXT


def test_streaming_lock_guard_text_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "distributed")
    with pytest.raises(RuntimeError) as exc:
        StreamingLangGraphApp()
    assert str(exc.value) == LOCK_TEXT.replace("to LangGraphApp()", "to StreamingLangGraphApp()")


def test_store_guard_text_via_thread_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZFUNC_LANGGRAPH_THREAD_STORE", "distributed")
    graph = CountingGraph()
    app = LangGraphApp(platform_compat=True)
    app.register(graph, "g")
    with pytest.raises(RuntimeError) as exc:
        app.function_app
    assert str(exc.value) == STORE_TEXT
    assert graph.calls == 0
    assert app._function_app is None
    assert app._optional_routes_exported is False


def test_store_guard_via_lock_env_even_with_custom_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "distributed")
    app = LangGraphApp(platform_compat=True, thread_lock=CustomLock())
    with pytest.raises(RuntimeError) as exc:
        app.function_app
    assert str(exc.value) == STORE_TEXT


def test_store_guard_not_applied_without_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "distributed")
    app = LangGraphApp(thread_lock=CustomLock())
    assert app.function_app is not None


def test_mixed_auth_error_text_and_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mixed auth is checked before the store guard, so it wins when both apply.
    monkeypatch.setenv("AZFUNC_LANGGRAPH_THREAD_STORE", "distributed")
    graph = CountingGraph()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS, platform_compat=True)
        app.register(graph, "agent", auth_level=func.AuthLevel.FUNCTION)
    with pytest.raises(ValueError) as exc:
        app.function_app
    assert str(exc.value) == (
        "Graph 'agent' requires auth_level=FUNCTION but platform_compat=True expose it "
        "through routes protected only by the app-level auth_level=ANONYMOUS, which would "
        "bypass the graph's host key. Raise the app-level auth_level to at least "
        "FUNCTION, relax the graph's auth_level, or disable the optional routes."
    )
    assert graph.calls == 0
    assert app._optional_routes_exported is False


def test_hosted_in_memory_store_warns_with_runtime_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "abc")
    app = LangGraphApp(platform_compat=True)
    with pytest.warns(RuntimeWarning) as record:
        app.function_app
    text = str(record[0].message)
    assert "InMemoryThreadStore is per-instance" in text
    assert "AZFUNC_LANGGRAPH_THREAD_STORE=distributed" in text


def test_hosted_custom_store_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "abc")
    app = LangGraphApp(platform_compat=True, thread_lock=InProcessThreadLock())
    app.thread_store = object()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        app.function_app
