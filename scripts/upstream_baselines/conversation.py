"""Independent MIT baseline: langchain-ai/docs@15c95b5f33287dff462acb53e1f34aaed69d5c21.

Source: src/oss/langgraph/add-memory.mdx, sync Postgres example.
License: examples/official_langgraph_conversation/UPSTREAM_LICENSE.
Model construction is the same explicit fake substitution used before extraction.
"""

from langgraph.graph import START, MessagesState, StateGraph
from model import model


def call_model(state: MessagesState):
    response = model.invoke(state["messages"])
    return {"messages": response}


builder = StateGraph(MessagesState)
builder.add_node(call_model)
builder.add_edge(START, "call_model")
