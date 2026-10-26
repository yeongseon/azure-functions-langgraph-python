import json
from typing import Literal

import azure.functions as func
import pytest

from tests.test_native_run_authorization import Scenario


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("case", ["valid", "malformed", "denied", "contention"])
async def test_native_preflight_characterization(
    endpoint: Literal["invoke", "stream"], async_mode: bool, case: str
) -> None:
    scenario = Scenario(lambda context: case != "denied", "thread-one")
    if case == "malformed":
        scenario.request = func.HttpRequest(method="POST", url="/invoke", body=b"{")
    if case == "contention":
        assert scenario.lock.acquire("agent", "thread-one") is not None
    response = await scenario.run(endpoint, async_mode)
    assert (
        response.status_code
        == {"valid": 200, "malformed": 400, "denied": 403, "contention": 409}[case]
    )
    assert len(scenario.calls) == (1 if case == "valid" else 0)
    assert scenario.observer.started == (1 if case == "valid" else 0)
    if case == "malformed":
        assert json.loads(response.get_body())["detail"] == "Invalid JSON body"
    if case == "denied":
        assert scenario.lock.attempts == 0
        assert json.loads(response.get_body())["detail"] == "Access denied"
