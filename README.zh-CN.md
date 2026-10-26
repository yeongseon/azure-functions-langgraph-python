# Azure Functions LangGraph

将现有 LangGraph 图部署为 Azure Functions Python v2 HTTP 端点的适配器，
不是用于创建提示词、模型或工具的代理框架。

> 此翻译由社区维护，可能落后于最新的 [English README](README.md)。英文版是权威来源。

## 安装与开始

Python 3.11–3.14：`pip install azure-functions-langgraph`。
请先完成[五分钟教程](docs/tutorial-5-min.md)，再阅读[用法](docs/usage.md)和[配置](docs/configuration.md)。

## 稳定性

采用滚动 0.x，包级分类为 Beta。核心 HTTP 契约为 Stable，检查点、锁、async、观测契约为 Beta。
Platform 兼容、真正 HTTP 流式传输、Service Bus、Durable 为 **Experimental**，可能在 minor 版本间变化。
默认流是缓冲 SSE，不是逐 token 实时传输。函数密钥不代表用户或线程所有权，
默认锁和线程存储仅存在于进程内。共享写入必须显式配置分布式存储和锁。
请阅读[生产与分布式指南](docs/production-guide.md#distributed-deployment)及[安全边界](docs/security.md)。

## 学习路径

1. [Azure OpenAI](examples/azure_openai_agent/)
2. [对话记忆](examples/conversation_memory/)
3. [工具调用](examples/tool_calling_agent/)
4. [持久化存储](examples/production_persistent_agent/)

各示例是独立的单功能应用，可通过 fake 模型在无云凭据时测试。
[详细参考与迁移](docs/adapter-reference.md)、[SDK 支持范围](COMPATIBILITY.md)、[贡献指南](CONTRIBUTING.md)。
这是独立社区项目，并非 Microsoft/LangChain 官方产品。[MIT](LICENSE)。
