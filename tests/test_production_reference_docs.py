from pathlib import Path
import re


def test_reference_command_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    page = (root / "docs/production-reference.md").read_text()
    template = (root / "infra/flex.bicep").read_text()
    harness = (root / "tests/e2e_reference/test_reference.py").read_text()
    example = (root / "examples/production_persistent_agent/function_app.py").read_text()
    for parameter in (
        "functionAppName",
        "storageName",
        "location",
        "pythonVersion",
        "identityBasedStorage",
    ):
        assert re.search(rf"\bparam {parameter}\b", template)
        assert f"{parameter}=" in page
    for variable in (
        "REF882_BASE_URL",
        "REF882_FUNCTION_KEY",
        "REF882_RESOURCE_GROUP",
        "REF882_APP",
        "REF882_EVIDENCE",
    ):
        assert variable in harness and variable in page
    for filename in ("requirements-reference.txt", ".funcignore"):
        assert (root / "examples/production_persistent_agent" / filename).is_file()
        assert filename in page
    assert 'route="reference/runtime"' in example
    assert "/api/reference/runtime" in page
    assert "tests/e2e_reference" in page
