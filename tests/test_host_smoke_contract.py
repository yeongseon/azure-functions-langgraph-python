from __future__ import annotations

import json
from pathlib import Path
import re
import runpy

import azure.functions as func


def test_ci_host_request_matches_checkpointed_fixture() -> None:
    # Given the exact JSON payload CI sends to the checkpointed smoke graph.
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci-test.yml").read_text()
    payload = re.search(r"--data '([^']+)'", workflow)
    assert payload is not None
    fixture = runpy.run_path(str(root / "tests/host_smoke/function_app.py"))
    handlers = {
        fn.get_function_name(): fn.get_user_function() for fn in fixture["app"].get_functions()
    }
    # When the registered native handler receives CI's request.
    response = handlers["aflg_smoke_invoke"](
        func.HttpRequest(
            method="POST",
            url="http://localhost/api/graphs/smoke/invoke",
            body=payload[1].encode(),
            headers={"Content-Type": "application/json"},
            route_params={"name": "smoke"},
        )
    )
    # Then it runs the real graph rather than returning a checkpoint configuration error.
    assert response.status_code == 200, response.get_body()
    assert (
        json.loads(response.get_body())["output"]["messages"][-1]["content"] == "host smoke passed"
    )
