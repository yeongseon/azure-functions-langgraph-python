from typing import Any

import azure.functions as func
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.authorization import AuthorizationContext, UnauthenticatedError


def answer(_state: MessagesState) -> dict[str, Any]:
    return {"messages": [AIMessage(content="host smoke passed")]}


builder = StateGraph(MessagesState)
builder.add_node("answer", RunnableLambda(answer))
builder.add_edge(START, "answer")
builder.add_edge("answer", END)


def authorize(context: AuthorizationContext) -> bool:
    raise UnauthenticatedError("host-credential-canary")


# Local-only fixture: Core Tools cannot prove deployed function-key enforcement.
langgraph_app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION, authorize=authorize)
langgraph_app.register(
    graph=builder.compile(checkpointer=MemorySaver()), name="smoke", stream=False
)
app = langgraph_app.function_app
