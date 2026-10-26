import json
from typing import Literal

import azure.functions as func
import pytest

from tests.test_native_run_authorization import Scenario


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
async def test_deep_json_is_rejected_without_execution(
    endpoint: Literal["invoke", "stream"], async_mode: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(lambda context: True, "thread-one")
    scenario.request = func.HttpRequest(
        method="POST", url="/invoke", body=b'{"input":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}"
    )

    def exhausted_decoder(self: func.HttpRequest) -> None:
        raise RecursionError("decoder stack exhausted")

    monkeypatch.setattr(func.HttpRequest, "get_json", exhausted_decoder)
    response = await scenario.run(endpoint, async_mode)
    assert response.status_code == 400
    assert scenario.calls == []
    assert scenario.lock.attempts == 0
    assert "RecursionError" not in json.dumps(json.loads(response.get_body()))
