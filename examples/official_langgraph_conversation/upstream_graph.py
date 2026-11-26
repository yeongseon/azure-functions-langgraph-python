"""Extracted sync graph from LangChain's MIT-licensed add-memory guide.

Source: https://github.com/langchain-ai/docs/blob/15c95b5f33287dff462acb53e1f34aaed69d5c21/src/oss/langgraph/add-memory.mdx
Section: Example: using Postgres checkpointer / Sync.
Only model construction and checkpoint context/compile move to separate wiring.
The node body, MessagesState and builder statements below are unchanged.
License: UPSTREAM_LICENSE. Model substitution: model.py. Persistence: graph.py.
"""

from langgraph.graph import START, MessagesState, StateGraph
from model import model


def call_model(state: MessagesState):
    response = model.invoke(state["messages"])
    return {"messages": response}


builder = StateGraph(MessagesState)
builder.add_node(call_model)
builder.add_edge(START, "call_model")
