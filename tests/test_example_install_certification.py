from __future__ import annotations

import importlib.util
from pathlib import Path
import runpy
import sys

import pytest

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
    monkeypatch.syspath_prepend(str(example_dir))

    # When: the Functions entry point is loaded
    module = runpy.run_path(str(example_dir / "function_app.py"))

    # Then: native route metadata remains available and the optional bridge is skipped
    metadata = module["langgraph_app"].get_app_metadata().graphs["tool_calling_agent"]
    assert module["count"] == 0
    assert any(route.path.endswith("/invoke") for route in metadata.routes)
