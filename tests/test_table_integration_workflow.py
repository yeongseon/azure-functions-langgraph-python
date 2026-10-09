from __future__ import annotations

from pathlib import Path

TABLE_WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-table-integration.yml"
)


def test_table_workflow_covers_all_service_integration_inputs() -> None:
    workflow = TABLE_WORKFLOW.read_text()

    for required_path in (
        '"src/azure_functions_langgraph/locks/**"',
        '"src/azure_functions_langgraph/app.py"',
        '"tests/integration/**"',
        '"tests/test_production_persistent_agent_example.py"',
    ):
        assert required_path in workflow


def test_table_workflow_requires_azurite_tests_and_accepts_new_sdk_versions() -> None:
    workflow = TABLE_WORKFLOW.read_text()

    assert "--skipApiVersionCheck" in workflow
    assert "AZURITE_REQUIRED: true" in workflow
    assert "--strict-markers" in workflow
