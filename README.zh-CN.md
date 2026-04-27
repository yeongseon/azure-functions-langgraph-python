# Azure Functions LangGraph

[![PyPI](https://img.shields.io/pypi/v/azure-functions-langgraph.svg)](https://pypi.org/project/azure-functions-langgraph/)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/azure-functions-langgraph/)
[![CI](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml)
[![Release](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/publish-pypi.yml)
[![Security Scans](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml)
[![codecov](https://codecov.io/gh/yeongseon/azure-functions-langgraph/branch/main/graph/badge.svg)](https://codecov.io/gh/yeongseon/azure-functions-langgraph)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://pre-commit.com/)
[![Docs](https://img.shields.io/badge/docs-gh--pages-blue)](https://yeongseon.github.io/azure-functions-langgraph-python/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

本文档语言: [한국어](README.ko.md) | [日本語](README.ja.md) | **简体中文** | [English](README.md)

> **Beta 版本说明** — 此软件包正在积极开发中。核心 API 趋于稳定，但在 v1.0 之前可能仍会发生变更。请在 GitHub 上报告问题。

以最少的样板代码将 [LangGraph](https://github.com/langchain-ai/langgraph) 图部署为 **Azure Functions** HTTP 端点。

---

**Azure Functions Python DX Toolkit** 的一部分

## 为什么需要

在 Azure Functions 上部署 LangGraph 比想象中更困难。

- LangGraph 不提供 Azure Functions 原生部署适配器
- 将编译后的图公开为 HTTP 端点需要重复的连接代码
- 团队经常为每个项目重新构建相同的 invoke/stream 包装器

本软件包提供了在 Azure Functions Python v2 上部署 LangGraph 图的专用适配器。

## 主要功能

- **零样板代码部署** — 注册编译后的图，自动获得 HTTP 端点
- **Invoke 端点** — `POST /api/graphs/{name}/invoke` 用于同步执行
- **Stream 端点** — `POST /api/graphs/{name}/stream` 用于缓冲式 SSE 响应
- **Health 端点** — `GET /api/health` 列出已注册图及检查点器状态
- **检查点器透传** — 通过 LangGraph 原生 config 实现基于线程的对话状态管理
- **State 端点** — `GET /api/graphs/{name}/threads/{thread_id}/state` 用于线程状态检查（支持时）
- **按图认证** — `register(graph, name, auth_level=...)` 按图覆盖应用级认证
- **LangGraph Platform API 兼容** — 线程、运行、助手、状态的 SDK 兼容端点 (v0.3+)
- **持久存储后端** — Azure Blob Storage 检查点器及 Azure Table Storage 线程存储 (v0.4+)

## 与 LangGraph Platform 对比

| 功能 | LangGraph Platform | azure-functions-langgraph |
|------|-------------------|--------------------------|
| 托管 | LangChain Cloud（付费） | 您的 Azure 订阅 |
| 助手 | 内置 | SDK 兼容 API (v0.3+) |
| 线程生命周期 | 内置 | 创建、获取、更新、删除、搜索、计数 (v0.3+) |
| 运行 | 内置 | 线程化 + 无线程运行 (v0.4+) |
| 状态读取/更新 | 内置 | get_state + update_state (v0.4+) |
| 状态历史 | 内置 | 支持过滤的检查点历史 (v0.4+) |
| 流式传输 | True SSE | 缓冲式 SSE |
| 持久存储 | 内置 | Azure Blob + Table Storage (v0.4+) |
| 基础设施 | 托管服务 | Azure Functions（无服务器） |
| 成本模型 | 按使用量/座位 | Azure Functions 定价 |

> 关于按功能划分的 SDK 支持矩阵（包括返回 `501 Not Implemented` 的 `RunCreate` 字段、线程过滤器和 SDK 调用），请参阅 [COMPATIBILITY.md](COMPATIBILITY.md)。

## 适用范围

- Azure Functions Python **v2 编程模型**
- LangGraph 图部署和 HTTP 公开
- LangGraph 运行时关注点：invoke、stream、threads、runs、state
- 通过伴侣软件包进行验证和 OpenAPI 的可选集成

本软件包是**部署适配器** — 包装 LangGraph，而非替代它。

> 在内部，图注册仍然基于协议（`LangGraphLike`），因此任何满足协议的对象都可以工作 — 但本软件包的文档和示例专注于 LangGraph 使用场景。

## 本软件包不做的事

本软件包不负责：
- OpenAPI 文档生成或 Swagger UI — 请使用 [`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python)
- LangGraph 契约之外的请求/响应验证 — 请使用 [`azure-functions-validation-python`](https://github.com/yeongseon/azure-functions-validation-python)
- LangGraph 之外的通用图服务抽象

> **注意：** 生成 OpenAPI 规范请使用专用的 [`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python) 软件包与桥接模块（`azure_functions_langgraph.openapi.register_with_openapi`）。

## 安装

```bash
pip install azure-functions-langgraph
```

Azure 服务持久存储：

```bash
# Azure Blob Storage 检查点器
pip install azure-functions-langgraph[azure-blob]

# Azure Table Storage 线程存储
pip install azure-functions-langgraph[azure-table]

# 两者都要
pip install azure-functions-langgraph[azure-blob,azure-table]
```

您的 Azure Functions 应用还需要包含以下依赖：

```text
azure-functions
langgraph
azure-functions-langgraph
```

本地开发安装：

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
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

### 生产环境认证

`LangGraphApp` 默认使用 `AuthLevel.ANONYMOUS` 以方便本地开发。
生产部署时，建议使用 `FUNCTION` 或 `ADMIN` 认证并发送 Azure Functions 密钥。

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
```

### 流式传输行为

> **重要：** 所有 `/stream` 端点（包括原生的 `POST /api/graphs/{name}/stream`，
> 以及 Platform 兼容的 `POST /threads/{id}/runs/stream` 和 `POST /runs/stream`）
> 都返回**缓冲式 SSE**。图执行过程中产生的 chunk 会先被收集，待运行**完成后**
> 一次性以 SSE 事件形式发送。这并非真正的逐 token 流式传输，客户端无法增量
> 接收部分 token。
>
> 真正的分块流式传输已列入路线图，依赖于 Azure Functions Python v2 对
> streaming response 的支持。如果当前确实需要实时 token 级流式传输，建议
> 在长时间运行的宿主（如 App Service 或 AKS）中运行图。

### 按图认证

可以按图覆盖应用级认证设置：

```python
# 按图认证覆盖
app.register(graph=public_graph, name="public", auth_level=func.AuthLevel.ANONYMOUS)
app.register(graph=private_graph, name="private", auth_level=func.AuthLevel.FUNCTION)
```

使用 Function 密钥的请求示例：

```bash
curl -X POST "https://<app>.azurewebsites.net/api/graphs/echo_agent/invoke?code=<FUNCTION_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Hello!"}]}}'
```

### 生成的端点

1. `POST /api/graphs/echo_agent/invoke` — 调用智能体
2. `POST /api/graphs/echo_agent/stream` — 流式传输智能体响应（缓冲式 SSE，非真正逐 token 流式传输）
3. `GET /api/graphs/echo_agent/threads/{thread_id}/state` — 检查线程状态
4. `GET /api/health` — 健康检查

设置 `platform_compat=True` 时还会生成 SDK 兼容端点：

6. `POST /assistants/search` — 列出已注册助手
7. `GET /assistants/{id}` — 获取助手详情
8. `POST /assistants/count` — 助手计数
9. `POST /threads` — 创建线程
10. `GET /threads/{id}` — 获取线程
11. `PATCH /threads/{id}` — 更新线程元数据
12. `DELETE /threads/{id}` — 删除线程
13. `POST /threads/search` — 搜索线程
14. `POST /threads/count` — 线程计数
15. `POST /threads/{id}/runs/wait` — 运行并等待结果
16. `POST /threads/{id}/runs/stream` — 运行并流式传输结果（缓冲式 SSE）
17. `POST /runs/wait` — 无线程运行
18. `POST /runs/stream` — 无线程流式传输（缓冲式 SSE）
19. `GET /threads/{id}/state` — 获取线程状态
20. `POST /threads/{id}/state` — 更新线程状态
21. `POST /threads/{id}/history` — 获取状态历史

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

### 持久存储 (v0.4+)

使用 Azure Blob Storage 进行检查点持久化，使用 Azure Table Storage 存储线程元数据：

```python
from azure.storage.blob import ContainerClient
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.azure_blob import AzureBlobCheckpointSaver
from azure_functions_langgraph.stores.azure_table import AzureTableThreadStore


class AgentState(TypedDict):
    messages: list[dict[str, str]]


def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


# 使用 Azure Blob 检查点器构建图
container_client = ContainerClient.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", "checkpoints"
)
saver = AzureBlobCheckpointSaver(container_client=container_client)

builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile(checkpointer=saver)

# 使用 Azure Table 线程存储部署
thread_store = AzureTableThreadStore.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", table_name="threads"
)

app = LangGraphApp(platform_compat=True)
app.thread_store = thread_store
app.register(graph=graph, name="echo_agent")
func_app = app.function_app
```

检查点和线程元数据在 Azure Functions 重启后仍然保留，并可跨实例扩展。

### 规模适用范围

随包提供的持久化后端面向开发与中小规模生产部署。在突破以下限制前请提前规划:

| 后端 | 舒适区间 | 注意区间 | 建议更换后端 |
|---|---|---|---|
| `AzureBlobCheckpointSaver` | 每线程检查点 < 100，线程数 < 10K | 每线程检查点 100–1000 | 使用 Cosmos DB 或基于 Redis 的检查点器 |
| `AzureTableThreadStore` | 线程数 < 100K，搜索负载较轻 | 线程数 100K–500K | 使用分片线程存储或 Cosmos DB |

说明:

- **单分区** — `AzureTableThreadStore` 将所有线程置于同一 PartitionKey 下，受 Azure Table 单分区吞吐量（Standard 账户约 2000 实体/秒）限制。除 `status` 以外的搜索与计数过滤均在**客户端**完成，详见 [COMPATIBILITY.md](COMPATIBILITY.md)。
- **前缀扫描** — `AzureBlobCheckpointSaver` 通过 blob 前缀扫描列出检查点，事务数与延迟随每线程检查点数量增长。请使用下文的保留辅助函数加以约束。
- **实体大小** — Azure Table 实体上限为 1 MB；当达到阈值的 90% 时会记录警告。

#### 保留辅助函数

`AzureBlobCheckpointSaver` 提供两个用于定期清理（例如 Timer 触发的 Function）的辅助方法:

```python
# 每个 (线程, 命名空间) 仅保留最新 50 个检查点
saver.delete_old_checkpoints(thread_id="conversation-1", keep_last=50)

# 或删除某个检查点 id 之前的所有检查点
saver.delete_checkpoints_before(
    thread_id="conversation-1",
    before_checkpoint_id="01HXY...",
)
```

两个辅助函数仅删除检查点标记、元数据和 write blob。通道值 blob（位于 `values/` 下）和 `latest.json` 指针被有意保留，因此保留下来的检查点仍可正常使用。

> **注意** — 这些辅助函数是安全的，但**并不完整**。仅被刚刚删除的检查点引用的通道值 blob 会变为 orphan 且不会被删除。对于频繁创建检查点的长时间运行线程，这些 orphan blob 会随时间逐渐占据大部分存储空间。完整的值 blob 垃圾回收作为可选的 opt-in 辅助函数候选项在 [#153](https://github.com/yeongseon/azure-functions-langgraph-python/issues/153) 中跟踪。

### 从 v0.3.0 升级

v0.4.0 与 v0.3.0 完全向后兼容。无破坏性变更。

- **新的可选 extras**：`pip install azure-functions-langgraph[azure-blob,azure-table]` 用于持久存储
- **新的平台端点**：线程 CRUD、状态更新/历史、无线程运行、助手计数
- **新的协议**：`UpdatableStateGraph`、`StateHistoryGraph`（从 `azure_functions_langgraph.protocols` 可用）

## 适用场景

- 需要将 LangGraph 智能体部署到 Azure Functions
- 需要无 LangGraph Platform 费用的无服务器部署
- 需要以最少配置为编译后的图创建 HTTP 端点
- 需要通过 LangGraph 检查点器实现基于线程的对话状态
- 需要通过 Azure Blob/Table Storage 实现持久状态存储

## 文档

- 项目文档位于 `docs/`
- 经过测试的示例位于 `examples/`
- 产品需求：`PRD.md`
- 设计原则：`DESIGN.md`

## 生态系统

本软件包是 **Azure Functions Python DX Toolkit** 的一部分。

**设计原则：** `azure-functions-langgraph` 负责 LangGraph 运行时公开。`azure-functions-validation-python` 负责验证。`azure-functions-openapi-python` 负责 API 文档。

| 软件包 | 用途 |
|--------|------|
| **azure-functions-langgraph** | Azure Functions 用 LangGraph 部署适配器 |
| [azure-functions-validation-python](https://github.com/yeongseon/azure-functions-validation-python) | 请求/响应验证和序列化 |
| [azure-functions-openapi-python](https://github.com/yeongseon/azure-functions-openapi-python) | OpenAPI 规范生成和 Swagger UI |
| [azure-functions-logging-python](https://github.com/yeongseon/azure-functions-logging-python) | 结构化日志和可观测性 |
| [azure-functions-doctor-python](https://github.com/yeongseon/azure-functions-doctor-python) | 部署前诊断 CLI |
| [azure-functions-scaffold-python](https://github.com/yeongseon/azure-functions-scaffold-python) | 项目脚手架 |
| [azure-functions-durable-graph-python](https://github.com/yeongseon/azure-functions-durable-graph-python) | 基于 Durable Functions 的清单图运行时 |
| [azure-functions-cookbook-python](https://github.com/yeongseon/azure-functions-cookbook-python) | 示例和食谱 |

## 免责声明

本项目是独立的社区项目，与 Microsoft 或 LangChain 没有隶属、认可或维护关系。

Azure 和 Azure Functions 是 Microsoft Corporation 的商标。
LangGraph 和 LangChain 是 LangChain, Inc. 的商标。

## 许可证

MIT
