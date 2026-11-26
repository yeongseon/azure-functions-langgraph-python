"""Offline contract for the example's protected deployed-version endpoint."""

from importlib.metadata import version
import json
from pathlib import Path
import platform
import re

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
        payload = json.loads(response.get_body())
        token = payload.pop("process_token")
        assert re.fullmatch(r"[a-f0-9]{32}", token)
        assert response.headers["Cache-Control"] == "no-store"
        assert payload == {
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
        assert set(binding["methods"]) == {func.HttpMethod.GET, func.HttpMethod.POST}
        bindings = {
            function.get_function_name(): function.get_bindings_dict()["bindings"][0]
            for function in module.app.get_functions()
        }
        assert bindings["aflg_health"]["authLevel"] == func.AuthLevel.ANONYMOUS
        assert bindings["aflg_health_details"]["authLevel"] == func.AuthLevel.FUNCTION
        from langgraph.checkpoint.memory import InMemorySaver

        monkeypatch.setattr(
            module, "compiled_graph", module.build_graph().compile(checkpointer=InMemorySaver())
        )
        from tests.e2e_reference._contracts import RestartResponse

        thread = "restart-" + "a" * 32
        handler = module.reference_runtime.build().get_user_function()
        for method in ("POST", "GET"):
            probe = handler(
                func.HttpRequest(
                    method=method,
                    url="/api/reference/runtime",
                    params={"restart_thread": thread},
                    body=b"",
                )
            )
            parsed = RestartResponse.model_validate_json(probe.get_body())
            assert parsed.process_token == token
            parsed.values.check_turns(["before"])
            assert "process_token" not in parsed.model_dump()
        for thread in ("private-thread", "restart-" + "g" * 32):
            rejected = handler(
                func.HttpRequest(
                    method="GET",
                    url="/api/reference/runtime",
                    params={"restart_thread": thread},
                    body=b"",
                )
            )
            assert rejected.status_code == 400
            assert rejected.get_body() == b""
        monkeypatch.setenv("AGENT_MODEL_MODE", "azure_openai")
        rejected = handler(
            func.HttpRequest(
                method="POST",
                url="/api/reference/runtime",
                params={"restart_thread": "restart-" + "a" * 32},
                body=b"",
            )
        )
        assert rejected.status_code == 400
        assert rejected.get_body() == b""
    finally:
        module.thread_lock.close()
        module.container_client.close()
