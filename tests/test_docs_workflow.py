from pathlib import Path

DOCS_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docs.yml"


def test_legacy_docs_workflow_is_manual_only() -> None:
    workflow = DOCS_WORKFLOW.read_text()
    trigger_block = workflow.split("permissions:", 1)[0]

    assert "workflow_dispatch:" in trigger_block
    assert "push:" not in trigger_block
    assert "pull_request:" not in trigger_block
