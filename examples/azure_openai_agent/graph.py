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

When Azure OpenAI is **not** configured (no endpoint), the graph falls back to
a deterministic in-process fake model so the example imports, compiles, and can
be smoke-tested in CI without any cloud credentials. The fake path never
imports ``langchain_openai``.

Requirements::

    pip install azure-functions-langgraph langgraph langchain-core langchain-openai azure-identity

Usage::

    # In your function_app.py
    from graph import compiled_graph
"""

from __future__ import annotations

import itertools
import os
from typing import Annotated, Any

from typing_extensions import TypedDict

_TRUTHY = {"1", "true", "yes", "on"}

SYSTEM_PROMPT = (
    "You are a concise Azure cloud assistant. Answer questions about Azure, "
    "serverless architecture, and LangGraph clearly and briefly. If you are "
    "unsure, say so rather than guessing."
)

# Azure OpenAI configuration (read at import / cold start).
_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY")
# langchain-openai / openai default to reading OPENAI_API_VERSION; we surface an
# explicit, currently-supported default so the example is not tied to whatever
# happens to be in the environment.
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
            # Managed Identity / Entra ID path. DefaultAzureCredential picks up
            # the Function App's Managed Identity in Azure and `az login`
            # locally — the same code path works in both environments.
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
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    canned = AIMessage(
        content=(
            "[fake model] Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT, "
            "AZURE_OPENAI_DEPLOYMENT, and AZURE_OPENAI_API_KEY (or "
            "AZURE_OPENAI_USE_ENTRA_ID=true) to use a real deployment."
        )
    )
    # itertools.cycle → the fake never exhausts across repeated invocations.
    return GenericFakeChatModel(messages=itertools.cycle([canned]))


# ------------------------------------------------------------------
# 1. Define state (LangGraph's standard message-state pattern)
# ------------------------------------------------------------------
try:
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages

    class AgentState(TypedDict):
        messages: Annotated[list, add_messages]

    # Built once at cold start; reused across invocations.
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

except ImportError as exc:  # pragma: no cover - defensive import guard
    raise ImportError(
        "langgraph and langchain-core are required for this example. Install with: "
        "pip install azure-functions-langgraph langgraph langchain-core langchain-openai azure-identity"
    ) from exc
