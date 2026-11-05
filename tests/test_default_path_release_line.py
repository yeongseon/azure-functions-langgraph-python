"""The default-path example, primary examples and install guide share one release line (#793).

The package bound in an example's ``requirements.txt`` is an explicit compatibility decision
(Release Please does not manage it), so this check pins consistency, not currency: a newcomer's
first install must not silently land on an older minor line than the one the other onboarding
artifacts target.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SPEC = re.compile(r"^azure-functions-langgraph(?:\[[^\]]+\])?\s*([<>=!~][^\s#]*)\s*$", re.M)


def _specifiers(text: str) -> set[str]:
    return set(SPEC.findall(text))


def _primary_examples() -> list[str]:
    source = (ROOT / "scripts" / "certify_primary_examples.py").read_text()
    block = re.search(r"PRIMARY_EXAMPLES: Final = \((.*?)\)", source, re.S)
    assert block is not None
    return re.findall(r'"([a-z0-9_]+)"', block.group(1))


def test_default_path_example_uses_a_single_release_line() -> None:
    requirements = (ROOT / "examples" / "simple_agent" / "requirements.txt").read_text()
    assert len(_specifiers(requirements)) == 1


def test_default_path_matches_the_primary_examples() -> None:
    default = _specifiers((ROOT / "examples" / "simple_agent" / "requirements.txt").read_text())
    primary = _primary_examples()
    assert len(primary) == 4
    for name in primary:
        text = (ROOT / "examples" / name / "requirements.txt").read_text()
        assert _specifiers(text) == default, name


def test_install_guide_matches_the_default_path_example() -> None:
    default = _specifiers((ROOT / "examples" / "simple_agent" / "requirements.txt").read_text())
    guide = (ROOT / "docs" / "installation.md").read_text()
    assert _specifiers(guide) == default
    # The guide must not keep claiming the examples use an older line.
    assert "0.10.0,<0.11" not in guide
