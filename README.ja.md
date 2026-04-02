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

この文書の言語: [한국어](README.ko.md) | **日本語** | [简体中文](README.zh-CN.md) | [English](README.md)

> **アルファ版について** — このパッケージは初期開発段階（`0.1.0a0`）です。リリース間でAPIが予告なく変更される場合があります。本番環境での使用前に十分なテストを実施してください。

[LangGraph](https://github.com/langchain-ai/langgraph) エージェントを **Azure Functions** HTTPエンドポイントとしてボイラープレートなしでデプロイできます。

---

**Azure Functions Python DX Toolkit** の一部
→ Azure FunctionsにFastAPIレベルの開発者体験を提供

## なぜ必要か

LangGraphエージェントをAzureにデプロイするのは、思ったより大変です:

- **Azureネイティブなデプロイ方法がない** — LangGraph PlatformはLangChainがホスティングしており、Azureではありません
- **手動のHTTP接続** — `graph.invoke()` / `graph.stream()` をAzure Functionsと接続するには、繰り返しのボイラープレートコードが必要です
- **標準パターンがない** — チームごとにコンパイル済みグラフのHTTPラッパーを独自に実装しています

## 主な機能

- **ボイラープレート不要のデプロイ** — コンパイル済みグラフを登録するだけで、HTTPエンドポイントが自動生成されます
- **Invokeエンドポイント** — `POST /api/graphs/{name}/invoke` で同期実行
- **Streamエンドポイント** — `POST /api/graphs/{name}/stream` でバッファリングされたSSEレスポンス
- **Healthエンドポイント** — `GET /api/health` で登録済みグラフ一覧とチェックポインターの状態を確認
- **プロトコルベース** — `invoke()` と `stream()` メソッドを持つ任意のオブジェクトで動作し、LangGraphに限定されません
- **チェックポインター転送** — LangGraphネイティブのconfigによるスレッドベースの会話状態管理

## LangGraph Platformとの比較

| 機能 | LangGraph Platform | azure-functions-langgraph |
|------|-------------------|--------------------------|
| ホスティング | LangChain Cloud（有料） | ユーザーのAzureサブスクリプション |
| Invoke | `POST /runs/stream` | `POST /api/graphs/{name}/invoke` |
| ストリーミング | True SSE | バッファリングSSE（v0.1） |
| スレッド | 組み込み | LangGraphチェックポインター経由 |
| インフラ | マネージド | Azure Functions（サーバーレス） |
| コストモデル | 使用量/シートベース | Azure Functions料金プラン |

## 対象範囲

- Azure Functions Python **v2プログラミングモデル**
- `LangGraphLike` プロトコルを満たす任意のグラフ（invoke + stream）
- Pydantic v2ベースのリクエスト/レスポンスコントラクト

このパッケージは**デプロイアダプター**です — LangGraphをラップしますが、置き換えるものではありません。

## インストール

```bash
pip install azure-functions-langgraph
```

Azure Functionsアプリには以下の依存関係も含めてください:

```text
azure-functions
langgraph
azure-functions-langgraph
```

ローカル開発用インストール:

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph.git
cd azure-functions-langgraph
pip install -e .[dev]
```

## クイックスタート

```python
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from azure_functions_langgraph import LangGraphApp


# 1. 状態を定義
class AgentState(TypedDict):
    messages: list[dict[str, str]]


# 2. ノード関数を定義
def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


# 3. グラフをビルド
builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile()

# 4. デプロイ
app = LangGraphApp()
app.register(graph=graph, name="echo_agent")
func_app = app.function_app  # ← Azure Functionsアプリとして使用
```

### 生成されるエンドポイント

1. `POST /api/graphs/echo_agent/invoke` — エージェントの呼び出し
2. `POST /api/graphs/echo_agent/stream` — エージェントレスポンスのストリーミング（バッファリングSSE）
3. `GET /api/health` — ヘルスチェック

### リクエスト形式

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

## 使用に適したケース

- LangGraphエージェントをAzure Functionsにデプロイしたい場合
- LangGraph Platformのコストなしでサーバーレスデプロイが必要な場合
- コンパイル済みグラフのHTTPエンドポイントを最小限の設定で必要とする場合
- LangGraphチェックポインターによるスレッドベースの会話状態が必要な場合

## ドキュメント

- プロジェクトドキュメント: `docs/`
- テスト済みサンプル: `examples/`
- 製品要件: `PRD.md`
- 設計原則: `DESIGN.md`

## エコシステム

**Azure Functions Python DX Toolkit** の一部:

| パッケージ | 役割 |
|-----------|------|
| [azure-functions-validation](https://github.com/yeongseon/azure-functions-validation) | リクエスト/レスポンスバリデーション |
| [azure-functions-openapi](https://github.com/yeongseon/azure-functions-openapi) | OpenAPI仕様とSwagger UI |
| [azure-functions-logging](https://github.com/yeongseon/azure-functions-logging) | 構造化ロギングとオブザーバビリティ |
| [azure-functions-doctor](https://github.com/yeongseon/azure-functions-doctor) | デプロイ前診断CLI |
| [azure-functions-scaffold](https://github.com/yeongseon/azure-functions-scaffold) | プロジェクトスキャフォールディング |
| **azure-functions-langgraph** | LangGraphエージェントのデプロイ |
| [azure-functions-durable-graph](https://github.com/yeongseon/azure-functions-durable-graph) | Durable Functionsベースのマニフェストグラフランタイム |
| [azure-functions-python-cookbook](https://github.com/yeongseon/azure-functions-python-cookbook) | レシピとサンプル |

## 免責事項

このプロジェクトは独立したコミュニティプロジェクトであり、MicrosoftまたはLangChainとの提携、承認、保守関係にはありません。

AzureおよびAzure FunctionsはMicrosoft Corporationの商標です。
LangGraphおよびLangChainはLangChain, Inc.の商標です。

## ライセンス

MIT
