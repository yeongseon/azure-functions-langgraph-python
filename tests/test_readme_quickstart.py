import json
from pathlib import Path

import azure.functions as func


def test_readme_quickstart_runs_through_registered_handler() -> None:
    source = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    snippet = source.split("```python\n", 1)[1].split("```", 1)[0]
    namespace: dict[str, func.FunctionApp] = {}
    exec(compile(snippet, "README.md", "exec"), namespace)  # noqa: S102
    handlers = {
        fn.get_function_name(): fn.get_user_function()
        for fn in namespace["func_app"].get_functions()
    }
    response = handlers["aflg_echo_invoke"](
        func.HttpRequest(
            method="POST",
            url="/api/graphs/echo/invoke",
            body=b'{"input":{"message":"hello"}}',
        )
    )
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"output": {"message": "hello"}}
