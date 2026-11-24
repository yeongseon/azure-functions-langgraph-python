"""Offline contract for the example's protected deployed-version endpoint."""

from importlib.metadata import version
import json
from pathlib import Path
import platform

import azure.functions as func
import pytest

from tests.example_imports import load_example_file


def test_runtime_reports_installed_distributions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the existing example's fake model and a client that performs no cold-start IO.
    pytest.importorskip("azure.storage.blob")
    pytest.importorskip("azure.identity")
    monkeypatch.setenv("AGENT_MODEL_MODE", "fake")
    monkeypatch.setenv("AZURE_STORAGE_BLOB_ACCOUNT_URL", "https://example.blob.core.windows.net")
    monkeypatch.setenv("LANGGRAPH_ENABLE_PLATFORM", "false")
    monkeypatch.setenv("LANGGRAPH_AUTO_CREATE_STORAGE", "false")
    root = Path(__file__).resolve().parents[1]
    module = load_example_file(
        root / "examples/production_persistent_agent/function_app.py", "_reference_example"
    )
    try:
        # When calling the version endpoint registered by the real Functions decorator.
        response = module.reference_runtime.build().get_user_function()(
            func.HttpRequest(method="GET", url="/api/reference/runtime", body=b"")
        )
        # Then it reflects this process's installed distributions, and is FUNCTION-protected.
        assert json.loads(response.get_body()) == {
            "python": platform.python_version(),
            "adapter": version("azure-functions-langgraph"),
            "functions": version("azure-functions"),
            "langgraph": version("langgraph"),
            "langchain_core": version("langchain-core"),
            "blob": version("azure-storage-blob"),
            "identity": version("azure-identity"),
        }
        binding = module.reference_runtime.build().get_bindings_dict()["bindings"][0]
        assert binding["authLevel"] == func.AuthLevel.FUNCTION
    finally:
        module.thread_lock.close()
        module.container_client.close()
