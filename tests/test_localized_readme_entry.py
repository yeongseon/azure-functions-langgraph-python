from pathlib import Path
import re
from typing import Final

import pytest

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
LOCALIZED_READMES: Final[list[str]] = ["README.ko.md", "README.ja.md", "README.zh-CN.md"]

EXPECTED_BANNERS: Final[dict[str, str]] = {
    "README.ko.md": (
        "> 이 번역은 커뮤니티가 관리하며 최신 [English README](README.md)보다 뒤처질 수 있습니다. "
        "영어 원문이 기준입니다."
    ),
    "README.ja.md": (
        "> この翻訳はコミュニティ管理で、最新の [English README](README.md) "
        "より古い場合があります。英語版が正本です。"
    ),
    "README.zh-CN.md": (
        "> 此翻译由社区维护，可能落后于最新的 [English README](README.md)。英文版是权威来源。"
    ),
}


def extract_quickstart_fence(content: str) -> str:
    parts = content.split("```python\n", 1)
    if len(parts) < 2:
        return ""
    return parts[1].split("```", 1)[0]


def extract_relative_link_targets(content: str) -> list[str]:
    matches = re.findall(r"\[.*?\]\(([^)]+)\)", content)
    targets: list[str] = []
    for raw in matches:
        link = raw.strip()
        if link.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = link.split("#", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


@pytest.fixture
def canonical_quickstart_fence() -> str:
    content = (ROOT / "README.md").read_text(encoding="utf-8")
    return extract_quickstart_fence(content)


@pytest.mark.parametrize("readme_filename", LOCALIZED_READMES)
def test_localized_readme_quickstart_is_byte_identical(
    readme_filename: str, canonical_quickstart_fence: str
) -> None:
    # Given: the localized README and the canonical English README snippet.
    readme_path = ROOT / readme_filename
    content = readme_path.read_text(encoding="utf-8")

    # When: extracting the Python quickstart fence from the localized README.
    snippet = extract_quickstart_fence(content)

    # Then: the extracted snippet must be byte-identical to the English Quick Start snippet.
    assert snippet != "", f"No ```python fence found in {readme_filename}"
    assert snippet == canonical_quickstart_fence


@pytest.mark.parametrize("readme_filename", LOCALIZED_READMES)
def test_localized_readme_has_staleness_banner(readme_filename: str) -> None:
    # Given: the localized README content.
    readme_path = ROOT / readme_filename
    content = readme_path.read_text(encoding="utf-8")

    # When: checking for the staleness banner linking to English README.
    expected_banner = EXPECTED_BANNERS[readme_filename]

    # Then: the exact staleness banner linking to README.md must be present.
    assert expected_banner in content


@pytest.mark.parametrize("readme_filename", LOCALIZED_READMES)
def test_localized_readme_has_install_command(readme_filename: str) -> None:
    # Given: the localized README content.
    readme_path = ROOT / readme_filename
    content = readme_path.read_text(encoding="utf-8")

    # When: checking for the published package install command and Python range.
    has_install = "pip install azure-functions-langgraph" in content
    has_python_range = "3.11–3.14" in content or "3.11-3.14" in content

    # Then: both the install command and the supported Python range must be present.
    assert has_install, f"Install command missing from {readme_filename}"
    assert has_python_range, f"Supported Python range missing from {readme_filename}"


@pytest.mark.parametrize("readme_filename", LOCALIZED_READMES)
def test_localized_readme_relative_links_exist(readme_filename: str) -> None:
    # Given: the localized README content.
    readme_path = ROOT / readme_filename
    content = readme_path.read_text(encoding="utf-8")

    # When: extracting all relative links in the document.
    targets = extract_relative_link_targets(content)

    # Then: every relative link target must exist in the repository.
    assert len(targets) > 0, f"No relative links found in {readme_filename}"
    missing = [t for t in targets if not (ROOT / t).exists()]
    assert not missing, f"Missing relative link targets in {readme_filename}: {missing}"


@pytest.mark.parametrize("readme_filename", LOCALIZED_READMES)
def test_localized_readme_links_official_example_and_tutorial(readme_filename: str) -> None:
    # Given: the localized README content.
    readme_path = ROOT / readme_filename
    content = readme_path.read_text(encoding="utf-8")

    # When: inspecting entry-path reference links.
    has_tutorial = "docs/tutorial-5-min.md" in content
    has_official_example = "examples/official_langgraph_basic/" in content

    # Then: both entry paths must be clearly linked.
    assert has_tutorial, f"Tutorial link missing from {readme_filename}"
    assert has_official_example, f"Official example link missing from {readme_filename}"


@pytest.mark.parametrize("readme_filename", LOCALIZED_READMES)
def test_localized_readme_has_quickstart_execution_and_result(readme_filename: str) -> None:
    # Given: the localized README content.
    readme_path = ROOT / readme_filename
    content = readme_path.read_text(encoding="utf-8")

    # When: checking for the quickstart invocation command and expected output.
    has_func_start = "func start" in content
    has_invoke_route = "/api/graphs/echo/invoke" in content
    has_input = '{"input":{"message":"hello"}}' in content
    has_output = '{"output":{"message":"hello"}}' in content

    # Then: all invocation elements must be present.
    assert has_func_start, f"func start missing from {readme_filename}"
    assert has_invoke_route, f"Invoke route missing from {readme_filename}"
    assert has_input, f"Input payload missing from {readme_filename}"
    assert has_output, f"Expected output payload missing from {readme_filename}"


@pytest.mark.parametrize("readme_filename", LOCALIZED_READMES)
def test_localized_readme_links_stability_table(readme_filename: str) -> None:
    # Given: the localized README content.
    readme_path = ROOT / readme_filename
    content = readme_path.read_text(encoding="utf-8")

    # When: checking for a link to the canonical English stability table.
    has_stability_link = "README.md#stability--rolling-0x-per-surface" in content

    # Then: the link to the canonical stability table must be present.
    assert has_stability_link, f"Stability table link missing from {readme_filename}"
