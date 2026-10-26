import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock


class DistributedLock:
    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        return "token"

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        pass


def test_distributed_lock_requirement_also_rejects_default_thread_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an explicit scale-out requirement without hosted-environment markers.
    monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "distributed")
    monkeypatch.delenv("AZFUNC_LANGGRAPH_THREAD_STORE", raising=False)
    app = LangGraphApp(platform_compat=True, thread_lock=DistributedLock())
    # When final routes would capture the default process-local store.
    with pytest.raises(RuntimeError, match="distributed thread store"):
        app.function_app


def test_local_thread_store_remains_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a local single-process application without a scale-out requirement.
    monkeypatch.delenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", raising=False)
    monkeypatch.delenv("AZFUNC_LANGGRAPH_THREAD_STORE", raising=False)
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    app = LangGraphApp(platform_compat=True, thread_lock=InProcessThreadLock())
    # When routes are built, no external service is mandatory.
    assert app.function_app is not None
