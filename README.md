# Azure Functions LangGraph

Deploy existing [LangGraph](https://github.com/langchain-ai/langgraph) graphs as
Azure Functions Python v2 HTTP endpoints without writing request wrappers.
This is a deployment adapter, not an agent builder or a replacement for LangGraph.

[![PyPI](https://img.shields.io/pypi/v/azure-functions-langgraph.svg)](https://pypi.org/project/azure-functions-langgraph/)
[![CI](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml)
[![Security](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml)
[![Docs](https://img.shields.io/badge/docs-yeongseon.dev-blue)](https://yeongseon.dev/azure-functions-python/langgraph/)

[한국어](README.ko.md) · [日本語](README.ja.md) · [简体中文](README.zh-CN.md)

## Installation

Python 3.11–3.14 is supported. Azure Functions hosting support depends on the
selected plan/runtime; see [deployment](docs/deployment.md).

```bash
pip install azure-functions-langgraph
```

## Quick Start

Save as `function_app.py` in a Functions app with `host.json`:

```python
from typing import TypedDict
from langgraph.graph import START, END, StateGraph
from azure_functions_langgraph import LangGraphApp
class State(TypedDict):
    message: str
builder = StateGraph(State)
builder.add_node("echo", lambda state: {"message": state["message"]})
builder.add_edge(START, "echo")
builder.add_edge("echo", END)
app = LangGraphApp()
app.register(builder.compile(), "echo")
func_app = app.function_app
```

Run `func start`, then POST `{"input":{"message":"hello"}}` to
`http://localhost:7071/api/graphs/echo/invoke`; the result is
`{"output":{"message":"hello"}}`. Deployed routes require a function key by
default. Follow the [5-minute tutorial](docs/tutorial-5-min.md) for complete
project files, local setup and deployment.

## Stability — rolling 0.x, per surface

The package-level classifier is **Beta**, not a promise that every surface is
production-ready. Compatibility decisions follow [CONTRIBUTING.md](CONTRIBUTING.md#breaking-change-classification);
breaking changes remain on the 0.x line under the existing release policy.

| Surface | Tier | Contract |
| --- | --- | --- |
| `LangGraphApp` construction, native invoke/stream/state HTTP endpoints, auth levels | Stable | Preserve supported calls; incompatible changes require explicit classification and migration |
| Checkpoint backends, thread locks, async runtime, `version="v2"`, `RunObserver` | Beta | Shape settled; refinements remain possible before v1.0 |
| Platform compatibility, true HTTP streaming, Service Bus and Durable lifecycle | Experimental | Opt-in; may change between minor releases |

Classic stream routes return **buffered SSE**, not incremental tokens.
`StreamingLangGraphApp` is a separate experimental transport. Function keys
are not user/thread ownership; applications must enforce authorization.
Default locks and Platform thread records are process-local. Shared writers
need explicit distributed storage/locking; see the [production guide](docs/production-guide.md#distributed-deployment)
and [security boundaries](docs/security.md).

## Recommended learning path

These are independent, capability-focused apps, not cumulative production templates:

1. [Azure OpenAI agent](examples/azure_openai_agent/)
2. [Conversation memory](examples/conversation_memory/)
3. [Tool calling](examples/tool_calling_agent/)
4. [Persistent production wiring](examples/production_persistent_agent/)

Examples support deterministic fake models without cloud credentials. For
Platform thread storage, the tested [Managed Identity storage recipe](examples/managed_identity_storage/)
wires Azure Table explicitly. State separation is not authenticated ownership.

## Documentation

- [Configuration](docs/configuration.md) and [usage](docs/usage.md)
- [Adapter reference, optional features and migrations](docs/adapter-reference.md)
- [Architecture](docs/architecture.md) and [distributed deployment](docs/production-guide.md#distributed-deployment)
- [Finite SDK support matrix](COMPATIBILITY.md): partial compatibility, not Platform parity
- [Examples](examples/README.md), [troubleshooting](docs/troubleshooting.md), [security](SECURITY.md)
- [Contributing](CONTRIBUTING.md), [product scope](PRD.md), [design](DESIGN.md)
- AI tooling: [llms.txt](llms.txt), [llms-full.txt](llms-full.txt), [AGENTS.md](AGENTS.md)

Part of the Azure Functions Python DX Toolkit. Integrated application recipes
live in the [cookbook](https://github.com/yeongseon/azure-functions-cookbook-python);
this package owns serving compiled graphs, not prompts, models or tool frameworks.

Independent community project; not affiliated with Microsoft or LangChain.
Azure, Azure Functions and LangGraph are their owners' trademarks. [MIT](LICENSE).
