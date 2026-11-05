"""Regression checks that keep ``docs/deployment.md`` copy-pasteable.

Issue: #778. Covers deployed-route authorization, the local settings prerequisite,
referenced file paths and documented routes versus the real route table.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "deployment.md"
TEXT = DOC.read_text()
FENCE = re.compile(r"```bash\n(.*?)```", re.S)


def _deployed_section() -> str:
    return TEXT.split("### Step 10", 1)[1].split("### Step 13", 1)[0]


def _curl_commands(block: str) -> list[str]:
    commands: list[str] = []
    for body in FENCE.findall(block):
        # Join backslash continuations so each curl is a single logical command.
        joined = body.replace("\\\n", " ")
        commands.extend(line for line in joined.splitlines() if "curl " in line)
    return commands


def test_every_protected_deployed_curl_sends_a_function_key() -> None:
    # Given: the curl examples that target the deployed $BASE_URL
    commands = [c for c in _curl_commands(_deployed_section()) if "$BASE_URL/api/" in c]

    # When: protected (non-health) examples are selected
    protected = [c for c in commands if '$BASE_URL/api/health"' not in c]

    # Then: all of them carry a key, and the anonymous probe stays keyless
    assert len(protected) >= 8
    missing = [c for c in protected if "x-functions-key" not in c and "code=" not in c]
    assert missing == []
    anonymous = [c for c in commands if '$BASE_URL/api/health"' in c]
    assert anonymous
    assert all("x-functions-key" not in c and "code=" not in c for c in anonymous)


def test_function_key_is_acquired_before_first_protected_call() -> None:
    section = _deployed_section()
    assert section.index("az functionapp keys list") < section.index("x-functions-key:")


def test_local_settings_prerequisite_is_not_masked() -> None:
    # Given: every bash block that touches local.settings.json
    blocks = [b for b in FENCE.findall(TEXT) if "local.settings.json" in b]

    # Then: it is written explicitly and never hidden behind || true / 2>/dev/null
    assert blocks
    for block in blocks:
        assert "|| true" not in block
        assert "2>/dev/null" not in block
        assert "local.settings.json.example" not in block
    assert "AZURE_STORAGE_CONNECTION_STRING" in "".join(blocks)


def test_referenced_repo_paths_exist() -> None:
    # Given: the example directory the walkthrough copies
    assert "cp -r examples/simple_agent" in TEXT

    # Then: it exists with the files the guide relies on
    example = ROOT / "examples" / "simple_agent"
    for name in ("function_app.py", "graph.py", "requirements.txt", "local.settings.json"):
        assert (example / name).is_file()
    for target in re.findall(r"\]\(([a-z0-9_./-]+\.md)(?:#[^)]*)?\)", TEXT):
        assert (DOC.parent / target).is_file(), target


def test_documented_routes_exist_in_route_table() -> None:
    # Given: route templates registered by the package
    registered = set()
    for path in (ROOT / "src").rglob("*.py"):
        source = path.read_text()
        registered.update(re.findall(r'route=f?"([^"]+)"', source))
        registered.update(re.findall(r'^_ROUTE_\w+ = "([^"]+)"', source, re.M))
    registered_norm = {re.sub(r"\{[^}]+\}", "{}", r) for r in registered}

    # When: documented deployed URLs are normalised the same way
    documented = set()
    for command in _curl_commands(_deployed_section()):
        match = re.search(r'\$BASE_URL/api/([^"?]+)', command)
        if match:
            documented.add(re.sub(r"simple_agent|\$\w+", "{}", match.group(1)))

    # Then: every documented route is a real one
    assert documented
    assert documented - registered_norm == set()


def test_buffered_sse_caveat_is_present() -> None:
    assert "buffered SSE" in TEXT
    assert "StreamingLangGraphApp" in TEXT
    assert "experimental" in TEXT
