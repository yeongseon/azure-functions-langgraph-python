"""Offline observer proof graph: one deterministic node that can succeed or fail.

No LLM, no network, no credentials. The ``echo`` node copies the user message
into its reply, and raises when the message asks for a failure. The raised
error deliberately embeds the user message so ``proof.py`` can show that the
observer records only the exception *class name*, never its message.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class ChatState(TypedDict, total=False):
    user_text: str
    reply: str


FAIL_MARKER = "!fail"


def echo(state: ChatState) -> dict[str, Any]:
    text = state.get("user_text", "")
    if text.endswith(FAIL_MARKER):
        raise RuntimeError(f"echo node failed for message: {text}")
    return {"reply": f"echo: {text}"}


builder = StateGraph(ChatState)
builder.add_node("echo", echo)
builder.add_edge(START, "echo")
builder.add_edge("echo", END)

compiled_graph = builder.compile()
