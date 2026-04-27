from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

GRAPH_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("simple_agent", "compiled_graph"),
    ("platform_compat_sdk", "compiled_graph"),
    ("openapi_bridge", "compiled_graph"),
    ("production_auth", "private_graph"),
    ("production_auth", "public_graph"),
)


def _load_module(example_dir: Path, module_name: str) -> object:
    path = example_dir / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(
        f"_examples_{example_dir.name}_{module_name}", path
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("example,attr", GRAPH_EXAMPLES)
def test_example_graph_compiles(example: str, attr: str) -> None:
    example_dir = EXAMPLES_DIR / example
    assert example_dir.is_dir(), f"missing example directory: {example}"

    sys.path.insert(0, str(example_dir))
    try:
        mod = _load_module(example_dir, "graph")
    finally:
        sys.path.pop(0)

    graph = getattr(mod, attr)
    assert graph is not None
    assert hasattr(graph, "invoke"), f"{example}.{attr} missing invoke()"


REQUIRED_FILES = (
    "README.md",
    "function_app.py",
    "host.json",
    "requirements.txt",
)

EXAMPLE_DIRS = (
    "simple_agent",
    "platform_compat_sdk",
    "persistent_agent_blob_table",
    "managed_identity_storage",
    "openapi_bridge",
    "production_auth",
)


@pytest.mark.parametrize("example", EXAMPLE_DIRS)
def test_example_has_required_files(example: str) -> None:
    example_dir = EXAMPLES_DIR / example
    for name in REQUIRED_FILES:
        assert (example_dir / name).is_file(), f"{example}/{name} missing"


def test_local_curl_scripts_executable() -> None:
    curl_dir = EXAMPLES_DIR / "local_curl"
    scripts = sorted(curl_dir.glob("*.sh"))
    assert scripts, "local_curl/ should contain shell scripts"
    for script in scripts:
        mode = script.stat().st_mode
        assert mode & 0o111, f"{script} is not executable"
