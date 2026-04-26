# Azure Functions LangGraph

[![PyPI](https://img.shields.io/pypi/v/azure-functions-langgraph-python.svg)](https://pypi.org/project/azure-functions-langgraph-python/)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/azure-functions-langgraph-python/)
[![CI](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/ci-test.yml)
[![Release](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/publish-pypi.yml)
[![Security Scans](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml/badge.svg)](https://github.com/yeongseon/azure-functions-langgraph-python/actions/workflows/security.yml)
[![codecov](https://codecov.io/gh/yeongseon/azure-functions-langgraph-python/branch/main/graph/badge.svg)](https://codecov.io/gh/yeongseon/azure-functions-langgraph-python)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://pre-commit.com/)
[![Docs](https://img.shields.io/badge/docs-gh--pages-blue)](https://yeongseon.github.io/azure-functions-langgraph-python/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

이 문서의 언어: **한국어** | [日本語](README.ja.md) | [简体中文](README.zh-CN.md) | [English](README.md)

> **베타 버전 안내** — 이 패키지는 활발히 개발 중(`0.4.0`)입니다. 핵심 API가 안정화되고 있으나 마이너 릴리스 간에 변경될 수 있습니다. GitHub에서 이슈를 보고해 주세요.

[LangGraph](https://github.com/langchain-ai/langgraph) 그래프를 최소한의 보일러플레이트로 **Azure Functions** HTTP 엔드포인트로 배포하세요.

---

**Azure Functions Python DX Toolkit**의 일부

## 왜 필요한가

Azure Functions에서 LangGraph를 배포하는 것은 생각보다 어렵습니다.

- LangGraph는 Azure Functions 네이티브 배포 어댑터를 제공하지 않습니다
- 컴파일된 그래프를 HTTP 엔드포인트로 노출하려면 반복적인 연결 코드가 필요합니다
- 팀마다 동일한 invoke/stream 래퍼를 매번 새로 구현합니다

이 패키지는 Azure Functions Python v2에서 LangGraph 그래프를 서빙하기 위한 전용 어댑터를 제공합니다.

## 주요 기능

- **보일러플레이트 없는 배포** — 컴파일된 그래프를 등록하면 HTTP 엔드포인트가 자동으로 생성됩니다
- **Invoke 엔드포인트** — `POST /api/graphs/{name}/invoke`로 동기 실행
- **Stream 엔드포인트** — `POST /api/graphs/{name}/stream`으로 버퍼링된 SSE 응답
- **Health 엔드포인트** — `GET /api/health`로 등록된 그래프 목록과 체크포인터 상태 확인
- **체크포인터 전달** — LangGraph 네이티브 config를 통한 스레드 기반 대화 상태 관리
- **State 엔드포인트** — `GET /api/graphs/{name}/threads/{thread_id}/state`로 스레드 상태 조회 (지원되는 경우)
- **그래프별 인증** — `register(graph, name, auth_level=...)`로 앱 수준 인증을 그래프별로 재정의
- **LangGraph Platform API 호환** — 스레드, 실행, 어시스턴트, 상태를 위한 SDK 호환 엔드포인트 (v0.3+)
- **영구 스토리지 백엔드** — Azure Blob Storage 체크포인터 및 Azure Table Storage 스레드 스토어 (v0.4+)

## LangGraph Platform 비교

| 기능 | LangGraph Platform | azure-functions-langgraph-python |
|------|-------------------|--------------------------|
| 호스팅 | LangChain Cloud (유료) | 사용자의 Azure 구독 |
| 어시스턴트 | 내장 | SDK 호환 API (v0.3+) |
| 스레드 라이프사이클 | 내장 | 생성, 조회, 수정, 삭제, 검색, 카운트 (v0.3+) |
| 실행 | 내장 | 스레드 기반 + 스레드리스 실행 (v0.4+) |
| 상태 읽기/수정 | 내장 | get_state + update_state (v0.4+) |
| 상태 히스토리 | 내장 | 필터링 지원 체크포인트 히스토리 (v0.4+) |
| 스트리밍 | True SSE | 버퍼링된 SSE |
| 영구 스토리지 | 내장 | Azure Blob + Table Storage (v0.4+) |
| 인프라 | 관리형 | Azure Functions (서버리스) |
| 비용 모델 | 사용량/좌석 기반 | Azure Functions 요금제 |

## 범위

- Azure Functions Python **v2 프로그래밍 모델**
- LangGraph 그래프 배포 및 HTTP 노출
- LangGraph 런타임 관심사: invoke, stream, threads, runs, state
- 검증과 OpenAPI를 위한 컴패니언 패키지 연동 지원

이 패키지는 **배포 어댑터**입니다 — LangGraph를 감싸며, 대체하지 않습니다.

> 내부적으로 그래프 등록은 프로토콜 기반(`LangGraphLike`)으로 유지되므로, 프로토콜을 만족하는 모든 객체가 동작합니다 — 다만 이 패키지의 문서와 예제는 LangGraph 사용 사례에 초점을 맞추고 있습니다.

## 이 패키지가 하지 않는 것

이 패키지는 다음을 담당하지 않습니다:
- OpenAPI 문서 생성 또는 Swagger UI — [`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python) 사용
- LangGraph 계약 외의 요청/응답 검증 — [`azure-functions-validation-python`](https://github.com/yeongseon/azure-functions-validation-python) 사용
- LangGraph 외의 범용 그래프 서빙 추상화

> **참고:** OpenAPI 스펙 생성은 전용 [`azure-functions-openapi-python`](https://github.com/yeongseon/azure-functions-openapi-python) 패키지와 브리지 모듈(`azure_functions_langgraph.openapi.register_with_openapi`)을 사용하세요.

## 설치

```bash
pip install azure-functions-langgraph-python
```

Azure 서비스를 이용한 영구 스토리지:

```bash
# Azure Blob Storage 체크포인터
pip install azure-functions-langgraph-python[azure-blob]

# Azure Table Storage 스레드 스토어
pip install azure-functions-langgraph-python[azure-table]

# 둘 다
pip install azure-functions-langgraph-python[azure-blob,azure-table]
```

Azure Functions 앱에 다음 의존성도 포함해야 합니다:

```text
azure-functions
langgraph
azure-functions-langgraph-python
```

로컬 개발 설치:

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
cd azure-functions-langgraph-python
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

### 프로덕션 인증

`LangGraphApp`은 로컬 개발 편의를 위해 기본적으로 `AuthLevel.ANONYMOUS`를 사용합니다.
프로덕션 배포 시에는 `FUNCTION` 또는 `ADMIN` 인증을 사용하고 Azure Functions 키를 전송하세요.

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
```

### 스트리밍 동작 방식

> **중요:** 모든 `/stream` 엔드포인트(네이티브 `POST /api/graphs/{name}/stream`,
> Platform 호환 `POST /threads/{id}/runs/stream`, `POST /runs/stream`)는
> **버퍼링된 SSE**를 반환합니다. 그래프가 emit하는 청크는 실행 중에 수집되어
> 실행이 **완료된 후**에 한꺼번에 SSE 이벤트로 전송됩니다. 즉, 진정한 토큰 단위
> 스트리밍이 아니며, 클라이언트는 부분 토큰을 점진적으로 받지 못합니다.
>
> 진정한 청크 스트리밍은 로드맵에 있으며 Azure Functions Python v2의 streaming
> response 지원에 의존합니다. 실시간 토큰 스트리밍이 필요한 경우, 장시간 실행
> 호스트(예: App Service 또는 AKS)에서 그래프를 실행하는 것을 권장합니다.

### 그래프별 인증

앱 수준 인증 설정을 그래프별로 재정의할 수 있습니다:

```python
# 그래프별 인증 재정의
app.register(graph=public_graph, name="public", auth_level=func.AuthLevel.ANONYMOUS)
app.register(graph=private_graph, name="private", auth_level=func.AuthLevel.FUNCTION)
```

Function 키를 사용한 요청 예시:

```bash
curl -X POST "https://<app>.azurewebsites.net/api/graphs/echo_agent/invoke?code=<FUNCTION_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "Hello!"}]}}'
```

### 생성되는 엔드포인트

1. `POST /api/graphs/echo_agent/invoke` — 에이전트 호출
2. `POST /api/graphs/echo_agent/stream` — 에이전트 응답 스트리밍 (버퍼링된 SSE, 진정한 토큰 스트리밍 아님)
3. `GET /api/graphs/echo_agent/threads/{thread_id}/state` — 스레드 상태 조회
4. `GET /api/health` — 헬스 체크

`platform_compat=True` 설정 시 SDK 호환 엔드포인트도 생성됩니다:

6. `POST /assistants/search` — 등록된 어시스턴트 목록
7. `GET /assistants/{id}` — 어시스턴트 상세 정보
8. `POST /assistants/count` — 어시스턴트 수
9. `POST /threads` — 스레드 생성
10. `GET /threads/{id}` — 스레드 조회
11. `PATCH /threads/{id}` — 스레드 메타데이터 수정
12. `DELETE /threads/{id}` — 스레드 삭제
13. `POST /threads/search` — 스레드 검색
14. `POST /threads/count` — 스레드 수
15. `POST /threads/{id}/runs/wait` — 실행 후 결과 대기
16. `POST /threads/{id}/runs/stream` — 실행 후 결과 스트리밍 (버퍼링된 SSE)
17. `POST /runs/wait` — 스레드리스 실행
18. `POST /runs/stream` — 스레드리스 스트리밍 (버퍼링된 SSE)
19. `GET /threads/{id}/state` — 스레드 상태 조회
20. `POST /threads/{id}/state` — 스레드 상태 수정
21. `POST /threads/{id}/history` — 상태 히스토리

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

### 영구 스토리지 (v0.4+)

체크포인트 영구 저장을 위한 Azure Blob Storage와 스레드 메타데이터를 위한 Azure Table Storage를 사용합니다:

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


# Azure Blob 체크포인터로 그래프 빌드
container_client = ContainerClient.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", "checkpoints"
)
saver = AzureBlobCheckpointSaver(container_client=container_client)

builder = StateGraph(AgentState)
builder.add_node("chat", chat)
builder.add_edge(START, "chat")
builder.add_edge("chat", END)
graph = builder.compile(checkpointer=saver)

# Azure Table 스레드 스토어로 배포
thread_store = AzureTableThreadStore.from_connection_string(
    "DefaultEndpointsProtocol=https;AccountName=...", table_name="threads"
)

app = LangGraphApp(platform_compat=True)
app.thread_store = thread_store
app.register(graph=graph, name="echo_agent")
func_app = app.function_app
```

체크포인트와 스레드 메타데이터는 Azure Functions 재시작 후에도 유지되며 인스턴스 간 확장됩니다.

### v0.3.0에서 업그레이드

v0.4.0은 v0.3.0과 완전히 하위 호환됩니다. 브레이킹 체인지가 없습니다.

- **새로운 선택적 extras**: `pip install azure-functions-langgraph-python[azure-blob,azure-table]`로 영구 스토리지
- **새로운 플랫폼 엔드포인트**: 스레드 CRUD, 상태 수정/히스토리, 스레드리스 실행, 어시스턴트 카운트
- **새로운 프로토콜**: `UpdatableStateGraph`, `StateHistoryGraph` (`azure_functions_langgraph.protocols`에서 사용 가능)

## 언제 사용하면 좋은가

- LangGraph 에이전트를 Azure Functions에 배포하고 싶을 때
- LangGraph Platform 비용 없이 서버리스 배포가 필요할 때
- 컴파일된 그래프에 대한 HTTP 엔드포인트를 최소한의 설정으로 필요할 때
- LangGraph 체크포인터를 통한 스레드 기반 대화 상태가 필요할 때
- Azure Blob/Table Storage를 통한 영구 상태 저장이 필요할 때

## 문서

- 프로젝트 문서: `docs/`
- 예제: `examples/`
- 제품 요구사항: `PRD.md`
- 설계 원칙: `DESIGN.md`

## 에코시스템

이 패키지는 **Azure Functions Python DX Toolkit**의 일부입니다.

**설계 원칙:** `azure-functions-langgraph-python`는 LangGraph 런타임 노출을 담당합니다. `azure-functions-validation-python`은 검증을 담당합니다. `azure-functions-openapi-python`는 API 문서화를 담당합니다.

| 패키지 | 역할 |
|--------|------|
| **azure-functions-langgraph-python** | Azure Functions용 LangGraph 배포 어댑터 |
| [azure-functions-validation-python](https://github.com/yeongseon/azure-functions-validation-python) | 요청/응답 검증 및 직렬화 |
| [azure-functions-openapi-python](https://github.com/yeongseon/azure-functions-openapi-python) | OpenAPI 스펙 생성 및 Swagger UI |
| [azure-functions-logging-python](https://github.com/yeongseon/azure-functions-logging-python) | 구조화된 로깅 및 관측성 |
| [azure-functions-doctor-python](https://github.com/yeongseon/azure-functions-doctor-python) | 배포 전 진단 CLI |
| [azure-functions-scaffold-python](https://github.com/yeongseon/azure-functions-scaffold-python) | 프로젝트 스캐폴딩 |
| [azure-functions-durable-graph-python](https://github.com/yeongseon/azure-functions-durable-graph-python) | Durable Functions 기반 매니페스트 그래프 런타임 |
| [azure-functions-cookbook-python](https://github.com/yeongseon/azure-functions-cookbook-python) | 레시피 및 예제 |

## 면책 조항

이 프로젝트는 독립적인 커뮤니티 프로젝트이며, Microsoft 또는 LangChain과 제휴, 보증, 유지보수 관계에 있지 않습니다.

Azure 및 Azure Functions는 Microsoft Corporation의 상표입니다.
LangGraph 및 LangChain은 LangChain, Inc.의 상표입니다.

## 라이선스

MIT
