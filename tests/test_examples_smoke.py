from __future__ import annotations

from pathlib import Path

import pytest

from tests.example_imports import load_example_file

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

GRAPH_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("simple_agent", "compiled_graph"),
    ("azure_openai_agent", "compiled_graph"),
    ("conversation_memory", "compiled_graph"),
    ("platform_compat_sdk", "compiled_graph"),
    ("platform_thread_isolation", "compiled_graph"),
    ("openapi_bridge", "compiled_graph"),
    ("tool_calling_agent", "compiled_graph"),
    ("production_auth", "private_graph"),
    ("production_auth", "public_graph"),
    ("managed_identity_storage", "compiled_graph"),
    ("cosmos_checkpoint_azure", "compiled_graph"),
    ("production_persistent_agent", "compiled_graph"),
    ("versioned_output_agent", "compiled_graph"),
    ("async_agent", "compiled_graph"),
    ("run_observer", "compiled_graph"),
    ("observability_app_insights", "compiled_graph"),
    ("run_observer_otel", "compiled_graph"),
    ("service_bus_agent", "compiled_graph"),
    ("true_streaming_agent", "compiled_graph"),
    ("durable_async_agent", "compiled_graph"),
)


@pytest.mark.parametrize("example,attr", GRAPH_EXAMPLES)
def test_example_graph_compiles(example: str, attr: str, monkeypatch: pytest.MonkeyPatch) -> None:
    example_dir = EXAMPLES_DIR / example
    assert example_dir.is_dir(), f"missing example directory: {example}"

    if example in {
        "azure_openai_agent",
        "conversation_memory",
        "tool_calling_agent",
        "production_persistent_agent",
    }:
        monkeypatch.setenv("AGENT_MODEL_MODE", "fake")
    mod = load_example_file(example_dir / "graph.py", f"_examples_{example}_graph")

    graph = getattr(mod, attr)
    assert graph is not None
    assert hasattr(graph, "invoke"), f"{example}.{attr} missing invoke()"


REQUIRED_FILES = (
    "README.md",
    "function_app.py",
    "host.json",
    "requirements.txt",
)

EXAMPLE_DIRS = (
    "simple_agent",
    "official_langgraph_basic",
    "azure_openai_agent",
    "conversation_memory",
    "platform_compat_sdk",
    "platform_thread_isolation",
    "persistent_agent_blob_table",
    "managed_identity_storage",
    "openapi_bridge",
    "tool_calling_agent",
    "production_auth",
    "cosmos_checkpoint_azure",
    "production_persistent_agent",
    "versioned_output_agent",
    "async_agent",
    "run_observer",
    "observability_app_insights",
    "run_observer_otel",
    "service_bus_agent",
    "true_streaming_agent",
    "durable_async_agent",
    "sqlite_checkpoint_local",
)


@pytest.mark.parametrize("example", EXAMPLE_DIRS)
def test_example_has_required_files(example: str) -> None:
    example_dir = EXAMPLES_DIR / example
    for name in REQUIRED_FILES:
        assert (example_dir / name).is_file(), f"{example}/{name} missing"


def test_local_curl_scripts_executable() -> None:
    curl_dir = EXAMPLES_DIR / "local_curl"
    scripts = sorted(curl_dir.glob("*.sh"))
    assert scripts, "local_curl/ should contain shell scripts"
    for script in scripts:
        mode = script.stat().st_mode
        assert mode & 0o111, f"{script} is not executable"
