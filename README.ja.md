# Azure Functions LangGraph

既存の LangGraph グラフを Azure Functions Python v2 の HTTP エンドポイントとして公開するアダプターです。
プロンプト・モデル・ツールを構築するエージェントフレームワークではありません。

> この翻訳はコミュニティ管理で、最新の [English README](README.md) より古い場合があります。英語版が正本です。

## インストールと開始

Python 3.11–3.14: `pip install azure-functions-langgraph`。
[5分チュートリアル](docs/tutorial-5-min.md)、[使い方](docs/usage.md)、[設定](docs/configuration.md)を参照してください。

## 安定性

ローリング 0.x、パッケージ分類は Beta です。コア HTTP 契約は Stable、
チェックポイント・ロック・async・観測契約は Beta です。Platform 互換、真の HTTP
ストリーミング、Service Bus、Durable は **Experimental** で、minor リリース間でも変更される場合があります。
標準ストリームはバッファリングされた SSE で、トークンの逐次配信ではありません。
関数キーはユーザー所有権の認証ではなく、標準ロックとスレッドストアはプロセス内のみです。
[本番・分散ガイド](docs/production-guide.md#distributed-deployment)と[セキュリティ](docs/security.md)を確認してください。

## 学習順序

1. [Azure OpenAI](examples/azure_openai_agent/)
2. [会話メモリ](examples/conversation_memory/)
3. [ツール呼び出し](examples/tool_calling_agent/)
4. [永続ストレージ](examples/production_persistent_agent/)

各例は独立した機能別アプリで、クラウド資格情報なしの fake モデルで検証できます。
[詳細参照と移行](docs/adapter-reference.md)、[SDK 対応範囲](COMPATIBILITY.md)、[貢献](CONTRIBUTING.md)。
Microsoft/LangChain と提携していないコミュニティプロジェクトです。[MIT](LICENSE)。
