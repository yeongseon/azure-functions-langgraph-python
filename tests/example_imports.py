"""Import an example's code without leaving its sibling modules behind (#833).

An example's ``function_app.py`` does ``from graph import ...`` and its ``graph.py`` imports
``model_config`` and ``tools`` by those bare names, so the first example that runs in a process
leaves its own ``graph`` in ``sys.modules`` and the next example imports the wrong one. The loader
forgets those three names before it runs a file, and ``restore_example_imports`` (used by the
autouse fixture in ``conftest.py``) puts ``sys.modules`` and ``sys.path`` back after each test.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType

EXAMPLE_LOCAL_MODULES = ("graph", "model_config", "tools")


@dataclass(frozen=True)
class ImportSnapshot:
    path: tuple[str, ...]
    modules: dict[str, ModuleType]


def take_import_snapshot() -> ImportSnapshot:
    return ImportSnapshot(path=tuple(sys.path), modules=dict(sys.modules))


def forget_example_local_modules() -> None:
    for name in EXAMPLE_LOCAL_MODULES:
        sys.modules.pop(name, None)


def load_example_file(path: Path, module_name: str) -> ModuleType:
    """Run one file of an example as ``module_name``, with the example's directory importable.

    The directory is on ``sys.path`` only while the file runs, and the example's sibling modules
    are imported fresh, never taken from an example that ran earlier.
    """
    forget_example_local_modules()
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    entry = str(path.parent)
    sys.path.insert(0, entry)
    try:
        spec.loader.exec_module(module)
    finally:
        if entry in sys.path:
            sys.path.remove(entry)
    return module


def restore_example_imports(snapshot: ImportSnapshot, roots: Iterable[Path]) -> None:
    """Undo what a test imported from an example: its modules and its directories on ``sys.path``.

    Only what lives under ``roots`` is removed, so a library that a test happened to import for
    the first time is never dropped. ``graph``, ``model_config`` and ``tools`` go back to what they
    were, whoever defined them.
    """
    resolved = tuple(os.path.realpath(root) for root in roots)

    def inside(location: str) -> bool:
        real = os.path.realpath(location)
        return any(real == root or real.startswith(root + os.sep) for root in resolved)

    sys.path[:] = [
        entry for entry in sys.path if entry in snapshot.path or not (entry and inside(entry))
    ]
    for name in set(sys.modules) - set(snapshot.modules):
        origin = getattr(sys.modules[name], "__file__", None)
        if isinstance(origin, str) and inside(origin):
            del sys.modules[name]
    for name in EXAMPLE_LOCAL_MODULES:
        previous = snapshot.modules.get(name)
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
