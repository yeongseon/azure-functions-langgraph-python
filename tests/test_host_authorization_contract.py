from pathlib import Path
import runpy

import azure.functions as func


def test_host_fixture_rejects_missing_identity_without_canary() -> None:
    # Given: the same app that the real-host runner copies into its owned directory.
    fixture = runpy.run_path(str(Path(__file__).parent / "host_smoke/function_app.py"))
    handlers = {
        function.get_function_name(): function.get_user_function()
        for function in fixture["app"].get_functions()
    }
    # When: state is requested without the smoke fixture's explicit owner identity.
    response = handlers["aflg_smoke_state"](
        func.HttpRequest(
            method="GET",
            url="http://localhost/api/graphs/smoke/threads/host-smoke/state",
            body=b"",
            route_params={"thread_id": "host-smoke"},
        )
    )
    # Then: the host proof exercises a real callback denial, not a validation error.
    assert response.status_code == 401
    assert b"host-credential-canary" not in response.get_body()
