"""An example that runs in a test must not change what the next example imports (#833).

Every example's ``function_app.py`` does ``from graph import ...``. Before this, the first example
that ran left its own ``graph`` in ``sys.modules`` and a later one, such as the production example
once Azurite made its tests run, failed with ``cannot import name 'build_graph' from 'graph'``.
The loader forgets the example-local modules before it runs a file, and an autouse fixture in
``conftest.py`` puts ``sys.modules`` and ``sys.path`` back after every test.

The last two tests are a pair that depends on definition order, which pytest keeps within a
module: the first runs an example and leaves its modules imported, the second checks that the
fixture took them away.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType

from tests.example_imports import (
    EXAMPLE_LOCAL_MODULES,
    load_example_file,
    restore_example_imports,
    take_import_snapshot,
)

BEFORE_THE_LEAKY_TEST: dict[str, ModuleType | None] = {}


def _write_example(root: Path, name: str, value: str) -> Path:
    directory = root / name
    directory.mkdir()
    (directory / "model_config.py").write_text(f"MODEL = {value!r}\n")
    (directory / "tools.py").write_text(f"TOOLS = [{value!r}]\n")
    (directory / "graph.py").write_text(
        "from model_config import MODEL\n"
        "from tools import TOOLS\n"
        f"NAME = {value!r}\n"
        "def build_graph():\n"
        "    return NAME, MODEL, TOOLS\n"
    )
    (directory / "function_app.py").write_text(
        "from graph import NAME, build_graph\nLOADED = (NAME, build_graph())\n"
    )
    return directory / "function_app.py"


def test_two_examples_that_each_define_a_graph_module_each_get_their_own(tmp_path: Path) -> None:
    # Given two examples whose function_app.py both do `from graph import ...`.
    first = _write_example(tmp_path, "first", "first")
    second = _write_example(tmp_path, "second", "second")

    # When they are loaded one after the other in one process.
    one = load_example_file(first, "_isolation_first_function_app")
    two = load_example_file(second, "_isolation_second_function_app")

    # Then each one gets the graph, model_config and tools of its own directory.
    assert one.LOADED == ("first", ("first", "first", ["first"]))
    assert two.LOADED == ("second", ("second", "second", ["second"]))


def test_the_loader_puts_the_example_directory_on_sys_path_only_while_the_file_runs(
    tmp_path: Path,
) -> None:
    # Given an example and the path as it is.
    function_app = _write_example(tmp_path, "only", "only")
    before = list(sys.path)

    # When the example is loaded.
    load_example_file(function_app, "_isolation_only_function_app")

    # Then its directory is not left on sys.path.
    assert sys.path == before


def test_restore_puts_sys_modules_and_sys_path_back_after_an_example_ran(tmp_path: Path) -> None:
    # Given a snapshot taken before an example runs, and an example that leaves modules behind.
    snapshot = take_import_snapshot()
    function_app = _write_example(tmp_path, "left_behind", "left")
    load_example_file(function_app, "_isolation_left_behind_function_app")
    sys.path.insert(0, str(tmp_path / "left_behind"))
    assert all(name in sys.modules for name in EXAMPLE_LOCAL_MODULES)

    # When the test is over.
    restore_example_imports(snapshot, roots=[tmp_path])

    # Then graph, model_config and tools are as they were, the example's own module is gone, and
    # its directory is off sys.path.
    for name in EXAMPLE_LOCAL_MODULES:
        assert sys.modules.get(name) is snapshot.modules.get(name)
    assert "_isolation_left_behind_function_app" not in sys.modules
    assert str(tmp_path / "left_behind") not in sys.path


def test_restore_gives_back_a_graph_module_that_existed_before(tmp_path: Path) -> None:
    # Given a graph module that was already imported, and an example that replaces it.
    placeholder = ModuleType("graph")
    sys.modules["graph"] = placeholder
    snapshot = take_import_snapshot()
    function_app = _write_example(tmp_path, "replaces", "replaced")
    load_example_file(function_app, "_isolation_replaces_function_app")
    assert sys.modules["graph"] is not placeholder

    # When the test is over.
    restore_example_imports(snapshot, roots=[tmp_path])

    # Then the earlier module is back, not removed.
    assert sys.modules["graph"] is placeholder


def test_restore_leaves_a_library_that_was_imported_for_the_first_time_alone(
    tmp_path: Path,
) -> None:
    # Given a module from outside the example roots that appears while a test runs.
    snapshot = take_import_snapshot()
    library = ModuleType("_isolation_library_imported_for_the_first_time")
    library.__file__ = "/usr/lib/python3/site-packages/library.py"
    sys.modules[library.__name__] = library
    try:
        # When the example imports are restored.
        restore_example_imports(snapshot, roots=[tmp_path])

        # Then the library is still imported.
        assert sys.modules[library.__name__] is library
    finally:
        del sys.modules[library.__name__]


def test_an_example_that_runs_leaves_its_graph_module_imported_within_the_test(
    tmp_path: Path,
) -> None:
    # Given an example that this test runs, and what the process had before it.
    function_app = _write_example(tmp_path, "leaky", "leaky")
    for name in EXAMPLE_LOCAL_MODULES:
        BEFORE_THE_LEAKY_TEST[name] = sys.modules.get(name)

    # When it runs.
    load_example_file(function_app, "_isolation_leaky_function_app")

    # Then its modules are imported for the rest of this test: the fixture takes them away later.
    assert sys.modules["graph"].NAME == "leaky"


def test_the_next_test_does_not_inherit_the_modules_of_the_example_that_ran_before_it() -> None:
    # Given the test above ran an example and left its graph, model_config and tools imported.
    assert BEFORE_THE_LEAKY_TEST, "this test follows the one above and needs what it recorded"

    # Then this test starts from what the process had before that test, not from the example's.
    for name in EXAMPLE_LOCAL_MODULES:
        assert sys.modules.get(name) is BEFORE_THE_LEAKY_TEST[name]
