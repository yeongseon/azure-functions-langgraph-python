# Azure Functions LangGraph

[![PyPI](https://img.shields.io/pypi/v/azure-functions-langgraph.svg)](https://pypi.org/project/azure-functions-langgraph/)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/azure-functions-langgraph/)
[![CI](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/ci-test.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/ci-test.yml)
[![Release](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/publish-pypi.yml)
[![Security Scans](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/security.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph/actions/workflows/security.yml)
[![codecov](https://codecov.io/gh/yeongseon/azure-functions-langgraph/branch/main/graph/badge.svg)](https://codecov.io/gh/yeongseon/azure-functions-langgraph)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://pre-commit.com/)
[![Docs](https://img.shields.io/badge/docs-gh--pages-blue)](https://yeongseon.github.io/azure-functions-langgraph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

本文档语言: [한국어](README.ko.md) | [日本語](README.ja.md) | **简体中文** | [English](README.md)

> **Alpha 版本说明** — 此软件包处于早期开发阶段（`0.1.0a0`）。API 可能在版本之间发生不兼容变更。在生产环境使用前请进行充分测试。

将 [LangGraph](https://github.com/langchain-ai/langgraph) 智能体零样板代码部署为 **Azure Functions** HTTP 端点。

---

**Azure Functions Python DX Toolkit** 的一部分
→ 为 Azure Functions 带来 FastAPI 级别的开发体验

## 为什么需要

将 LangGraph 智能体部署到 Azure 比想象中更困难：

- **缺少 Azure 原生部署方式** — LangGraph Platform 由 LangChain 托管，而非 Azure
- **手动 HTTP 对接** — 将 `graph.invoke()` / `graph.stream()` 与 Azure Functions 连接需要大量重复的样板代码
- **缺少标准模式** — 每个团队都需要为编译后的图自行构建 HTTP 包装器

## 主要功能

- **零样板代码部署** — 注册编译后的图，自动获得 HTTP 端点
- **Invoke 端点** — `POST /api/graphs/{name}/invoke` 用于同步执行
- **Stream 端点** — `POST /api/graphs/{name}/stream` 用于缓冲式 SSE 响应
- **Health 端点** — `GET /api/health` 列出已注册图及检查点器状态
- **基于协议** — 与任何具有 `invoke()` 和 `stream()` 方法的对象兼容，不限于 LangGraph
- **检查点器透传** — 通过 LangGraph 原生 config 实现基于线程的对话状态管理

## 与 LangGraph Platform 对比

| 功能 | LangGraph Platform | azure-functions-langgraph |
|------|-------------------|--------------------------|
| 托管 | LangChain Cloud（付费） | 您的 Azure 订阅 |
| Invoke | `POST /runs/stream` | `POST /api/graphs/{name}/invoke` |
| 流式传输 | True SSE | 缓冲式 SSE（v0.1） |
| 线程 | 内置 | 通过 LangGraph 检查点器 |
| 基础设施 | 托管服务 | Azure Functions（无服务器） |
| 成本模型 | 按使用量/座位 | Azure Functions 定价 |

## 适用范围

- Azure Functions Python **v2 编程模型**
- 满足 `LangGraphLike` 协议的任何图（invoke + stream）
- 基于 Pydantic v2 的请求/响应契约

本软件包是**部署适配器** — 包装 LangGraph，而非替代它。

## 安装

```bash
pip install azure-functions-langgraph
```

您的 Azure Functions 应用还需要包含以下依赖：

```text
azure-functions
langgraph
azure-functions-langgraph
```

本地开发安装：

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph.git
cd azure-functions-langgraph
pip install -e .[dev]
```

## 快速开始

```python
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from azure_functions_langgraph import LangGraphApp


# 1. 定义状态
class AgentState(TypedDict):
    messages: list[dict[str, str]]


# 2. 定义节点函数
def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


# 3. 构建图
builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile()

# 4. 部署
app = LangGraphApp()
app.register(graph=graph, name="echo_agent")
func_app = app.function_app  # ← 用作 Azure Functions 应用
```

### 生成的端点

1. `POST /api/graphs/echo_agent/invoke` — 调用智能体
2. `POST /api/graphs/echo_agent/stream` — 流式传输智能体响应（缓冲式 SSE）
3. `GET /api/health` — 健康检查

### 请求格式

```json
{
    "input": {
        "messages": [{"role": "human", "content": "Hello!"}]
    },
    "config": {
        "configurable": {"thread_id": "conversation-1"}
    }
}
```

## 适用场景

- 需要将 LangGraph 智能体部署到 Azure Functions
- 需要无 LangGraph Platform 费用的无服务器部署
- 需要以最少配置为编译后的图创建 HTTP 端点
- 需要通过 LangGraph 检查点器实现基于线程的对话状态

## 文档

- 项目文档位于 `docs/`
- 经过测试的示例位于 `examples/`
- 产品需求：`PRD.md`
- 设计原则：`DESIGN.md`

## 生态系统

**Azure Functions Python DX Toolkit** 的一部分：

| 软件包 | 用途 |
|--------|------|
| [azure-functions-validation](https://github.com/yeongseon/azure-functions-validation) | 请求和响应验证 |
| [azure-functions-openapi](https://github.com/yeongseon/azure-functions-openapi) | OpenAPI 规范和 Swagger UI |
| [azure-functions-logging](https://github.com/yeongseon/azure-functions-logging) | 结构化日志和可观测性 |
| [azure-functions-doctor](https://github.com/yeongseon/azure-functions-doctor) | 部署前诊断 CLI |
| [azure-functions-scaffold](https://github.com/yeongseon/azure-functions-scaffold) | 项目脚手架 |
| **azure-functions-langgraph** | LangGraph 智能体部署 |
| [azure-functions-durable-graph](https://github.com/yeongseon/azure-functions-durable-graph) | 基于 Durable Functions 的清单图运行时 |
| [azure-functions-python-cookbook](https://github.com/yeongseon/azure-functions-python-cookbook) | 示例和食谱 |

## 免责声明

本项目是独立的社区项目，与 Microsoft 或 LangChain 没有隶属、认可或维护关系。

Azure 和 Azure Functions 是 Microsoft Corporation 的商标。
LangGraph 和 LangChain 是 LangChain, Inc. 的商标。

## 许可证

MIT
