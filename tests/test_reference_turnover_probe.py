from __future__ import annotations

import base64
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import azure.functions as func
from langgraph.checkpoint.memory import InMemorySaver
import pytest

pytest.importorskip("azure.storage.blob")
pytest.importorskip("azure.identity")
from tests.example_imports import load_example_file

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "existing_function_app"
TENANT = "11111111-1111-1111-1111-111111111111"
THREAD = "restart-" + "a" * 32


@pytest.fixture
def reference(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Mock]:
    monkeypatch.setenv("REFERENCE_TENANT", TENANT)
    monkeypatch.setenv("AZURE_STORAGE_BLOB_ACCOUNT_URL", "https://example.blob.core.windows.net")
    monkeypatch.setenv("LANGGRAPH_BLOB_CONTAINER", "checkpoints")
    monkeypatch.setattr("azure.storage.blob.ContainerClient", Mock())
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", Mock())
    monkeypatch.setattr(
        "azure_functions_langgraph.checkpointers.azure_blob.AzureBlobCheckpointSaver",
        lambda **kwargs: InMemorySaver(),
    )
    lock = Mock()
    lock.acquire.return_value = "lease"
    monkeypatch.setattr(
        "azure_functions_langgraph.locks.AzureBlobLeaseThreadLock", lambda **kwargs: lock
    )
    module = load_example_file(EXAMPLE / "reference_function_app.py", "_reference_turnover")
    return module, lock


def request(method: str, thread: str = THREAD) -> func.HttpRequest:
    claims = {
        "auth_typ": "aad",
        "claims": [
            {"typ": "tid", "val": TENANT},
            {"typ": "oid", "val": "22222222-2222-2222-2222-222222222222"},
        ],
    }
    return func.HttpRequest(
        method,
        "/",
        params={"restart_thread": thread},
        headers={"x-ms-client-principal": base64.b64encode(json.dumps(claims).encode()).decode()},
        body=b"",
    )


def test_checkpoint_read_is_correlated_with_serving_process(
    reference: tuple[ModuleType, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a provisioned owner and a completed pre-turnover checkpoint.
    module, lock = reference
    monkeypatch.setattr(type(module.policy), "__call__", lambda self, context: True)
    module.reference_runtime(request("POST"))
    # When a different serving process reads the checkpoint.
    monkeypatch.setattr(module, "_PROCESS_TOKEN", "replacement-worker")
    response = module.reference_runtime(request("GET"))
    # Then its token and the prior state are returned in the same uncached response.
    payload = json.loads(response.get_body())
    assert payload["process_token"] == "replacement-worker"
    assert [m["data"]["content"] for m in payload["values"]["messages"]] == ["before", "Hello!"]
    assert response.headers["Cache-Control"] == "no-store"
    lock.release.assert_called_once_with("official_basic", THREAD, "lease")


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_non_owner_probe_is_denied_before_checkpoint_access(
    reference: tuple[ModuleType, Mock], monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    # Given an authenticated caller without the thread entitlement.
    module, lock = reference
    monkeypatch.setattr(type(module.policy), "__call__", lambda self, context: False)
    # When the caller attempts a checkpoint probe.
    response = module.reference_runtime(request(method))
    # Then neither checkpoint state nor a write is exposed.
    assert response.status_code == 403
    lock.acquire.assert_not_called()


def test_probe_refuses_unbounded_thread_names(reference: tuple[ModuleType, Mock]) -> None:
    # Given a valid ingress principal but a non-reference thread name.
    module, _ = reference
    # When requesting the probe.
    response = module.reference_runtime(request("GET", "ordinary-thread"))
    # Then the diagnostic surface refuses it.
    assert response.status_code == 400
