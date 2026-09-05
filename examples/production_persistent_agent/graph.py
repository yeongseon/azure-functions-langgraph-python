"""Production persistent Azure OpenAI agent — the capstone example.

This example is the fourth step of the adoption learning path and composes the
three preceding examples into one production-oriented deployment:

1. [`azure_openai_agent`](../azure_openai_agent/) — a **real** Azure OpenAI chat
   agent (API-key or Managed Identity / Entra ID auth).
2. [`conversation_memory`](../conversation_memory/) — graph **state** across
   requests via a checkpointer keyed by ``thread_id``.
3. [`managed_identity_storage`](../managed_identity_storage/) — that state made
   **durable and Azure-native** on ``AzureBlobCheckpointSaver``, wired with
   ``DefaultAzureCredential`` (Managed Identity) in production and a
   connection-string fallback for local [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite).

``graph.py`` stays **normal LangGraph code**: a message-state ``StateGraph`` with
a single Azure OpenAI chat node. It is intentionally checkpointer-agnostic — the
checkpointer is attached in ``function_app.py`` so the same graph definition can
be compiled with or without persistence (and smoke-tested without any storage).

Authentication (resolved at model-construction time):

1. **API key** — set ``AZURE_OPENAI_API_KEY`` (easiest local onboarding).
2. **Managed Identity / Entra ID** — leave the key unset and set
   ``AZURE_OPENAI_USE_ENTRA_ID=true``; the model authenticates with
   ``DefaultAzureCredential`` (Function App Managed Identity in Azure,
   ``az login`` locally). No secret lands in App Settings.

Both real paths require ``AZURE_OPENAI_ENDPOINT`` and ``AZURE_OPENAI_DEPLOYMENT``.
When Azure OpenAI is **not** fully configured, the graph falls back to a
deterministic in-process fake model so the example imports, compiles, and can be
smoke-tested in CI without any cloud credentials. The fake path never imports
``langchain_openai``.

Requirements::

    pip install "azure-functions-langgraph[azure-blob,azure-identity]" \\
        langgraph langchain-core langchain-openai azure-identity
"""

from __future__ import annotations

import itertools
import os
from typing import Annotated, Any

from typing_extensions import TypedDict

_TRUTHY = {"1", "true", "yes", "on"}

SYSTEM_PROMPT = (
    "You are a concise Azure cloud assistant with memory of this conversation. "
    "Answer questions about Azure, serverless architecture, and LangGraph "
    "clearly and briefly, using earlier turns for context. If you are unsure, "
    "say so rather than guessing."
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


def chat(state: AgentState) -> dict[str, Any]:
    """Single agent node: prepend the system prompt and call the model.

    With a checkpointer attached (see ``function_app.py``), ``state["messages"]``
    already contains the prior turns for this ``thread_id``, so the model sees
    the whole conversation — that is what makes the persisted memory useful.
    """
    from langchain_core.messages import SystemMessage

    response = _model.invoke([SystemMessage(content=SYSTEM_PROMPT), *state["messages"]])
    return {"messages": [response]}


def build_graph() -> StateGraph:
    """Return the uncompiled graph builder.

    Factory (not a module-level compiled graph) so callers choose the
    checkpointer: ``function_app.py`` compiles with ``AzureBlobCheckpointSaver``
    for durable state, while the smoke test compiles without one.
    """
    builder = StateGraph(AgentState)
    builder.add_node("chat", chat)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder


# Checkpointer-less compile: importable and smoke-testable with no storage.
# function_app.py recompiles via build_graph().compile(checkpointer=...).
compiled_graph = build_graph().compile()
