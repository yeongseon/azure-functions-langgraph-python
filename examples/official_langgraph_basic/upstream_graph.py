"""Graph copied from the official LangGraph "Define and update state" guide.

Upstream page : https://docs.langchain.com/oss/python/langgraph/graph-api-state
                (retrieved 2026-11-05)
Upstream repo : https://github.com/langchain-ai/docs (MIT License,
                Copyright (c) 2025 LangChain)
Pinned source : commit 42bc913ad89c7f288667184ae9fc485d9fa28023
                - src/oss/langgraph/graph-api-state.mdx
                - src/snippets/code-samples/langgraph-graph-api-state-define-py.mdx
                - src/snippets/code-samples/langgraph-graph-api-state-update-node-py.mdx
                - src/snippets/code-samples/langgraph-graph-api-state-build-graph-py.mdx

Extraction: the four Python snippets of the page's "Define state" and "Update
state" sections are concatenated verbatim, in page order. The page's final
``graph.invoke(...)`` snippet is *not* part of this module: invoking is the
host's job (see ``function_app.py``). The nodes, edges and state schema below
are NOT modified.

MIT License

Copyright (c) 2025 LangChain

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

# --- upstream: langgraph-graph-api-state-define-py ---------------------------
from langchain.messages import AnyMessage
from typing_extensions import TypedDict


class State(TypedDict):
    messages: list[AnyMessage]
    extra_field: int


# --- upstream: langgraph-graph-api-state-update-node-py ----------------------
from langchain.messages import AIMessage


def node(state: State):
    messages = state["messages"]
    new_message = AIMessage("Hello!")
    return {"messages": messages + [new_message], "extra_field": 10}


# --- upstream: langgraph-graph-api-state-build-graph-py ----------------------
from langgraph.graph import StateGraph

builder = StateGraph(State)
builder.add_node(node)
builder.set_entry_point("node")
graph = builder.compile()
