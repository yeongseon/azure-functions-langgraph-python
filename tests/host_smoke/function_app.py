from typing import Any

import azure.functions as func
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from azure_functions_langgraph import LangGraphApp


def answer(_state: MessagesState) -> dict[str, Any]:
    return {"messages": [AIMessage(content="host smoke passed")]}


builder = StateGraph(MessagesState)
builder.add_node("answer", answer)
builder.add_edge(START, "answer")
builder.add_edge("answer", END)

langgraph_app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
langgraph_app.register(graph=builder.compile(), name="smoke", stream=False)
app = langgraph_app.function_app
