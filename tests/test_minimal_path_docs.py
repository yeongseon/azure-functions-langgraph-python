"""Doc-drift checks for the single default Azure deployment path.

Issue: #779. The 5-minute tutorial is the one default path; README links it and
``docs/deployment.md`` is the optional advanced guide. Deterministic, no network.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
TUTORIAL = (ROOT / "docs" / "tutorial-5-min.md").read_text()
DEPLOYMENT = (ROOT / "docs" / "deployment.md").read_text()
CHOOSE = (ROOT / "docs" / "choose-a-plan.md").read_text()
FENCE = re.compile(r"```bash\n(.*?)```", re.S)


def _slug(heading: str) -> str:
    text = re.sub(r"[^\w\s-]", "", heading.lower())
    return re.sub(r"[-\s]+", "-", text.strip())


def _anchors(text: str) -> set[str]:
    return {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", text, re.M)}


def test_readme_links_default_path_under_labelled_heading() -> None:
    # Given: the README section named as the default deploy path
    section = README.split("## Deploy to Azure (default path)", 1)[1].split("\n## ", 1)[0]

    # Then: it links the tutorial and the advanced guide, labelled optional
    assert "docs/tutorial-5-min.md" in section
    assert "docs/deployment.md" in section
    assert "optional" in section.lower()


def test_default_path_uses_one_plan_and_no_advanced_facilities() -> None:
    # Given: the commands of the default-path tutorial
    commands = "".join(FENCE.findall(TUTORIAL))

    # Then: exactly one plan is provisioned and no optional infra is required
    assert commands.count("az functionapp create") == 1
    assert "--flexconsumption-location" in commands
    assert "--sku EP1" not in commands and "az functionapp plan create" not in commands
    for advanced in ("platform_compat", "BlobCheckpoint", "TableThread", "Blob", "Table"):
        assert advanced not in commands


def test_default_path_distinguishes_functions_storage_from_app_storage() -> None:
    assert "AzureWebJobsStorage" in TUTORIAL
    assert "optional" in TUTORIAL.lower()
    assert "deployment.md#optional-application-storage" in TUTORIAL
    assert "## Optional application storage" in DEPLOYMENT
    assert "AzureWebJobsStorage" in DEPLOYMENT.split("## Optional application storage", 1)[1]


def test_deployment_guide_opens_as_optional_advanced_guide() -> None:
    # Given: the opening of the deployment guide
    head = DEPLOYMENT.split("## Who this guide is for", 1)[0]

    # Then: newcomers are sent to the tutorial and told advanced items are optional
    assert "tutorial-5-min.md" in head
    assert "do not need anything in this guide" in head
    for advanced in ("Premium", "Blob", "Table", "platform_compat", "Managed Identity"):
        assert advanced in head
    assert "(optional, advanced)" in DEPLOYMENT


def test_plan_recommendations_do_not_contradict_across_docs() -> None:
    # Then: no doc declares Premium the unconditional default any more
    assert "**Default plan** | **Premium (EP1)**" not in DEPLOYMENT
    assert "`azure-functions-langgraph` defaults to **Premium**" not in CHOOSE
    assert "default plan for this minimal demo" in TUTORIAL
    assert "## When to move to Premium" in DEPLOYMENT
    assert "Flex Consumption" in CHOOSE.split("minimal `azure-functions-langgraph` demo", 1)[1][:80]


def test_cloud_certification_is_not_overclaimed() -> None:
    assert "Not cloud-certified here" in TUTORIAL
    assert "COMPATIBILITY.md" in TUTORIAL
    assert "COMPATIBILITY.md" in DEPLOYMENT.split("## Who this guide is for", 1)[0]


def test_limitations_are_noted_briefly() -> None:
    limits = TUTORIAL.split("### Limits to know", 1)[1].split("\n### ", 1)[0]
    for word in ("Cold starts", "HTTP timeout", "buffered SSE"):
        assert word in limits
    assert len(limits.splitlines()) < 20


def test_published_package_vs_candidate_wheel_is_explicit() -> None:
    assert "published" in TUTORIAL.lower()
    assert "candidate wheel" in TUTORIAL


def test_default_path_deployed_invoke_keeps_function_key_and_cleanup() -> None:
    # Given: the commands after the deploy heading
    cloud = TUTORIAL.split("### Call the deployed app", 1)[1]

    # Then: key is fetched before the protected call, and cleanup is shown
    assert cloud.index("az functionapp keys list") < cloud.index("code=$FUNCTION_KEY")
    assert "az group delete" in cloud
    assert "-o tsv" not in TUTORIAL


def test_added_cross_doc_links_and_anchors_resolve() -> None:
    docs = ROOT / "docs"
    for source, text in (
        ("tutorial-5-min.md", TUTORIAL),
        ("deployment.md", DEPLOYMENT),
        ("choose-a-plan.md", CHOOSE),
    ):
        for target, anchor in re.findall(r"\]\(([a-z0-9_-]+\.md)#([^)]+)\)", text):
            page = docs / target
            assert page.is_file(), (source, target)
            assert anchor in _anchors(page.read_text()), (source, target, anchor)
        for anchor in re.findall(r"\]\(#([^)]+)\)", text):
            assert anchor in _anchors(text), (source, anchor)
    for target in re.findall(r"\]\((docs/[a-z0-9_-]+\.md)\)", README):
        assert (ROOT / target).is_file(), target
