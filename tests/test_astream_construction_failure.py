from collections.abc import AsyncIterator

from azure_functions_langgraph import LangGraphApp
from tests.test_async_stream_cleanup import _request, _SingleOwnerLock
from tests.test_native_run_authorization import Observer


class BrokenStreamGraph:
    checkpointer = "checkpointed"

    async def ainvoke(self, input: dict[str, str], config: None = None) -> dict[str, str]:
        return input

    def astream(
        self, input: dict[str, str], config: None = None, stream_mode: str = "values"
    ) -> AsyncIterator[dict[str, str]]:
        raise RuntimeError("secret-construction-failure")


async def test_immediate_astream_failure_is_sanitized_and_releases_owner() -> None:
    lock = _SingleOwnerLock()
    observer = Observer()
    app = LangGraphApp(thread_lock=lock, observer=observer)
    app.register(BrokenStreamGraph(), "agent", async_mode=True)
    response = await app._handle_stream_async(_request(), app._registrations["agent"])
    assert response.status_code == 200
    assert b"event: error" in response.get_body()
    assert b"secret-construction-failure" not in response.get_body()
    assert len(lock.release_calls) == 1
