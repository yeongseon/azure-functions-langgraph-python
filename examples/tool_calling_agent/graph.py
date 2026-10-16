"""Azure OpenAI tool-calling agent deployed as Azure Functions.

Builds on the [`azure_openai_agent`](../azure_openai_agent/) (base model setup)
and [`conversation_memory`](../conversation_memory/) (state) examples by adding a
**tool-calling loop**:

    HTTP request
      -> agent node (Azure OpenAI decides whether to call a tool)
      -> tool node (ToolNode executes the chosen tool from tools.py)
      -> result returns to the agent
      -> final answer
      -> Azure Functions HTTP response

This stays **normal LangGraph code**. ``azure-functions-langgraph`` only exposes
the compiled graph over HTTP — it does not own tools, models, or the agent loop.
The tools live in ``tools.py`` (your business logic); swap them for real APIs.

Requirements::

    pip install azure-functions-langgraph langgraph langchain-core langchain-openai azure-identity

When Azure OpenAI is **not** fully configured (endpoint, deployment, AND an auth
method), the graph falls back to a deterministic scripted model that emits one
known tool call followed by a final answer, so the whole agent->tool->agent loop
is importable and smoke-testable in CI without any cloud credentials. The fake
path never imports ``langchain_openai``.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Annotated, Any

from model_config import ModelMode, load_azure_openai_config, load_model_mode
from tools import TOOLS
from typing import TypedDict

MODEL_MODE = load_model_mode()

SYSTEM_PROMPT = (
    "You are a concise Azure cloud assistant that can call tools. Use the "
    "provided tools when a question needs order or weather data; otherwise "
    "answer directly. Keep answers brief."
)


@dataclass(frozen=True, slots=True)
class FakeToolCall:
    name: str
    args: dict[str, str]


FAKE_TOOL_CALL = FakeToolCall(name="lookup_order", args={"order_id": "A1001"})


def build_chat_model() -> Any:
    """Construct the tool-bound chat model for the agent.

    Returns a real ``AzureChatOpenAI`` bound to ``TOOLS`` when Azure OpenAI is
    configured, else a deterministic scripted fake model for credential-free
    local runs and CI.
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
            model: Any = AzureChatOpenAI(
                azure_endpoint=config.endpoint,
                azure_deployment=config.deployment,
                api_version=config.api_version,
                azure_ad_token_provider=token_provider,
            )
        else:
            model = AzureChatOpenAI(
                azure_endpoint=config.endpoint,
                azure_deployment=config.deployment,
                api_version=config.api_version,
                api_key=config.api_key,
            )
        return model.bind_tools(TOOLS)

    from langchain_core.messages import AIMessage

    available_names = {tool.name for tool in TOOLS}
    if FAKE_TOOL_CALL.name not in available_names:
        message = (
            f"Fake tool script names {FAKE_TOOL_CALL.name!r}, but TOOLS contains "
            f"{sorted(available_names)!r}; update FAKE_TOOL_CALL when replacing tools"
        )
        raise RuntimeError(message)

    class StateAwareToolModel:
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

        def invoke(self, messages: list[Any]) -> AIMessage:
            if getattr(messages[-1], "type", None) == "tool":
                return AIMessage(
                    content=f"[fake model] deterministic result for {FAKE_TOOL_CALL.args!r}"
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": FAKE_TOOL_CALL.name,
                        "args": FAKE_TOOL_CALL.args,
                        "id": "call_demo_1",
                        "type": "tool_call",
                    }
                ],
            )

    logging.getLogger(__name__).warning(
        "example agent is using the deterministic fake model",
        extra={"model_mode": ModelMode.FAKE.value},
    )
    return StateAwareToolModel().bind_tools(TOOLS)


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

# name -> tool, so the tool node can dispatch whichever tool the model chose.
_TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def agent(state: AgentState) -> dict[str, Any]:
    """Agent node: prepend the system prompt and let the model decide.

    The model may return a plain answer or an ``AIMessage`` with ``tool_calls``;
    ``route_after_agent`` sends the latter to the tool node.
    """
    from langchain_core.messages import SystemMessage

    response = _model.invoke([SystemMessage(content=SYSTEM_PROMPT), *state["messages"]])
    return {"messages": [response]}


def tools(state: AgentState) -> dict[str, Any]:
    """Tool node: run every tool the agent requested, append the results.

    This is a hand-rolled equivalent of ``langgraph.prebuilt.ToolNode`` — it is
    intentionally spelled out so you can see exactly how a tool call becomes a
    ``ToolMessage``, and so the example works across the whole supported
    ``langgraph>=1.0`` range without depending on the prebuilt package. On newer
    langgraph you can replace this whole function with ``ToolNode(TOOLS)``.
    """
    from langchain_core.messages import ToolMessage

    last = state["messages"][-1]
    outputs: list[Any] = []
    for call in last.tool_calls:
        tool = _TOOLS_BY_NAME.get(call["name"])
        if tool is None:
            message = (
                f"Model requested unknown tool {call['name']!r}; available TOOLS: "
                f"{sorted(_TOOLS_BY_NAME)!r}"
            )
            raise RuntimeError(message)
        result = tool.invoke(call["args"])
        outputs.append(ToolMessage(content=str(result), name=call["name"], tool_call_id=call["id"]))
    return {"messages": outputs}


def route_after_agent(state: AgentState) -> str:
    """If the last AIMessage requested tools -> \"tools\", else end the run."""
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return END


builder = StateGraph(AgentState)
builder.add_node("agent", agent)
builder.add_node("tools", tools)
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", route_after_agent, {"tools": "tools", END: END})
# After the tool runs, hand the result back to the agent to produce the answer.
builder.add_edge("tools", "agent")

compiled_graph = builder.compile()
