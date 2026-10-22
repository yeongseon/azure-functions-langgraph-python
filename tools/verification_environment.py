from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import sys


def doctor() -> int:
    missing = []
    for module in ("ruff", "mypy", "pytest", "bandit", "build", "mkdocs"):
        available = importlib.util.find_spec(module) is not None
        print(f"{module}: {'available' if available else 'MISSING'} (check/test/build/docs)")
        if not available:
            missing.append(module)
    for tool, target in (
        ("docker", "Cosmos/Compose integration"),
        ("azurite", "integration/host-smoke"),
        ("func", "host-smoke/verify"),
    ):
        available = shutil.which(tool) is not None
        print(f"{tool}: {'available' if available else 'MISSING'} (required for {target})")
        if not available:
            missing.append(tool)
    print(f"Python: {sys.version.split()[0]}; doctor is read-only")
    return int(bool(missing))


def clean(root: Path) -> None:
    for name in (
        "test-results",
        "dist",
        "build",
        "site",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".coverage",
        "coverage.xml",
        "htmlcov",
        "__pycache__",
    ):
        path = root / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
