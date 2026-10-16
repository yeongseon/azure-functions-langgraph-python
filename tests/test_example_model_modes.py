from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
import sys
from types import ModuleType
from typing import Any
import uuid

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
MODEL_EXAMPLES = (
    "azure_openai_agent",
    "conversation_memory",
    "tool_calling_agent",
    "production_persistent_agent",
)
AZURE_SETTINGS = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_USE_ENTRA_ID",
)


def _load_graph(example: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    example_dir = EXAMPLES_DIR / example
    module_name = f"_model_mode_{example}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, example_dir / "graph.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.modules.pop("model_config", None)
    monkeypatch.syspath_prepend(str(example_dir))
    spec.loader.exec_module(module)
    return module


def _clear_model_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_MODEL_MODE", raising=False)
    for name in AZURE_SETTINGS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("example", MODEL_EXAMPLES)
def test_fake_mode_needs_no_credentials_or_live_sdk(
    example: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    _clear_model_settings(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL_MODE", "fake")
    monkeypatch.setitem(sys.modules, "langchain_openai", None)

    # When
    with caplog.at_level(logging.WARNING):
        module = _load_graph(example, monkeypatch)

    # Then
    assert module.MODEL_MODE == "fake"
    assert any(getattr(record, "model_mode", None) == "fake" for record in caplog.records)


@pytest.mark.parametrize("example", MODEL_EXAMPLES)
@pytest.mark.parametrize("mode", [None, "unexpected"])
def test_omitted_or_invalid_mode_fails_before_startup(
    example: str,
    mode: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    _clear_model_settings(monkeypatch)
    if mode is not None:
        monkeypatch.setenv("AGENT_MODEL_MODE", mode)

    # When / Then
    with pytest.raises(ValueError, match="AGENT_MODEL_MODE"):
        _load_graph(example, monkeypatch)


@pytest.mark.parametrize("example", MODEL_EXAMPLES)
@pytest.mark.parametrize(
    ("configured", "missing"),
    [
        ({"AZURE_OPENAI_API_KEY": "secret"}, {"AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"}),
        (
            {"AZURE_OPENAI_ENDPOINT": "https://example.invalid", "AZURE_OPENAI_API_KEY": "secret"},
            {"AZURE_OPENAI_DEPLOYMENT"},
        ),
        (
            {
                "AZURE_OPENAI_ENDPOINT": "https://example.invalid",
                "AZURE_OPENAI_DEPLOYMENT": "chat",
            },
            {"AZURE_OPENAI_API_KEY", "AZURE_OPENAI_USE_ENTRA_ID"},
        ),
    ],
)
def test_azure_mode_lists_only_missing_setting_names(
    example: str,
    configured: dict[str, str],
    missing: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    _clear_model_settings(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL_MODE", "azure_openai")
    for name, value in configured.items():
        monkeypatch.setenv(name, value)

    # When / Then
    with pytest.raises(RuntimeError) as caught:
        _load_graph(example, monkeypatch)
    assert set(str(caught.value).split(": ", maxsplit=1)[1].split(", ")) == missing
    assert "secret" not in str(caught.value)
    assert "example.invalid" not in str(caught.value)


@pytest.mark.parametrize("example", MODEL_EXAMPLES)
@pytest.mark.parametrize("auth", ["key", "entra"])
def test_valid_azure_auth_builds_live_model(
    example: str,
    auth: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    _clear_model_settings(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL_MODE", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "chat")
    if auth == "key":
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
    else:
        monkeypatch.setenv("AZURE_OPENAI_USE_ENTRA_ID", "true")

    calls: list[dict[str, Any]] = []

    class FakeAzureChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        def bind_tools(self, tools: Any) -> FakeAzureChatOpenAI:
            return self

        def invoke(self, messages: Any) -> None:
            return None

    openai_module = ModuleType("langchain_openai")
    openai_module.__dict__["AzureChatOpenAI"] = FakeAzureChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", openai_module)

    if auth == "entra":
        identity_module = ModuleType("azure.identity")
        identity_module.__dict__["DefaultAzureCredential"] = lambda: "credential"
        identity_module.__dict__["get_bearer_token_provider"] = lambda credential, scope: (
            "token-provider"
        )
        monkeypatch.setitem(sys.modules, "azure.identity", identity_module)

    # When
    module = _load_graph(example, monkeypatch)

    # Then
    assert module.MODEL_MODE == "azure_openai"
    assert len(calls) == 1
    assert ("api_key" in calls[0]) is (auth == "key")
    assert ("azure_ad_token_provider" in calls[0]) is (auth == "entra")


def test_live_sdk_construction_error_does_not_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    _clear_model_settings(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL_MODE", "azure_openai")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "chat")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")

    class SDKConstructionError(Exception):
        pass

    class BrokenAzureChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            raise SDKConstructionError

    openai_module = ModuleType("langchain_openai")
    openai_module.__dict__["AzureChatOpenAI"] = BrokenAzureChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", openai_module)

    # When / Then
    with pytest.raises(SDKConstructionError):
        _load_graph("azure_openai_agent", monkeypatch)


def test_tool_fake_is_state_aware_for_independent_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    _clear_model_settings(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL_MODE", "fake")
    module = _load_graph("tool_calling_agent", monkeypatch)
    request = {"messages": [{"role": "human", "content": "status"}]}

    # When
    first = module.compiled_graph.invoke(request)
    second = module.compiled_graph.invoke(request)

    # Then
    assert sum(message.type == "tool" for message in first["messages"]) == 1
    assert sum(message.type == "tool" for message in second["messages"]) == 1


def test_missing_tool_name_has_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    _clear_model_settings(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL_MODE", "fake")
    module = _load_graph("tool_calling_agent", monkeypatch)
    module.FAKE_TOOL_CALL = module.FakeToolCall(name="removed_tool", args={})

    # When / Then
    with pytest.raises(RuntimeError, match="removed_tool.*TOOLS.*FAKE_TOOL_CALL"):
        module.build_chat_model()
