"""Doc-contract tests for the copy-into-your-project scaffold path (#923).

Verifies the Quickstart's Core Tools onboarding section, cross-reference links,
and observed host contracts without running Core Tools or external dependencies.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

# Files created by `func init . --worker-runtime python --model V2` in Core Tools 4.8.0.
# Verification record: issue #923 (2026-11-18).
SCAFFOLD_FILES: tuple[str, ...] = (
    "function_app.py",
    "host.json",
    "local.settings.json",
    "requirements.txt",
    ".gitignore",
    ".vscode/extensions.json",
)


def _slug(heading: str) -> str:
    text = re.sub(r"`", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")


def test_scaffold_section_position_and_incoming_links() -> None:
    # Given: the Quickstart, README, and 5-minute tutorial
    quickstart = (ROOT / "docs" / "getting-started.md").read_text()
    readme = (ROOT / "README.md").read_text()
    tutorial = (ROOT / "docs" / "tutorial-5-min.md").read_text()

    # When: section positions and anchors are inspected
    anchor = "start-from-a-new-functions-project"
    prereq_idx = quickstart.find("## Prerequisites")
    section_idx = quickstart.find("## Start from a new Functions project")
    step1_idx = quickstart.find("## Step 1: Define your graph state")

    readme_quickstart = readme.split("## Quick Start", 1)[1].split("\n## ", 1)[0]
    tutorial_next_steps = tutorial.split("## Next steps", 1)[1]

    # Then: the section exists between Prerequisites and Step 1
    assert prereq_idx != -1, "Prerequisites section missing in getting-started.md"
    assert section_idx != -1, (
        "Start from a new Functions project section missing in getting-started.md"
    )
    assert step1_idx != -1, "Step 1 section missing in getting-started.md"
    assert prereq_idx < section_idx < step1_idx, (
        "Section 'Start from a new Functions project' must sit between "
        "'## Prerequisites' and '## Step 1'"
    )

    # And: README Quick Start links to the section with exact anchor
    expected_readme_link = f"docs/getting-started.md#{anchor}"
    assert expected_readme_link in readme_quickstart, (
        f"README Quick Start must link to {expected_readme_link}"
    )

    # And: tutorial Next steps links to the section with exact anchor
    expected_tutorial_link = f"getting-started.md#{anchor}"
    assert expected_tutorial_link in tutorial_next_steps, (
        f"Tutorial Next steps must link to {expected_tutorial_link}"
    )


def test_scaffold_section_contains_init_command_and_file_roles() -> None:
    # Given: the 'Start from a new Functions project' section
    quickstart = (ROOT / "docs" / "getting-started.md").read_text()
    section = quickstart.split("## Start from a new Functions project", 1)[1].split(
        "## Step 1: Define your graph state", 1
    )[0]

    # When: bash fences and file role instructions are extracted
    bash_fences: list[str] = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)
    normalized = " ".join(section.split())

    # Then: exact func init command is present inside a bash fence
    exact_command = "func init . --worker-runtime python --model V2"
    assert any(exact_command in fence for fence in bash_fences), (
        f"Scaffold section must contain exact command '{exact_command}' inside a bash code block"
    )

    # And: host.json, local.settings.json, and function_app.py are named with keep/replace roles
    clean_text = re.sub(r"[`]", "", normalized).lower()
    assert "keep host.json and local.settings.json" in clean_text, (
        "Scaffold section must instruct reader to keep host.json and local.settings.json"
    )
    assert "replace function_app.py" in clean_text, (
        "Scaffold section must instruct reader to replace function_app.py"
    )


def test_scaffold_section_quotes_multiple_app_error_and_exports_app() -> None:
    # Given: the 'Start from a new Functions project' section
    quickstart = (ROOT / "docs" / "getting-started.md").read_text()
    section = quickstart.split("## Start from a new Functions project", 1)[1].split(
        "## Step 1: Define your graph state", 1
    )[0]
    normalized = " ".join(section.split())

    # When: error messages and export instructions are inspected
    # Then: host error text is quoted
    assert "Worker failed to index functions" in section, (
        "Scaffold section must quote host log 'Worker failed to index functions'"
    )
    assert (
        "More than one FunctionApp or other top level function app instances are defined."
        in section
    ), (
        "Scaffold section must quote host exception "
        "'More than one FunctionApp or other top level function app instances are defined.'"
    )

    # And: instructs exporting the library's app as app
    assert (
        "export the library's app as `app`" in normalized
        or "export the library's app as app" in normalized
    ), "Scaffold section must instruct reader to export the library app as 'app'"


def test_scaffold_section_records_storage_note_and_no_package_version_specifier() -> None:
    # Given: the 'Start from a new Functions project' section
    quickstart = (ROOT / "docs" / "getting-started.md").read_text()
    section = quickstart.split("## Start from a new Functions project", 1)[1].split(
        "## Step 1: Define your graph state", 1
    )[0]

    # When: storage keys and observed descriptions are checked
    # Then: names UseDevelopmentStorage=true and AzureWebJobsStorage
    assert "UseDevelopmentStorage=true" in section, (
        "Scaffold section must mention 'UseDevelopmentStorage=true'"
    )
    assert "AzureWebJobsStorage" in section, "Scaffold section must mention 'AzureWebJobsStorage'"

    # And: quotes both observed unhealthy descriptions
    assert "A timeout occurred while running check." in section, (
        "Scaffold section must quote observed description 'A timeout occurred while running check.'"
    )
    assert "Unable to access AzureWebJobsStorage" in section, (
        "Scaffold section must quote observed description 'Unable to access AzureWebJobsStorage'"
    )

    # And: ties both observations (scaffold output and host log text) to the Core Tools version
    assert " ".join(section.split()).count("Core Tools 4.8.0") >= 2, (
        "Scaffold section must name Core Tools 4.8.0 for the scaffold output and the host log text"
    )

    # And: does NOT contain a hardcoded package version specifier
    assert "azure-functions-langgraph>=" not in section, (
        "Scaffold section must not hardcode 'azure-functions-langgraph>='"
    )
    assert "azure-functions-langgraph==" not in section, (
        "Scaffold section must not hardcode 'azure-functions-langgraph=='"
    )


def test_scaffold_section_links_installation_requirements_heading() -> None:
    # Given: the Quickstart section and installation guide
    quickstart = (ROOT / "docs" / "getting-started.md").read_text()
    installation = (ROOT / "docs" / "installation.md").read_text()
    section = quickstart.split("## Start from a new Functions project", 1)[1].split(
        "## Step 1: Define your graph state", 1
    )[0]

    # When: the installation requirements link and installation anchors are resolved
    expected_target = "installation.md#add-to-your-requirements"
    slug = _slug("Add to your requirements")

    # Then: section links to installation.md#add-to-your-requirements
    assert expected_target in section, f"Scaffold section must link to '{expected_target}'"

    # And: that heading exists in docs/installation.md with matching slug
    headings: list[str] = re.findall(r"^#{1,6}\s+(.+)$", installation, re.M)
    slugs = {_slug(h) for h in headings}
    assert slug == "add-to-your-requirements", (
        f"Heading slug calculation drifted: expected 'add-to-your-requirements', got '{slug}'"
    )
    assert slug in slugs, (
        f"Heading 'Add to your requirements' (slug '{slug}') not found in docs/installation.md"
    )


def test_step_5_exports_function_app_discovered_by_host() -> None:
    # Given: Step 5 of the Quickstart
    quickstart = (ROOT / "docs" / "getting-started.md").read_text()
    step_5 = quickstart.split("## Step 5: Export the function app", 1)[1].split(
        "## Step 6: Run locally", 1
    )[0]

    # When: the Python code block in Step 5 is inspected
    py_blocks: list[str] = re.findall(r"```python\n(.*?)```", step_5, re.DOTALL)
    assert py_blocks, "Step 5 must contain a python code block"

    code_lines: list[str] = [
        line.strip() for line in py_blocks[0].strip().splitlines() if line.strip()
    ]

    # Then: the block ends with app = langgraph_app.function_app
    assert code_lines[-1] == "app = langgraph_app.function_app", (
        "Step 5 code block must end with 'app = langgraph_app.function_app', "
        f"got '{code_lines[-1]}'"
    )


def test_scaffold_file_inventory_matches_issue_923_contract() -> None:
    # Given: the documented file names in the Quickstart scaffold section
    quickstart = (ROOT / "docs" / "getting-started.md").read_text()
    section = quickstart.split("## Start from a new Functions project", 1)[1].split(
        "## Step 1: Define your graph state", 1
    )[0]

    # When: checking each scaffolded file name against the section text
    # Then: all files from the verified Core Tools scaffold are named on the page
    for filename in SCAFFOLD_FILES:
        assert f"`{filename}`" in section or filename in section, (
            f"Scaffold section must list scaffolded file '{filename}' verified in issue #923"
        )
