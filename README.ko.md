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

이 문서의 언어: **한국어** | [日本語](README.ja.md) | [简体中文](README.zh-CN.md) | [English](README.md)

> **알파 버전 안내** — 이 패키지는 활발히 개발 중입니다. `pyproject.toml`의 `Development Status :: 3 - Alpha` 분류가 진실의 원철입니다. v1.0 이전에는 minor 버전 간 변경될 수 있습니다. GitHub에서 이슈를 보고해 주세요.

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

| 기능 | LangGraph Platform | azure-functions-langgraph |
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

> 기능별 SDK 지원 매트릭스(`501 Not Implemented`로 거부되는 `RunCreate` 필드, 스레드 필터, SDK 호출 포함)는 [COMPATIBILITY.md](COMPATIBILITY.md)를 참고하세요.

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

## 아키텍처 다이어그램

시퀀스/상태 다이어그램(Invoke·버퍼링 Stream 및 오버플로 경로, 스레드 생명주기 상태 머신, Blob 리스 생명주기)은 정식 영문 문서에서 렌더링됩니다:

- [Architecture](docs/architecture.md) — 모듈 플로우차트, Invoke/Stream 시퀀스(오버플로 포함), 스레드 생명주기 상태 머신
- [Production Guide](docs/production-guide.md#distributed-thread-locking) — 스레드 생명주기 및 분산 리스 생명주기 다이어그램


## 설치

```bash
pip install azure-functions-langgraph
```

Azure 서비스를 이용한 영구 스토리지:

```bash
# Azure Blob Storage 체크포인터
pip install azure-functions-langgraph[azure-blob]

# Azure Table Storage 스레드 스토어
pip install azure-functions-langgraph[azure-table]

# 둘 다
pip install azure-functions-langgraph[azure-blob,azure-table]
```

Azure Functions 앱에 다음 의존성도 포함해야 합니다:

```text
azure-functions
langgraph
azure-functions-langgraph
```

로컬 개발 설치:

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
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

### 프로덕션 인증

`LangGraphApp`은 배포된 엔드포인트에 함수 키가 요구되도록 기본으로 `AuthLevel.FUNCTION`을 사용합니다.
로컬 개발 등 키 없이 엔드포인트에 접근하려면 `auth_level=func.AuthLevel.ANONYMOUS`를 명시적으로 전달하세요. 그러면 실수로 공개 배포되는 것을 방지하기 위해 무조건 `UserWarning`이 명시적으로 발생합니다.

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

# 프로덕션 기본값: 함수 키 인증 요구
app = LangGraphApp()  # LangGraphApp(auth_level=func.AuthLevel.FUNCTION)과 동일

# 로컬 개발 전용: 명시적으로 익명 접근 허용 — UserWarning 발생
app_local = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
```

> **참고:** `health_auth_level`은 `auth_level`과 무관하게 기본값이 `ANONYMOUS`입니다.
> 따라서 `auth_level=FUNCTION`으로 설정해도 health 엔드포인트는 여전히 공개적으로 접근 가능합니다.
> health 엔드포인트에도 function key를 요구하려면 `health_auth_level=func.AuthLevel.FUNCTION`을
> 명시적으로 설정하세요.

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

### 커스텀 라우트 프리픽스

모든 라우트는 Azure Functions 기본값인 `/api` 프리픽스를 사용합니다. 프리픽스를 변경하려면 `host.json`의 `routePrefix`를 수정하세요:

```json
{
  "extensions": {
    "http": {
      "routePrefix": "v1"
    }
  }
}
```

이는 모든 라우트(예: `POST /v1/graphs/{name}/invoke`)를 변경합니다. 프리픽스를 제거하려면 `routePrefix`를 `""`로 설정하세요.

> **중요 — `LangGraphApp(route_prefix=...)`는 메타데이터 전용입니다(metadata-only).** Azure Functions는 HTTP 라우트를 `host.json`(**진실의 원천**)에서 결정하며, 생성자 인수는 해당하지 않습니다. `route_prefix` 인수는 `azure-functions-openapi-python` 브리지가 사용하는 메타데이터 스냅샷에만 기록되어 생성된 스펙이 배포된 라우트를 반영하도록 도웁니다. `host.json`을 함께 수정하지 않으면 요청이 제공되는 위치는 **바뀌지 않습니다**. 라우트를 실제로 이동하려면 항상 `host.json`을 수정하고, 메타데이터 일관성을 위해 `LangGraphApp(route_prefix=...)`에 동일한 값을 전달하세요.

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

### 스케일 가이드

번들된 영구 저장 백엔드는 개발 및 중소 규모 프로덕션 배포를 위한 것입니다. 다음 한계를 넘기 전에 계획을 세우세요:

| 백엔드 | 권장 범위 | 주의 구간 | 백엔드 교체 권장 |
|---|---|---|---|
| `AzureBlobCheckpointSaver` | 스레드당 체크포인트 < 100개, 스레드 < 10K개 | 스레드당 체크포인트 100–1000개 | Cosmos DB 또는 Redis 기반 체크포인터 사용 |
| `AzureTableThreadStore` | 스레드 < 100K개, 가벼운 검색 부하 | 스레드 100K–500K개 | 샤딩된 스레드 스토어 또는 Cosmos DB 사용 |

참고 사항:

- **단일 파티션** — `AzureTableThreadStore`는 모든 스레드를 단일 PartitionKey에 저장하며, Azure Table 파티션당 처리량(Standard 계정 기준 약 2000 엔티티/초)에 의해 제한됩니다. `status` 외의 검색 및 카운트 필터링은 **클라이언트 사이드**에서 수행됩니다. [COMPATIBILITY.md](COMPATIBILITY.md) 참고.
- **Prefix 스캔** — `AzureBlobCheckpointSaver`는 blob prefix 스캔으로 체크포인트를 나열하므로 트랜잭션 수와 지연이 스레드당 체크포인트 수에 비례하여 증가합니다. 아래 보존 헬퍼로 이를 제한하세요.
- **엔티티 크기** — Azure Table 엔티티는 1 MB로 제한되며, 임계값의 90%에서 경고가 기록됩니다.
- **스테일 락 정리 주의** — `AzureTableThreadStore.reset_stale_locks`는 프로젝션 쿼리(`select=["RowKey", "updated_at"]`)를 사용하며, `entity.metadata["etag"]` 또는 `entity["etag"]` 중 하나로도 ETag가 노출되어야 합니다. 두 형태 모두에서 ETag가 없는 행은 CAS용 ETag 없이 스테일 락을 재설정하지 않도록 건너뛰어(DEBUG 로그) 다음 스캔에서 다시 시도됩니다.
- **Cosmos DB Managed Identity 미지원** — 업스트림 `langgraph-checkpoint-cosmosdb` 패키지가 `TokenCredential`을 지원하지 않으므로 `create_cosmos_checkpointer`는 **키 기반 인증만** 사용합니다. 헬퍼는 생성자 인자로부터 `COSMOSDB_ENDPOINT` / `COSMOSDB_KEY` 환경 변수를 일시적으로 설정한 뒤 원복합니다. Cosmos DB에 패스워드리스 인증이 필수라면 업스트림이 `TokenCredential`을 추가할 때까지 다른 체크포인터 백엔드를 사용하세요.

#### 네이티브 엔드포인트 스레드 락

네이티브 invoke/stream 엔드포인트(`POST /api/graphs/{name}/invoke` 및 `.../stream`)는 그래프에 체크포인터가 있고 요청에 `config.configurable.thread_id`가 포함된 경우 **인프로세스 스레드별 락**을 사용합니다. 이는 동일 Python 워커 프로세스 내에서 단일 작성자 체크포인터(예: `AzureBlobCheckpointSaver`)에 대한 동시 쓰기를 방지합니다.

> **중요:** 이는 **분산 락이 아닙니다**(not distributed). 여러 Function App 인스턴스, 워커 프로세스 또는 호스트 간에는 조율되지 않습니다. 분산 런 락이 필요하다면 `AzureTableThreadStore`와 함께 Platform 호환 런(`platform_compat=True`)을 사용하세요 — ETag 기반의 원자적 락을 제공합니다.

#### 보존 헬퍼

`AzureBlobCheckpointSaver`는 정기 정리(예: Timer 트리거 Function)를 위한 두 가지 헬퍼를 제공합니다:

```python
# (스레드, 네임스페이스)별로 최근 50개의 체크포인트만 유지
saver.delete_old_checkpoints(thread_id="conversation-1", keep_last=50)

# 또는 특정 체크포인트 id 이전의 모든 체크포인트 삭제
saver.delete_checkpoints_before(
    thread_id="conversation-1",
    before_checkpoint_id="01HXY...",
)
```

두 헬퍼 모두 체크포인트 마커, 메타데이터, write blob만 삭제합니다. 채널 값 blob(`values/` 아래)과 `latest.json` 포인터는 의도적으로 보존하므로, 유지되는 체크포인트는 그대로 사용 가능합니다.

> **참고** — `delete_old_checkpoints` / `delete_checkpoints_before`는 안전하지만 **완전하지는 않습니다**. 방금 삭제된 체크포인트에서**만** 참조되던 채널 값 blob은 orphan 상태가 되며 제거되지 않습니다. 자주 체크포인트되는 장기 실행 스레드의 경우, 시간이 지나면 이러한 orphan blob이 스토리지 사용량의 대부분을 차지할 수 있습니다. 두 번째 단계로 `collect_orphaned_values()`(아래)를 스케줄에 따라 실행하세요.

#### 고아 채널 값 가비지 컬렉션

체크포인트 정리 후 `collect_orphaned_values()`는 살아남은 체크포인트를 순회하며 참조되는 `(channel, version)` 집합을 만들고, 그 집합 밖의 `values/` blob을 삭제합니다. 기본값은 **dry-run**이므로 먼저 감사할 수 있습니다:

```python
audit = saver.collect_orphaned_values(thread_id="conversation-1")
print(audit.would_delete)

result = saver.collect_orphaned_values(thread_id="conversation-1", dry_run=False)
print(f"고아 blob {len(result.deleted)}개 삭제됨")
```

이 헬퍼는 두 가지 보완적인 메커니즘으로 동시성 안전성을 제공합니다: (1) 최근 쓰기 grace period — `last_modified`가 `grace_period_seconds`(기본 **300초**) 이내인 값 blob은 다음 GC 패스로 연기되며 `result.skipped_recent`에 기록됩니다(value blob 업로드와 `latest.json` 확정 사이의 갭을 보호). (2) orphan별 재스캔 — 각 삭제 직전에 survivor 집합을 다시 계산하므로, 스냅샷 이후에 새로 확정된 체크포인트가 참조하기 시작한 *오래된* value blob도 보존됩니다.

네임스페이스별로 **fail-closed** 동작합니다: `latest.json`이 없거나 살아남은 체크포인트 blob 중 하나라도 읽을 수 없거나 역직렬화에 실패하면, 해당 네임스페이스 전체가 건너뛰어지고(`result.skipped_namespaces`에 기록) 잘못 구성되었거나 일시적으로 사용할 수 없는 스토리지에서 파괴적인 삭제가 트리거되지 않도록 합니다.

### v0.3.0에서 업그레이드

v0.4.0은 v0.3.0과 완전히 하위 호환됩니다. 브레이킹 체인지가 없습니다.

- **새로운 선택적 extras**: `pip install azure-functions-langgraph[azure-blob,azure-table]`로 영구 스토리지
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

**설계 원칙:** `azure-functions-langgraph`는 LangGraph 런타임 노출을 담당합니다. `azure-functions-validation-python`은 검증을 담당합니다. `azure-functions-openapi-python`는 API 문서화를 담당합니다.

| 패키지 | 역할 |
|--------|------|
| **azure-functions-langgraph** | Azure Functions용 LangGraph 배포 어댑터 |
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
