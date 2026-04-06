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

> **ベータ版について** — このパッケージは活発に開発中（`0.4.0`）です。コアAPIは安定化に向かっていますが、マイナーリリース間で変更される可能性があります。GitHubでイシューを報告してください。

最小限のボイラープレートで [LangGraph](https://github.com/langchain-ai/langgraph) グラフを **Azure Functions** HTTPエンドポイントとしてデプロイできます。

---

**Azure Functions Python DX Toolkit** の一部

## なぜ必要か

Azure FunctionsでLangGraphをデプロイするのは、思ったより大変です。

- LangGraphはAzure Functionsネイティブなデプロイアダプターを提供していません
- コンパイル済みグラフをHTTPエンドポイントとして公開するには、繰り返しの接続コードが必要です
- チームごとに同じinvoke/streamラッパーを毎回新たに実装しています

このパッケージは、Azure Functions Python v2でLangGraphグラフをサーブするための専用アダプターを提供します。

## 主な機能

- **ボイラープレート不要のデプロイ** — コンパイル済みグラフを登録するだけで、HTTPエンドポイントが自動生成されます
- **Invokeエンドポイント** — `POST /api/graphs/{name}/invoke` で同期実行
- **Streamエンドポイント** — `POST /api/graphs/{name}/stream` でバッファリングされたSSEレスポンス
- **Healthエンドポイント** — `GET /api/health` で登録済みグラフ一覧とチェックポインターの状態を確認
- **チェックポインター転送** — LangGraphネイティブのconfigによるスレッドベースの会話状態管理
- **Stateエンドポイント** — `GET /api/graphs/{name}/threads/{thread_id}/state` でスレッド状態を検査（サポートされている場合）
- **グラフごとの認証** — `register(graph, name, auth_level=...)` でアプリレベルの認証をグラフごとにオーバーライド
- **LangGraph Platform API互換** — スレッド、ラン、アシスタント、ステートのためのSDK互換エンドポイント (v0.3+)
- **永続ストレージバックエンド** — Azure Blob Storageチェックポインター及びAzure Table Storageスレッドストア (v0.4+)

## LangGraph Platformとの比較

| 機能 | LangGraph Platform | azure-functions-langgraph |
|------|-------------------|--------------------------|
| ホスティング | LangChain Cloud（有料） | ユーザーのAzureサブスクリプション |
| アシスタント | 組み込み | SDK互換API (v0.3+) |
| スレッドライフサイクル | 組み込み | 作成、取得、更新、削除、検索、カウント (v0.3+) |
| ラン | 組み込み | スレッド付き + スレッドレスラン (v0.4+) |
| ステート読み取り/更新 | 組み込み | get_state + update_state (v0.4+) |
| ステート履歴 | 組み込み | フィルタリング対応チェックポイント履歴 (v0.4+) |
| ストリーミング | True SSE | バッファリングSSE |
| 永続ストレージ | 組み込み | Azure Blob + Table Storage (v0.4+) |
| インフラ | マネージド | Azure Functions（サーバーレス） |
| コストモデル | 使用量/シートベース | Azure Functions料金プラン |

## 対象範囲

- Azure Functions Python **v2プログラミングモデル**
- LangGraphグラフのデプロイとHTTP公開
- LangGraphランタイムの関心事: invoke, stream, threads, runs, state
- バリデーションとOpenAPIのためのコンパニオンパッケージ連携

このパッケージは**デプロイアダプター**です — LangGraphをラップしますが、置き換えるものではありません。

> 内部的に、グラフ登録はプロトコルベース（`LangGraphLike`）のままであり、プロトコルを満たす任意のオブジェクトが動作します — ただし、このパッケージのドキュメントと例はLangGraphのユースケースに焦点を当てています。

## このパッケージが行わないこと

このパッケージは以下を担当しません:
- OpenAPIドキュメント生成またはSwagger UI — [`azure-functions-openapi`](https://github.com/yeongseon/azure-functions-openapi)を使用
- LangGraph契約外のリクエスト/レスポンスバリデーション — [`azure-functions-validation`](https://github.com/yeongseon/azure-functions-validation)を使用
- LangGraph以外の汎用グラフサーブ抽象化

> **注意:** OpenAPI仕様の生成には、専用の [`azure-functions-openapi`](https://github.com/yeongseon/azure-functions-openapi) パッケージとブリッジモジュール（`azure_functions_langgraph.openapi.register_with_openapi`）を使用してください。

## インストール

```bash
pip install azure-functions-langgraph
```

Azureサービスによる永続ストレージ:

```bash
# Azure Blob Storageチェックポインター
pip install azure-functions-langgraph[azure-blob]

# Azure Table Storageスレッドストア
pip install azure-functions-langgraph[azure-table]

# 両方
pip install azure-functions-langgraph[azure-blob,azure-table]
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

### プロダクション認証

`LangGraphApp`はローカル開発の利便性のためにデフォルトで`AuthLevel.ANONYMOUS`を使用します。
プロダクションデプロイでは`FUNCTION`または`ADMIN`認証を使用し、Azure Functionsキーを送信してください。

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
```

### グラフごとの認証

アプリレベルの認証設定をグラフごとにオーバーライドできます:

```python
# グラフごとの認証オーバーライド
app.register(graph=public_graph, name="public", auth_level=func.AuthLevel.ANONYMOUS)
app.register(graph=private_graph, name="private", auth_level=func.AuthLevel.FUNCTION)
```

Functionキーを使用したリクエスト例:

```bash
curl -X POST "https://<app>.azurewebsites.net/api/graphs/echo_agent/invoke?code=<FUNCTION_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Hello!"}]}}'
```

### 生成されるエンドポイント

1. `POST /api/graphs/echo_agent/invoke` — エージェントの呼び出し
2. `POST /api/graphs/echo_agent/stream` — エージェントレスポンスのストリーミング（バッファリングSSE）
3. `GET /api/graphs/echo_agent/threads/{thread_id}/state` — スレッド状態の検査
4. `GET /api/health` — ヘルスチェック

`platform_compat=True`を設定すると、SDK互換エンドポイントも生成されます:

6. `POST /assistants/search` — 登録済みアシスタント一覧
7. `GET /assistants/{id}` — アシスタント詳細
8. `POST /assistants/count` — アシスタント数
9. `POST /threads` — スレッド作成
10. `GET /threads/{id}` — スレッド取得
11. `PATCH /threads/{id}` — スレッドメタデータ更新
12. `DELETE /threads/{id}` — スレッド削除
13. `POST /threads/search` — スレッド検索
14. `POST /threads/count` — スレッド数
15. `POST /threads/{id}/runs/wait` — 実行して結果を待機
16. `POST /threads/{id}/runs/stream` — 実行して結果をストリーミング
17. `POST /runs/wait` — スレッドレス実行
18. `POST /runs/stream` — スレッドレスストリーミング
19. `GET /threads/{id}/state` — スレッド状態取得
20. `POST /threads/{id}/state` — スレッド状態更新
21. `POST /threads/{id}/history` — ステート履歴

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

### 永続ストレージ (v0.4+)

チェックポイント永続化のためのAzure Blob Storageとスレッドメタデータ用のAzure Table Storageを使用します:

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


# Azure Blobチェックポインターでグラフをビルド
container_client = ContainerClient.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", "checkpoints"
)
saver = AzureBlobCheckpointSaver(container_client=container_client)

builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile(checkpointer=saver)

# Azure Tableスレッドストアでデプロイ
thread_store = AzureTableThreadStore.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", table_name="threads"
)

app = LangGraphApp(platform_compat=True)
app.thread_store = thread_store
app.register(graph=graph, name="echo_agent")
func_app = app.function_app
```

チェックポイントとスレッドメタデータはAzure Functions再起動後も維持され、インスタンス間で拡張されます。

### v0.3.0からのアップグレード

v0.4.0はv0.3.0と完全に後方互換です。ブレイキングチェンジはありません。

- **新しいオプショナルextras**: `pip install azure-functions-langgraph[azure-blob,azure-table]`で永続ストレージ
- **新しいプラットフォームエンドポイント**: スレッドCRUD、ステート更新/履歴、スレッドレスラン、アシスタントカウント
- **新しいプロトコル**: `UpdatableStateGraph`, `StateHistoryGraph` (`azure_functions_langgraph.protocols`から利用可能)

## 使用に適したケース

- LangGraphエージェントをAzure Functionsにデプロイしたい場合
- LangGraph Platformのコストなしでサーバーレスデプロイが必要な場合
- コンパイル済みグラフのHTTPエンドポイントを最小限の設定で必要とする場合
- LangGraphチェックポインターによるスレッドベースの会話状態が必要な場合
- Azure Blob/Table Storageによる永続的な状態保存が必要な場合

## ドキュメント

- プロジェクトドキュメント: `docs/`
- テスト済みサンプル: `examples/`
- 製品要件: `PRD.md`
- 設計原則: `DESIGN.md`

## エコシステム

このパッケージは **Azure Functions Python DX Toolkit** の一部です。

**設計原則:** `azure-functions-langgraph`はLangGraphランタイム公開を担当。`azure-functions-validation`はバリデーションを担当。`azure-functions-openapi`はAPIドキュメントを担当。

| パッケージ | 役割 |
|-----------|------|
| **azure-functions-langgraph** | Azure Functions用LangGraphデプロイアダプター |
| [azure-functions-validation](https://github.com/yeongseon/azure-functions-validation) | リクエスト/レスポンスバリデーションとシリアライゼーション |
| [azure-functions-openapi](https://github.com/yeongseon/azure-functions-openapi) | OpenAPI仕様生成とSwagger UI |
| [azure-functions-logging](https://github.com/yeongseon/azure-functions-logging) | 構造化ロギングとオブザーバビリティ |
| [azure-functions-doctor](https://github.com/yeongseon/azure-functions-doctor) | デプロイ前診断CLI |
| [azure-functions-scaffold](https://github.com/yeongseon/azure-functions-scaffold) | プロジェクトスキャフォールディング |
| [azure-functions-durable-graph](https://github.com/yeongseon/azure-functions-durable-graph) | Durable Functionsベースのマニフェストグラフランタイム |
| [azure-functions-python-cookbook](https://github.com/yeongseon/azure-functions-python-cookbook) | レシピとサンプル |

## 免責事項

このプロジェクトは独立したコミュニティプロジェクトであり、MicrosoftまたはLangChainとの提携、承認、保守関係にはありません。

AzureおよびAzure FunctionsはMicrosoft Corporationの商標です。
LangGraphおよびLangChainはLangChain, Inc.の商標です。

## ライセンス

MIT
