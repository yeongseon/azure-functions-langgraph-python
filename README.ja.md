# Azure Functions LangGraph

既存の [LangGraph](https://github.com/langchain-ai/langgraph) グラフをリクエストラッパーを書かずに Azure Functions Python v2 の HTTP エンドポイントとしてデプロイするアダプターです。
プロンプト・モデル・ツールを構築するエージェントフレームワークではありません。

[English](README.md) · [한국어](README.ko.md) · [简体中文](README.zh-CN.md)

> この翻訳はコミュニティ管理で、最新の [English README](README.md) より古い場合があります。英語版が正本です。

## インストール

Python 3.11–3.14 をサポートしています。プランおよびランタイム別のホスティング対応については[デプロイガイド](docs/deployment.md)を参照してください。

```bash
pip install azure-functions-langgraph
```

## クイックスタート

`host.json` のある Functions アプリ内で `function_app.py` として保存します:

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

`func start` を実行し、`http://localhost:7071/api/graphs/echo/invoke` に `{"input":{"message":"hello"}}` を POST すると、`{"output":{"message":"hello"}}` が返されます。
デプロイ済みのルートは既定で関数キーが必要です。完全なプロジェクトファイル、ローカル設定、デプロイ手順については [5分チュートリアル](docs/tutorial-5-min.md)を参照してください。
新しい Functions プロジェクトからスキャフォールディングを開始する場合は、[新規 Functions プロジェクトから始める](docs/getting-started.md#start-from-a-new-functions-project)を参照してください。

アップストリームの出所と個別に記録された Azure デプロイ検証については、[公式 LangGraph 例](examples/official_langgraph_basic/)を参照してください。

## 安定性

ローリング 0.x、パッケージ分類は Beta です。サーフェス別（Stable, Beta, Experimental）の詳細な契約は [English README 安定性テーブル](README.md#stability--rolling-0x-per-surface)を参照してください。
標準ストリームはバッファリングされた SSE であり、トークン単位の逐次配信ではありません。
関数キーはユーザー所有権の認証ではなく、標準ロックとスレッドストアはプロセス内のみです。
詳細は[本番・分散ガイド](docs/production-guide.md#distributed-deployment)と[セキュリティ](docs/security.md)を参照してください。

## 学習順序

1. [Azure OpenAI](examples/azure_openai_agent/)
2. [会話メモリ](examples/conversation_memory/)
3. [ツール呼び出し](examples/tool_calling_agent/)
4. [永続ストレージ](examples/production_persistent_agent/)

各例は独立した機能別アプリで、クラウド資格情報なしの fake モデルで検証できます。
[詳細参照と移行](docs/adapter-reference.md)、[SDK 対応範囲](COMPATIBILITY.md)、[貢献ガイド](CONTRIBUTING.md)。
Microsoft または LangChain と提携していない独立したコミュニティプロジェクトです。[MIT](LICENSE)。
