"""Defer stream ownership until ASGI execution or explicit body consumption."""

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing

from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send


class DeferredStreamingResponse(StreamingResponse):
    def __init__(self, prepare: Callable[[], Awaitable[Response]], request_id: str) -> None:
        self._prepare = prepare
        super().__init__(
            self._body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-ID": request_id,
            },
        )

    async def _body(self) -> AsyncIterator[str | bytes | memoryview]:
        response = await self._prepare()
        if isinstance(response, StreamingResponse):
            iterator = response.body_iterator
            if not isinstance(iterator, AsyncGenerator):
                raise TypeError("prepared stream must own an async generator")
            async with aclosing(iterator):
                async for chunk in iterator:
                    yield chunk
        else:
            yield response.body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = await self._prepare()
        if isinstance(response, StreamingResponse):
            # The prepared iterator has entered its cleanup scope before headers are sent.
            iterator = response.body_iterator
            if not isinstance(iterator, AsyncGenerator):
                raise TypeError("prepared stream must own an async generator")
            async with aclosing(iterator):
                await response(scope, receive, send)
        else:
            await response(scope, receive, send)
