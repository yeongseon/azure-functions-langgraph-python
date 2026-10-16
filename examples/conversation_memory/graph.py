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

import logging
from typing import Annotated, Any

from model_config import ModelMode, load_azure_openai_config, load_model_mode
from typing import TypedDict

MODEL_MODE = load_model_mode()

SYSTEM_PROMPT = (
    "You are a concise Azure cloud assistant with memory of the current "
    "conversation. Use the prior messages in this thread to stay consistent. "
    "If you are unsure, say so rather than guessing."
)

def build_chat_model() -> Any:
    """Construct the chat model for the agent.

    Returns a real ``AzureChatOpenAI`` when Azure OpenAI is configured, else a
    deterministic fake model for credential-free local runs and CI.
    """
    config = load_azure_openai_config(MODEL_MODE)
    if config is not None:
        # Import lazily so the fake path never requires langchain-openai.
        from langchain_openai import AzureChatOpenAI

        if config.use_entra_id and not config.api_key:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(),
                "https://cognitiveservices.azure.com/.default",
            )
            return AzureChatOpenAI(
                azure_endpoint=config.endpoint,
                azure_deployment=config.deployment,
                api_version=config.api_version,
                azure_ad_token_provider=token_provider,
            )

        # API-key path.
        return AzureChatOpenAI(
            azure_endpoint=config.endpoint,
            azure_deployment=config.deployment,
            api_version=config.api_version,
            api_key=config.api_key,
        )

    # Deterministic fallback — no cloud, no secrets, no langchain-openai.
    #
    # The fake echoes the latest human turn so the same-thread demo and CI tests
    # can observe that history accumulated across invocations. A real model would
    # instead *reason over* the thread history the checkpointer replays.
    from langchain_core.messages import AIMessage

    class StateAwareFakeModel:
        def invoke(self, messages: list[Any]) -> AIMessage:
            human_turns = sum(getattr(message, "type", None) == "human" for message in messages)
            return AIMessage(content=f"[fake model] observed {human_turns} human turn(s)")

    logging.getLogger(__name__).warning(
        "example agent is using the deterministic fake model",
        extra={"model_mode": ModelMode.FAKE.value},
    )
    return StateAwareFakeModel()


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
