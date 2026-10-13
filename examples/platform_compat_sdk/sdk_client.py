"""Drive the example with the official langgraph-sdk client.

Run alongside ``func start`` in this directory:

    pip install langgraph-sdk
    python sdk_client.py
"""

from __future__ import annotations

import anyio
from langgraph_sdk import get_client


async def main() -> None:
    client = get_client(url="http://localhost:7071/api")

    assistants = await client.assistants.search()
    print("assistants:", [a["assistant_id"] for a in assistants])

    thread = await client.threads.create()
    print("thread:", thread["thread_id"])

    result = await client.runs.wait(
        thread["thread_id"],
        assistant_id="echo_agent",
        input={"messages": [{"role": "human", "content": "Hello, SDK!"}]},
    )
    print("result:", result)

    state = await client.threads.get_state(thread["thread_id"])
    print("state:", state)


if __name__ == "__main__":
    anyio.run(main)
