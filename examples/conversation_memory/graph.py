"""Stateful conversation agent — checkpointer-backed LangGraph on Azure Functions.

This is the **stateful delta** on top of the stateless
[`azure_openai_agent`](../azure_openai_agent/) example. The graph shape is the
same real-Azure-OpenAI chat agent (with a deterministic fake-model fallback for
credential-free CI), with **one** change: the compiled graph is given a
**checkpointer**, so passing the same ``thread_id`` across HTTP requests keeps
the conversation going.

Terminology (kept distinct on purpose):

* **thread_id** — LangGraph execution/conversation identity, supplied per request
  under ``config.configurable.thread_id``. Same id → same conversation.
* **checkpointer** — persists a thread's graph execution state between invokes.
  This example uses the in-memory ``InMemorySaver`` so onboarding needs zero
  external services; state lives only for the life of the Functions **host
  process** (see the README's restart note).
* **Platform ``ThreadStore``** — metadata registry for the optional
  ``platform_compat`` layer; **not** required for native conversation memory and
  not used here.
* **LangGraph ``BaseStore``** — long-term, cross-thread memory; **out of scope**.

Native teaching path::

    POST /api/graphs/conversation_agent/invoke        # same thread_id → continuity
    GET  /api/graphs/conversation_agent/threads/{thread_id}/state

Requirements::

    pip install azure-functions-langgraph langgraph langchain-core langchain-openai azure-identity
"""

from __future__ import annotations

import itertools
import os
from typing import Annotated, Any

from typing_extensions import TypedDict

_TRUTHY = {"1", "true", "yes", "on"}

SYSTEM_PROMPT = (
    "You are a concise Azure cloud assistant with memory of the current "
    "conversation. Use the prior messages in this thread to stay consistent. "
    "If you are unsure, say so rather than guessing."
)

# Azure OpenAI configuration (read at import / cold start).
_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY")
_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
_USE_ENTRA_ID = os.environ.get("AZURE_OPENAI_USE_ENTRA_ID", "").strip().lower() in _TRUTHY


def _azure_configured() -> bool:
    """True when enough config is present to build a real Azure OpenAI model."""
    return bool(_ENDPOINT and _DEPLOYMENT and (_API_KEY or _USE_ENTRA_ID))


def build_chat_model() -> Any:
    """Construct the chat model for the agent.

    Returns a real ``AzureChatOpenAI`` when Azure OpenAI is configured, else a
    deterministic fake model for credential-free local runs and CI.
    """
    if _azure_configured():
        # Import lazily so the fake path never requires langchain-openai.
        from langchain_openai import AzureChatOpenAI

        if _USE_ENTRA_ID and not _API_KEY:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(),
                "https://cognitiveservices.azure.com/.default",
            )
            return AzureChatOpenAI(
                azure_endpoint=_ENDPOINT,
                azure_deployment=_DEPLOYMENT,
                api_version=_API_VERSION,
                azure_ad_token_provider=token_provider,
            )

        # API-key path.
        return AzureChatOpenAI(
            azure_endpoint=_ENDPOINT,
            azure_deployment=_DEPLOYMENT,
            api_version=_API_VERSION,
            api_key=_API_KEY,
        )

    # Deterministic fallback — no cloud, no secrets, no langchain-openai.
    #
    # The fake echoes the latest human turn so the same-thread demo and CI tests
    # can observe that history accumulated across invocations. A real model would
    # instead *reason over* the thread history the checkpointer replays.
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    def _replies() -> Any:
        counter = itertools.count(1)
        while True:
            yield AIMessage(
                content=(
                    f"[fake model] turn {next(counter)} — Azure OpenAI is not configured. "
                    "Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, and "
                    "AZURE_OPENAI_API_KEY (or AZURE_OPENAI_USE_ENTRA_ID=true) to use a "
                    "real deployment."
                )
            )

    return GenericFakeChatModel(messages=_replies())


try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
except ImportError as exc:  # pragma: no cover - defensive import guard
    raise ImportError(
        "langgraph and langchain-core are required for this example. Install with: "
        "pip install azure-functions-langgraph langgraph langchain-core langchain-openai azure-identity"
    ) from exc


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


# Built once at cold start; reused across invocations. Azure/langchain-openai
# import errors (if that path is configured) propagate with their own message.
_model = build_chat_model()


def chat(state: AgentState) -> dict[str, Any]:
    """Single agent node: prepend the system prompt and call the model.

    ``state["messages"]`` already includes every prior turn the checkpointer
    replayed for this ``thread_id`` — that history is the conversation memory.
    """
    from langchain_core.messages import SystemMessage

    response = _model.invoke([SystemMessage(content=SYSTEM_PROMPT), *state["messages"]])
    return {"messages": [response]}


builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)

# The checkpointer is the whole point of this example: it persists per-thread
# state so repeated invokes with the same thread_id continue one conversation.
# InMemorySaver keeps onboarding dependency-free; swap it for a durable backend
# (see examples/persistent_agent_blob_table/) for restart/scale-safe storage.
compiled_graph = builder.compile(checkpointer=InMemorySaver())
