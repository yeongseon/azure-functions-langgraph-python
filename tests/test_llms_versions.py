from pathlib import Path
import re
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
ROOT_REFERENCES: Final = ("llms.txt", "llms-full.txt")
PACKAGE: Final = r"azure[-_]functions[-_]langgraph(?:\[[^\]]+\])?"
HARD_CODED_VERSION: Final = re.compile(
    rf"(?:{PACKAGE}[^\n]*?\d+\.\d+\.\d+"
    r"|(?:^|\n)[^\n]*(?:\bversion\b|__version__)[^\n]*?\d+\.\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


@pytest.mark.parametrize("filename", (*ROOT_REFERENCES, "docs/llms.txt"))
def test_llms_version_signals_have_no_hard_coded_package_version(filename: str) -> None:
    # Given: a machine-readable reference, including the separately published docs file
    reference = ROOT / filename

    # When: package version claims are inspected (Python/runtime versions are unrelated)
    claims = HARD_CODED_VERSION.findall(reference.read_text(encoding="utf-8"))

    # Then: a release never leaves behind a numeric package version claim
    assert claims == [], f"{filename} embeds a package version: {claims}"


@pytest.mark.parametrize("filename", ROOT_REFERENCES)
def test_llms_version_signals_resolve_authoritative_metadata(filename: str) -> None:
    # Given: either root reference consumed as LLM context
    text = (ROOT / filename).read_text(encoding="utf-8")

    # When: the version metadata fields are parsed
    fields = dict(re.findall(r"^- ([^:\n]+): (.+)$", text, re.MULTILINE))

    # Then: published and installed versions resolve independently of any release number
    assert re.findall(r"https://\S+", fields["Version"]) == [
        "https://pypi.org/project/azure-functions-langgraph/"
    ]
    assert re.findall(r"`([^`]+)`", fields["Installed version"]) == [
        'importlib.metadata.version("azure-functions-langgraph")'
    ]


def test_llms_version_signals_agree_between_root_references() -> None:
    # Given: the concise and full references have deliberately different content
    texts = [(ROOT / filename).read_text(encoding="utf-8") for filename in ROOT_REFERENCES]

    # When: only their version metadata fields are extracted
    signals = [re.findall(r"^- (?:Installed version|Version): .+$", t, re.MULTILINE) for t in texts]

    # Then: neither reference can independently change the shared version signal
    assert len(signals[0]) == 2
    assert signals[0] == signals[1]
