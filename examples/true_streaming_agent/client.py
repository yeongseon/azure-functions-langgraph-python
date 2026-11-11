"""Local-only SSE client: HTTP chunks are not event boundaries."""

from __future__ import annotations

import argparse
import codecs
from collections.abc import Iterable, Iterator
from contextlib import closing
from http.client import HTTPConnection
import json
import re


class StreamError(ValueError):
    """Malformed data, server error, or incomplete connection."""


def events(chunks: Iterable[bytes]) -> Iterator[tuple[str, object]]:
    """Parse this adapter's JSON SSE events, bounded to 1 MiB per frame.

    A missing end frame is failure, not evidence that the run was cancelled.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    buffer = ""
    event = "message"
    data: list[str] = []
    frame_size = 0
    skip_lf = False
    for chunk in chunks:
        text = decoder.decode(chunk)
        if skip_lf and text:
            text = text.removeprefix("\n")
            skip_lf = False
        buffer += text
        while match := re.search(r"\r\n|\r|\n", buffer):
            skip_lf = match.group() == "\r" and match.end() == len(buffer)
            line, buffer = buffer[: match.start()], buffer[match.end() :]
            # Reserve both CRLF bytes for CR, allowing immediate CR dispatch.
            terminator_size = 2 if "\r" in match.group() else 1
            frame_size += len(line.encode("utf-8")) + terminator_size
            if frame_size > 1024 * 1024:
                raise StreamError("SSE frame exceeds 1 MiB")
            if not line:
                if data:
                    try:
                        payload = json.loads("\n".join(data))
                    except ValueError as exc:
                        raise StreamError("Invalid JSON event") from exc
                    if event == "error":
                        raise StreamError("Server emitted an error event")
                    if event == "end":
                        return
                    yield event, payload
                event, data, frame_size = "message", [], 0
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "event":
                    event = value
                elif field == "data":
                    data.append(value)
        if len(buffer.encode("utf-8")) + frame_size > 1024 * 1024:
            raise StreamError("SSE frame exceeds 1 MiB")
    decoder.decode(b"", final=True)
    raise StreamError("Connection closed before end event")


def main() -> None:
    """Call a local Core Tools host without keys or cloud credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop-after", type=int, default=0, help="close after N data events")
    args = parser.parse_args()
    body = {
        "input": {"messages": [{"role": "human", "content": "hello world"}], "tokens": []},
        "config": {"configurable": {"thread_id": "local-sse-client"}},
        "stream_mode": "updates",
    }
    try:
        with closing(HTTPConnection("localhost", 7071, timeout=30)) as connection:
            connection.request(
                "POST",
                "/api/graphs/true_streaming_agent/stream",
                body=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise StreamError(f"HTTP {response.status}")
            if (
                response.getheader("Content-Type", "").split(";", 1)[0].strip()
                != "text/event-stream"
            ):
                raise StreamError("Expected text/event-stream")
            for count, (event, payload) in enumerate(
                events(iter(lambda: response.read1(4096), b"")), 1
            ):
                print(event, json.dumps(payload, ensure_ascii=False), flush=True)
                if args.stop_after > 0 and count >= args.stop_after:
                    print("Closing client; this does not confirm server-side cancellation.")
                    break
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Stream failed: {exc}\n")


if __name__ == "__main__":
    main()
