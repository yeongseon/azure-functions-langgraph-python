"""Azure OpenAI LangGraph agent deployed as Azure Functions.

This is the **primary real-world example**: a minimal but genuinely useful
LangGraph agent backed by a real Azure OpenAI chat deployment, exposed over
HTTP by ``LangGraphApp`` with zero adapter-specific abstractions.

The package stays a *deployment adapter* — everything below is a normal
LangGraph ``StateGraph``. ``function_app.py`` only calls ``register()``.

Authentication (resolved at model-construction time):

1. **API key** — set ``AZURE_OPENAI_API_KEY`` (easiest local onboarding).
2. **Managed Identity / Entra ID** — leave the key unset and set
   ``AZURE_OPENAI_USE_ENTRA_ID=true``; the model authenticates with
   ``DefaultAzureCredential`` (Function App Managed Identity in Azure,
   ``az login`` locally). No secret lands in App Settings.

Both paths require ``AZURE_OPENAI_ENDPOINT`` and ``AZURE_OPENAI_DEPLOYMENT``.

When Azure OpenAI is **not** fully configured (no endpoint, no deployment, or no
auth method), the graph falls back to a deterministic in-process fake model so
the example imports, compiles, and can be smoke-tested in CI without any cloud
credentials. The fake path never imports ``langchain_openai``.

Requirements::

    pip install azure-functions-langgraph langgraph langchain-core langchain-openai azure-identity

Usage::

    # In your function_app.py
    from graph import compiled_graph
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from model_config import ModelMode, load_azure_openai_config, load_model_mode
from typing import TypedDict

MODEL_MODE = load_model_mode()

SYSTEM_PROMPT = (
    "You are a concise Azure cloud assistant. Answer questions about Azure, "
    "serverless architecture, and LangGraph clearly and briefly. If you are "
    "unsure, say so rather than guessing."
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
            # Managed Identity / Entra ID path. DefaultAzureCredential picks up
            # the Function App's Managed Identity in Azure and `az login`
            # locally — the same code path works in both environments.
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
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import AIMessage

    logging.getLogger(__name__).warning(
        "example agent is using the deterministic fake model",
        extra={"model_mode": ModelMode.FAKE.value},
    )
    return FakeListChatModel(responses=["[fake model] deterministic offline response"])


# ------------------------------------------------------------------
# 1. Define state (LangGraph's standard message-state pattern)
# ------------------------------------------------------------------
try:
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


# ------------------------------------------------------------------
# 2. Define node functions
# ------------------------------------------------------------------
def chat(state: AgentState) -> dict[str, Any]:
    """Single agent node: prepend the system prompt and call the model."""
    from langchain_core.messages import SystemMessage

    response = _model.invoke([SystemMessage(content=SYSTEM_PROMPT), *state["messages"]])
    return {"messages": [response]}


# ------------------------------------------------------------------
# 3. Build the graph (normal LangGraph API — no adapter abstractions)
# ------------------------------------------------------------------
builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)

compiled_graph = builder.compile()
