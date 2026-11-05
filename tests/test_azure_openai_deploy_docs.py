"""Doc-smoke checks for the Azure OpenAI example deployment walkthrough.

Issue: #780. Deterministic, no network. Cross-checks the example README against
the settings the example code actually reads.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "azure_openai_agent"
README_PATH = EXAMPLE / "README.md"
README = README_PATH.read_text()
CONFIG = (EXAMPLE / "model_config.py").read_text()
FENCE = re.compile(r"```(\w+)\n(.*?)```", re.S)
DEPLOY = README.split("## Deploy to Azure", 1)[1].split("\n## ", 1)[0]


def _slug(heading: str) -> str:
    """GitHub heading anchor: lowercase, drop punctuation, each space becomes one hyphen.

    Adjacent separators are NOT collapsed, so "Step 5 — Deploy" is ``step-5--deploy``.
    """
    text = re.sub(r"[^\w\s-]", "", heading.lower())
    return text.strip().replace(" ", "-")


def _fences(language: str) -> list[str]:
    return [body for lang, body in FENCE.findall(README) if lang == language]


def test_readme_names_every_setting_the_code_reads() -> None:
    # Given: environment variable names read by the example's loader
    read = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', CONFIG))

    # Then: the README documents each (and the template file carries them)
    assert {"AGENT_MODEL_MODE", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_USE_ENTRA_ID"} <= read
    for name in read:
        assert f"`{name}`" in README, name
    template = (EXAMPLE / "local.settings.json.example").read_text()
    for name in read:
        assert name in template, name


def test_cloud_settings_use_only_names_the_code_reads() -> None:
    # Given: KEY=value tokens in `az ... appsettings set` commands
    read = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', CONFIG))
    blocks = [b for b in _fences("bash") if "appsettings set" in b]
    assert len(blocks) == 2

    # Then: every setting assigned is one the code reads
    for block in blocks:
        assigned = set(re.findall(r"^\s+([A-Z][A-Z_]+)=", block.replace("\\\n", "\n"), re.M))
        assert "AGENT_MODEL_MODE" in assigned
        assert assigned <= read, assigned - read


def test_secret_never_echoed_and_never_published_from_local_file() -> None:
    for block in (b for b in _fences("bash") if "appsettings set" in b):
        assert "--output none" in block
    assert "--publish-local-settings" in DEPLOY
    assert "do not pass it" in DEPLOY
    assert not re.search(r"AZURE_OPENAI_API_KEY=[^\"$<\s]", README)


def test_az_commands_use_long_flags_only() -> None:
    short = re.compile(r"(?<![\w-])-[a-zA-Z]\b")
    for block in _fences("bash"):
        for line in block.replace("\\\n", " ").splitlines():
            if line.lstrip().startswith(("az ", "func ", "PRINCIPAL", "AOAI")) and "az " in line:
                assert not short.search(re.sub(r'"[^"]*"', "", line)), line


def test_managed_identity_is_least_privilege() -> None:
    assert '--role "Cognitive Services OpenAI User"' in README
    assert '--scope "$AOAI_ID"' in README
    assert "Contributor" not in "".join(b for b in _fences("bash") if "role assignment" in b)
    assert '--identities "[system]"' in README
    assert "learn.microsoft.com/en-us/" in README


def test_same_example_directory_is_run_and_published() -> None:
    local = README.split("## Run locally", 1)[1].split("## Test", 1)[0]
    assert "cd examples/azure_openai_agent" in local
    assert "func start" in local
    publish = DEPLOY.split("func azure functionapp publish", 1)[0]
    assert "cd examples/azure_openai_agent" in publish[-120:]
    assert "simple_agent" not in "".join(b for b in _fences("bash") if "publish" in b)


def test_no_fake_fallback_for_live_results() -> None:
    lowered = README.lower()
    assert "falls back to fake" not in lowered
    assert "fall back to the fake" not in lowered
    flat = " ".join(README.split())
    assert "never downgrades to the fake model" in flat
    assert "does not switch to the fake model" in flat


def test_verification_status_labels_cloud_unverified() -> None:
    status = README.split("### Verification status", 1)[1]
    assert "UNVERIFIED" in status
    assert "not evidence of a real-model cloud deployment" in " ".join(status.split())
    assert "Verified" in status


def test_relative_links_and_anchors_resolve() -> None:
    for target, anchor in re.findall(r"\]\((\.\./[^)#]+\.md)(?:#([^)]+))?\)", README):
        page = (EXAMPLE / target).resolve()
        assert page.is_file(), target
        if anchor:
            headings = re.findall(r"^#{1,6} (.+)$", page.read_text(), re.M)
            assert anchor in {_slug(h) for h in headings}, (target, anchor)
    for anchor in re.findall(r"\]\(#([^)]+)\)", README):
        assert anchor in {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", README, re.M)}


def test_variables_are_defined_before_either_authentication_choice() -> None:
    text = README
    first_choice = text.index("**Managed Identity (recommended for production")
    for name in ("RESOURCE_GROUP", "APP", "AOAI_NAME", "AOAI_RG"):
        assert f'{name}="' in text[:first_choice], name


def test_managed_identity_path_removes_a_stored_api_key() -> None:
    # appsettings set merges and the app prefers a stored key over Entra ID (graph.py).
    text = README
    mi = text[
        text.index("**Managed Identity (recommended for production") : text.index(
            "**API key (quick tests only"
        )
    ]
    assert "appsettings delete" in mi
    assert "--setting-names AZURE_OPENAI_API_KEY" in mi
