import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Final

import azure.functions as func
import pytest

ROOT: Final = Path(__file__).resolve().parents[1]


@pytest.fixture
def quickstart_snippet() -> str:
    source = (ROOT / "README.md").read_text(encoding="utf-8")
    section = source.split("## Quick Start\n", 1)[1].split("\n## ", 1)[0]
    return section.split("```python\n", 1)[1].split("```", 1)[0]


def test_copied_quickstart_invokes_registered_native_route(
    quickstart_snippet: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the verbatim snippet in a fresh module, restored after this test.
    module = ModuleType("_readme_quickstart")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(quickstart_snippet, "README.md", "exec", dont_inherit=True), module.__dict__)
    function_app = module.func_app
    assert isinstance(function_app, func.FunctionApp)
    function_app.functions_bindings = {}
    handlers = {
        fn.get_function_name(): fn.get_user_function() for fn in function_app.get_functions()
    }

    # When: the registered native handler receives the documented HTTP request.
    response = handlers["aflg_echo_invoke"](
        func.HttpRequest(
            method="POST",
            url="/api/graphs/echo/invoke",
            body=b'{"input":{"message":"hello"}}',
            headers={"Content-Type": "application/json"},
            route_params={"name": "echo"},
        )
    )

    # Then: registration and invocation produce the documented result.
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"output": {"message": "hello"}}


def test_copied_quickstart_is_ruff_format_clean(quickstart_snippet: str, tmp_path: Path) -> None:
    # Given: the verbatim Python fence saved as the documented filename.
    snippet_path = tmp_path / "function_app.py"
    snippet_path.write_text(quickstart_snippet, encoding="utf-8")

    # When: the project's formatter checks the copied file without changing it.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            "--config",
            str(ROOT / "pyproject.toml"),
            str(snippet_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    # Then: a reader can use the copy without reformatting it.
    assert result.returncode == 0, result.stdout + result.stderr
