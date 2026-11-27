from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from tests.example_imports import load_example_file

# Load the script by path: the installed-wheel test tier runs from outside the
# checkout, so ``scripts`` is not importable as a package there.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "certify_primary_examples.py"
_SPEC = importlib.util.spec_from_file_location("certify_primary_examples", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_CERTIFY = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CERTIFY
_SPEC.loader.exec_module(_CERTIFY)

PRIMARY_EXAMPLES = _CERTIFY.PRIMARY_EXAMPLES
adapter_free_requirements = _CERTIFY.adapter_free_requirements
parse_args = _CERTIFY.parse_args


def test_primary_example_inventory_matches_learning_path() -> None:
    # Given: the repository's supported onboarding sequence
    # When: the certification inventory is inspected
    # Then: every primary example is certified in learning-path order
    assert PRIMARY_EXAMPLES == (
        "azure_openai_agent",
        "conversation_memory",
        "tool_calling_agent",
        "production_persistent_agent",
    )


def test_candidate_requirements_replace_only_adapter_distribution(tmp_path: Path) -> None:
    # Given: requirements containing adapter extras plus ordinary dependencies
    source = tmp_path / "requirements.txt"
    source.write_text(
        "# retained comment\n"
        "azure-functions\n"
        "azure-functions-langgraph[azure-blob]>=0.10,<0.11\n"
        "langgraph>=1,<2\n"
    )

    # When: candidate-wheel requirements are derived
    generated = adapter_free_requirements(source)

    # Then: only the adapter distribution requirement is removed
    assert generated == "# retained comment\nazure-functions\nlanggraph>=1,<2\n"


def test_published_lane_rejects_candidate_wheel_argument() -> None:
    # Given: the published lane, whose requirements must remain unchanged
    # When/Then: a candidate wheel is supplied, argument parsing rejects it
    with pytest.raises(SystemExit):
        parse_args(["published", "--wheel", "candidate.whl"])


def test_candidate_lane_requires_candidate_wheel() -> None:
    # Given: the candidate lane
    # When/Then: omitting its wheel is rejected before any install starts
    with pytest.raises(SystemExit):
        parse_args(["candidate"])


def test_official_selection_and_local_host_are_explicit(tmp_path: Path) -> None:
    # Given an opt-in real-host request limited to the upstream examples.
    # When parsing it, no installs or processes are started.
    config = parse_args(
        [
            "published",
            "--examples",
            "official_langgraph_basic",
            "official_langgraph_conversation",
            "--local-host",
            "--host-port",
            "7571",
            "--storage-port",
            "15000",
            "--output-dir",
            str(tmp_path),
        ]
    )
    # Then the selected examples and assigned ports are preserved.
    assert config.examples == ("official_langgraph_basic", "official_langgraph_conversation")
    assert (config.local_host, config.host_port, config.storage_port) == (True, 7571, 15000)


def test_default_certification_includes_official_examples_without_host() -> None:
    # Given the default published lane.
    # When selecting its inventory.
    config = parse_args(["published"])
    # Then both upstream graphs join the original learning path, with no host opt-in.
    assert config.examples == (*PRIMARY_EXAMPLES, *_CERTIFY.OFFICIAL_EXAMPLES)
    assert len(_CERTIFY.OFFICIAL_EXAMPLES) == 2
    assert not config.local_host


def test_candidate_install_preserves_extras_and_ignores_source_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given an adapter extra, a candidate wheel and a parent source-tree import path.
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("azure-functions-langgraph[azure-blob]==0.14.2\nlanggraph>=1,<2\n")
    wheel = tmp_path / "candidate.whl"
    venv = tmp_path / "venv"
    monkeypatch.setenv("PYTHONPATH", "/source/tree")
    commands: list[tuple[list[str], dict[str, str] | None]] = []

    def run(command: list[str], *, env: dict[str, str] | None = None) -> str:
        commands.append((command, env))
        return ""

    monkeypatch.setattr(_CERTIFY, "_run", run)
    config = parse_args(["candidate", "--wheel", str(wheel), "--output-dir", str(tmp_path)])
    # When installing with subprocesses replaced at the command boundary.
    _CERTIFY._install(config, requirements, venv)
    # Then only the adapter changes and every installer is scoped to the throwaway venv.
    assert (tmp_path / "requirements-without-adapter.txt").read_text() == "langgraph>=1,<2\n"
    assert commands[1][0][-1] == f"azure-functions-langgraph[azure-blob] @ {wheel.as_uri()}"
    assert all(
        env is not None and env["VIRTUAL_ENV"] == str(venv) and "PYTHONPATH" not in env
        for _, env in commands[1:]
    )


def test_default_output_directory_uses_system_temporary_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a platform-specific system temporary directory
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    # When: the published lane uses its default output directory
    config = parse_args(["published"])

    # Then: certification creates its workspace under that system directory
    assert config.output_dir.parent == tmp_path
    assert config.output_dir.name.startswith("primary-examples-published-")


def test_tool_calling_example_starts_without_optional_openapi_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a clean primary install without azure-functions-openapi
    example_dir = Path(__file__).resolve().parents[1] / "examples" / "tool_calling_agent"
    monkeypatch.setenv("AGENT_MODEL_MODE", "fake")
    monkeypatch.setitem(sys.modules, "azure_functions_openapi", None)

    # When: the Functions entry point is loaded
    module = load_example_file(
        example_dir / "function_app.py", "_install_certification_tool_calling_function_app"
    )

    # Then: native route metadata remains available and the optional bridge is skipped
    metadata = module.langgraph_app.get_app_metadata().graphs["tool_calling_agent"]
    assert module.count == 0
    assert any(route.path.endswith("/invoke") for route in metadata.routes)
