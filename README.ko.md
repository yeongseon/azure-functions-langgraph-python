# Azure Functions LangGraph

기존 LangGraph 그래프를 Azure Functions Python v2 HTTP 엔드포인트로 배포하는 어댑터입니다.
프롬프트·모델·도구를 만드는 에이전트 프레임워크가 아닙니다.

> 이 번역은 커뮤니티가 관리하며 최신 [English README](README.md)보다 뒤처질 수 있습니다. 영어 원문이 기준입니다.

## 설치와 시작

Python 3.11–3.14: `pip install azure-functions-langgraph`.
[5분 튜토리얼](docs/tutorial-5-min.md)의 앱을 실행한 뒤
[사용법](docs/usage.md)과 [설정](docs/configuration.md)을 확인하세요.

## 안정성

롤링 0.x이며 패키지 분류는 Beta입니다. 코어 HTTP 계약은 Stable,
체크포인트·잠금·async·관측 계약은 Beta입니다. Platform 호환, 실제 HTTP
스트리밍, Service Bus, Durable은 **Experimental**이며 minor 릴리스 사이에도 바뀔 수 있습니다.
기본 스트림은 버퍼링된 SSE이며 토큰 단위 실시간 전송이 아닙니다.
함수 키는 사용자 소유권 검증이 아니며 기본 잠금과 스레드 저장소는 프로세스 내부에만 존재합니다.
[프로덕션/분산 가이드](docs/production-guide.md#distributed-deployment)와 [보안](docs/security.md)을 먼저 읽으세요.

## 학습 경로

1. [Azure OpenAI](examples/azure_openai_agent/)
2. [대화 메모리](examples/conversation_memory/)
3. [도구 호출](examples/tool_calling_agent/)
4. [영구 저장소](examples/production_persistent_agent/)

예제는 개별 기능을 보여주는 독립 앱이며 클라우드 자격 증명 없이 fake 모델로 테스트할 수 있습니다.
[상세 참조와 마이그레이션](docs/adapter-reference.md), [SDK 지원 범위](COMPATIBILITY.md),
[기여 안내](CONTRIBUTING.md). 독립 커뮤니티 프로젝트이며 Microsoft/LangChain 공식 제품이 아닙니다. [MIT](LICENSE).
