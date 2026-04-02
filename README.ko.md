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

이 문서의 언어: **한국어** | [日本語](README.ja.md) | [简体中文](README.zh-CN.md) | [English](README.md)

> **알파 버전 안내** — 이 패키지는 초기 개발 단계(`0.1.0a0`)입니다. 릴리스 사이에 API가 예고 없이 변경될 수 있습니다. 프로덕션 사용 전 충분한 테스트를 수행하세요.

[LangGraph](https://github.com/langchain-ai/langgraph) 에이전트를 **Azure Functions** HTTP 엔드포인트로 보일러플레이트 없이 배포하세요.

---

**Azure Functions Python DX Toolkit**의 일부
→ Azure Functions에 FastAPI 수준의 개발자 경험 제공

## 왜 필요한가

LangGraph 에이전트를 Azure에 배포하는 것은 생각보다 어렵습니다:

- **Azure 네이티브 배포 방식 부재** — LangGraph Platform은 LangChain에서 호스팅하며 Azure가 아닙니다
- **수동 HTTP 연결** — `graph.invoke()` / `graph.stream()`을 Azure Functions와 연결하려면 반복적인 보일러플레이트 코드가 필요합니다
- **표준 패턴 부재** — 팀마다 컴파일된 그래프에 대한 HTTP 래퍼를 직접 구현합니다

## 주요 기능

- **보일러플레이트 없는 배포** — 컴파일된 그래프를 등록하면 HTTP 엔드포인트가 자동으로 생성됩니다
- **Invoke 엔드포인트** — `POST /api/graphs/{name}/invoke`로 동기 실행
- **Stream 엔드포인트** — `POST /api/graphs/{name}/stream`으로 버퍼링된 SSE 응답
- **Health 엔드포인트** — `GET /api/health`로 등록된 그래프 목록과 체크포인터 상태 확인
- **프로토콜 기반** — `invoke()`와 `stream()` 메서드를 가진 모든 객체에서 동작하며, LangGraph에 한정되지 않습니다
- **체크포인터 전달** — LangGraph 네이티브 config를 통한 스레드 기반 대화 상태 관리

## LangGraph Platform 비교

| 기능 | LangGraph Platform | azure-functions-langgraph |
|------|-------------------|--------------------------|
| 호스팅 | LangChain Cloud (유료) | 사용자의 Azure 구독 |
| Invoke | `POST /runs/stream` | `POST /api/graphs/{name}/invoke` |
| 스트리밍 | True SSE | 버퍼링된 SSE (v0.1) |
| 스레드 | 내장 | LangGraph 체크포인터 사용 |
| 인프라 | 관리형 | Azure Functions (서버리스) |
| 비용 모델 | 사용량/좌석 기반 | Azure Functions 요금제 |

## 범위

- Azure Functions Python **v2 프로그래밍 모델**
- `LangGraphLike` 프로토콜을 만족하는 모든 그래프 (invoke + stream)
- Pydantic v2 기반 요청/응답 계약

이 패키지는 **배포 어댑터**입니다 — LangGraph를 감싸며, 대체하지 않습니다.

## 설치

```bash
pip install azure-functions-langgraph
```

Azure Functions 앱에 다음 의존성도 포함해야 합니다:

```text
azure-functions
langgraph
azure-functions-langgraph
```

로컬 개발 설치:

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph.git
cd azure-functions-langgraph
pip install -e .[dev]
```

## 빠른 시작

```python
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from azure_functions_langgraph import LangGraphApp


# 1. 상태 정의
class AgentState(TypedDict):
    messages: list[dict[str, str]]


# 2. 노드 함수 정의
def chat(state: AgentState) -> dict:
    user_msg = state["messages"][-1]["content"]
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


# 3. 그래프 빌드
builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile()

# 4. 배포
app = LangGraphApp()
app.register(graph=graph, name="echo_agent")
func_app = app.function_app  # ← Azure Functions 앱으로 사용
```

### 생성되는 엔드포인트

1. `POST /api/graphs/echo_agent/invoke` — 에이전트 호출
2. `POST /api/graphs/echo_agent/stream` — 에이전트 응답 스트리밍 (버퍼링된 SSE)
3. `GET /api/health` — 헬스 체크

### 요청 형식

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

## 언제 사용하면 좋은가

- LangGraph 에이전트를 Azure Functions에 배포하고 싶을 때
- LangGraph Platform 비용 없이 서버리스 배포가 필요할 때
- 컴파일된 그래프에 대한 HTTP 엔드포인트를 최소한의 설정으로 필요할 때
- LangGraph 체크포인터를 통한 스레드 기반 대화 상태가 필요할 때

## 문서

- 프로젝트 문서: `docs/`
- 예제: `examples/`
- 제품 요구사항: `PRD.md`
- 설계 원칙: `DESIGN.md`

## 에코시스템

**Azure Functions Python DX Toolkit**의 일부:

| 패키지 | 역할 |
|--------|------|
| [azure-functions-validation](https://github.com/yeongseon/azure-functions-validation) | 요청/응답 유효성 검사 |
| [azure-functions-openapi](https://github.com/yeongseon/azure-functions-openapi) | OpenAPI 스펙 및 Swagger UI |
| [azure-functions-logging](https://github.com/yeongseon/azure-functions-logging) | 구조화된 로깅 및 관측성 |
| [azure-functions-doctor](https://github.com/yeongseon/azure-functions-doctor) | 배포 전 진단 CLI |
| [azure-functions-scaffold](https://github.com/yeongseon/azure-functions-scaffold) | 프로젝트 스캐폴딩 |
| **azure-functions-langgraph** | LangGraph 에이전트 배포 |
| [azure-functions-durable-graph](https://github.com/yeongseon/azure-functions-durable-graph) | Durable Functions 기반 매니페스트 그래프 런타임 |
| [azure-functions-python-cookbook](https://github.com/yeongseon/azure-functions-python-cookbook) | 레시피 및 예제 |

## 면책 조항

이 프로젝트는 독립적인 커뮤니티 프로젝트이며, Microsoft 또는 LangChain과 제휴, 보증, 유지보수 관계에 있지 않습니다.

Azure 및 Azure Functions는 Microsoft Corporation의 상표입니다.
LangGraph 및 LangChain은 LangChain, Inc.의 상표입니다.

## 라이선스

MIT
