# Azure Functions LangGraph

无需编写请求包装代码，将现有 [LangGraph](https://github.com/langchain-ai/langgraph) 图部署为 Azure Functions Python v2 HTTP 端点的适配器。
不是用于创建提示词、模型或工具的代理框架。

[English](README.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> 此翻译由社区维护，可能落后于最新的 [English README](README.md)。英文版是权威来源。

## 安装

支持 Python 3.11–3.14。基于托管计划与运行时的支持情况请参见[部署指南](docs/deployment.md)。

```bash
pip install azure-functions-langgraph
```

## 快速开始

在包含 `host.json` 的 Functions 应用中保存为 `function_app.py`：

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

运行 `func start`，向 `http://localhost:7071/api/graphs/echo/invoke` 发送 POST 请求 `{"input":{"message":"hello"}}`，将返回结果 `{"output":{"message":"hello"}}`。
部署后的路由默认需要函数密钥。完整项目文件、本地配置与部署步骤请参见[五分钟教程](docs/tutorial-5-min.md)。
如需从全新 Functions 项目开始搭建，请参见[从新 Functions 项目开始](docs/getting-started.md#start-from-a-new-functions-project)。

关于上游来源及独立记录的 Azure 部署验证，请参见[官方 LangGraph 示例](examples/official_langgraph_basic/)。

## 稳定性

采用滚动 0.x，包级分类为 Beta。各 API 层面（Stable、Beta、Experimental）的详细契约请参见 [English README 稳定性表格](README.md#stability--rolling-0x-per-surface)。
默认流式传输为缓冲式 SSE，并非逐 token 实时传输。
函数密钥不代表用户所有权验证，默认锁和线程存储仅存在于进程内部。
详情请参见[生产与分布式指南](docs/production-guide.md#distributed-deployment)及[安全边界](docs/security.md)。

## 学习路径

1. [Azure OpenAI](examples/azure_openai_agent/)
2. [对话记忆](examples/conversation_memory/)
3. [工具调用](examples/tool_calling_agent/)
4. [持久化存储](examples/production_persistent_agent/)

各示例均为展示特定功能的独立应用，可在无云凭证的情况下使用 fake 模型进行测试。
[详细参考与迁移](docs/adapter-reference.md)、[SDK 支持范围](COMPATIBILITY.md)、[贡献指南](CONTRIBUTING.md)。
此项目为独立社区项目，与 Microsoft 或 LangChain 无关联。[MIT](LICENSE)。
