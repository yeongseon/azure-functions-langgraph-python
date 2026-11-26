# Azure Functions LangGraph

기존 [LangGraph](https://github.com/langchain-ai/langgraph) 그래프를 요청 래퍼 작성 없이 Azure Functions Python v2 HTTP 엔드포인트로 배포하는 어댑터입니다.
프롬프트·모델·도구를 만드는 에이전트 프레임워크가 아닙니다.

[English](README.md) · [日本語](README.ja.md) · [简体中文](README.zh-CN.md)

> 이 번역은 커뮤니티가 관리하며 최신 [English README](README.md)보다 뒤처질 수 있습니다. 영어 원문이 기준입니다.

## 설치

Python 3.11–3.14를 지원합니다. 플랜 및 런타임별 호스팅 지원은 [배포 가이드](docs/deployment.md)를 참고하세요.

```bash
pip install azure-functions-langgraph
```

## 빠른 시작

`host.json`이 있는 Functions 앱에 `function_app.py`로 저장합니다:

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

`func start`를 실행한 후 `http://localhost:7071/api/graphs/echo/invoke`로 `{"input":{"message":"hello"}}`를 POST 요청하면 `{"output":{"message":"hello"}}` 결과가 반환됩니다.
배포된 라우트는 기본적으로 함수 키를 요구합니다. 전체 프로젝트 파일, 로컬 설정 및 배포는 [5분 튜토리얼](docs/tutorial-5-min.md)을 참고하세요.
새 Functions 프로젝트에서 시작하는 스캐폴딩은 [새 Functions 프로젝트에서 시작하기](docs/getting-started.md#start-from-a-new-functions-project)를 참고하세요.

업스트림 출처와 별도로 기록된 Azure 배포 검증은 [공식 LangGraph 예제](examples/official_langgraph_basic/)를 참고하세요.

## 안정성

롤링 0.x이며 패키지 분류는 Beta입니다. 표면별 계약(Stable, Beta, Experimental)의 상세 내용은 [English README 안정성 표](README.md#stability--rolling-0x-per-surface)를 참고하세요.
기본 스트림은 버퍼링된 SSE이며 토큰 단위 실시간 전송이 아닙니다.
함수 키는 사용자 소유권 검증이 아니며 기본 잠금과 스레드 저장소는 프로세스 내부에만 존재합니다.
자세한 내용은 [프로덕션/분산 가이드](docs/production-guide.md#distributed-deployment)와 [보안](docs/security.md)을 참고하세요.

## 학습 경로

1. [Azure OpenAI](examples/azure_openai_agent/)
2. [대화 메모리](examples/conversation_memory/)
3. [도구 호출](examples/tool_calling_agent/)
4. [영구 저장소](examples/production_persistent_agent/)

예제는 개별 기능을 보여주는 독립 앱이며 클라우드 자격 증명 없이 fake 모델로 테스트할 수 있습니다.
[상세 참조와 마이그레이션](docs/adapter-reference.md), [SDK 지원 범위](COMPATIBILITY.md), [기여 안내](CONTRIBUTING.md).
독립 커뮤니티 프로젝트이며 Microsoft 또는 LangChain과 관련이 없습니다. [MIT](LICENSE).
