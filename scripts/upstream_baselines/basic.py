"""Independent MIT baseline: langchain-ai/docs@42bc913ad89c7f288667184ae9fc485d9fa28023.

Source paths and attribution: examples/official_langgraph_basic/README.md.
Only import placement differs from the original checked-in baseline string.
"""

from langchain.messages import AIMessage, AnyMessage
from langgraph.graph import StateGraph
from typing_extensions import TypedDict


class State(TypedDict):
    messages: list[AnyMessage]
    extra_field: int


def node(state: State):
    messages = state["messages"]
    new_message = AIMessage("Hello!")
    return {"messages": messages + [new_message], "extra_field": 10}


builder = StateGraph(State)
builder.add_node(node)
builder.set_entry_point("node")
graph = builder.compile()
