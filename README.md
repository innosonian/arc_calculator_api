# ARC Calculator API

앱이 누적한 실제 CPR/AED 바이너리를 내부 계산기로 처리하고 점수·통계·코칭·차트를 제공하는 백엔드다. 계산 성공, 프로그램 완료, 공유 진도 반영, ARC 제출 상태를 각각 관리한다.

## 현재 상태와 최근 변경

- **새 과정 API `/api/v2`: 로컬 내부 구현·인수 PASS.** 배정 과정 → 영상·문서·훈련·평가 항목 → TrainingProgram → 결과·진도를 제공한다. `build_course_application`과 명시적인 공급자 조립이 필요하다.
- **기본 로컬 서버는 `/mock/v1`.** 새 API가 자동 활성화되거나 AWS에 배포된 상태가 아니다. 앱 전환 시 사용할 서버 주소·버전은 별도로 확인한다.
- 최근 고도화는 응답 자료형·상태 일치, 콘텐츠 완료, 배정/정의 변경, 동시 시작·초기화, 파일 영속성 및 재시작 복구를 보완했다. 마지막 평가는 앞선 항목 완료 후 시작하고 합격 후 재응시를 막는다.
- 전체 회귀 **3251개 + 하위 시험 9개 통과** 및 새 AI 역할 5인의 담당 범위 재심 PASS. 실행 범위와 한계는 [검증 요약](docs/VALIDATION.md)에 기록했다.
- 실물 앱·마네킨, AWS, 공식 ARC/MuleSoft 인수는 별도다. CPR 완료 규칙은 `pending_policy`, 실제 ARC 제출은 비활성이다. 퀴즈는 범위에서 제외한다.

## 앱 개발자에게 전달할 파일

1. [앱 API 명세 — Markdown](docs/APP_API.md): `/api/v2`의 호출 순서, 요청·응답, 자료형·필수값·null, JSON 예시, 오류·재시도와 기존 앱 이행을 한 파일로 제공한다.
2. [계산 입력·결과 및 기존 API 계약](docs/ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md): 바이너리/폼, 패킷 기록 규칙, 계산 JSON과 기존 `/mock/v1` 상세.

## 유지하는 문서

| 문서 | 목적 |
|---|---|
| [AGENTS](AGENTS.md) | 다음 작업의 보존·검증·Git 지침 |
| [DECISIONS](docs/DECISIONS.md) | 사용자 확정 정책과 남은 외부 확인 사항 |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | 현재 모듈, 상태·저장·보안 경계와 변경 규칙 |
| [LOCAL_RUN](docs/LOCAL_RUN.md) | 기본 로컬 서버·기기 연결·분리된 전체 검증 |
| [DEPLOY_GUIDE](docs/DEPLOY_GUIDE.md) | AWS 자원 확인·설정 검사·배포·복구 준비 |
| [VALIDATION](docs/VALIDATION.md) | 최신 검증 결과·AI 검토 범위·미검증 사항 |

이 README와 위 앱 문서 2개를 포함해 핵심 Markdown은 9개다. 앱 전달 명세는 Markdown으로 유지한다. 코드·시험이 사용하는 manifest/fixture는 별도 계약 자료다. 과거 설계 초안·중복 배포 문서·작업 사본·일회성 분석기·빌드 산출물·캐시는 2026-09-18 정리했다.

## 기본 로컬 실행

```sh
var/local-python/bin/python scripts/serve_local.py
```

준비된 Python 환경·Java/Javac·검증된 DynamoDB Local이 필요하다. `http://127.0.0.1:8000/healthz`로 상태를 확인한다. Dummy 로그인은 `test@test.com` / 문자열 `2222`다. 이 실행은 `/mock/v1`이며, 자세한 조건은 [로컬 안내](docs/LOCAL_RUN.md)를 따른다.

사용자 DB·키·비공개 입력/결과·실행환경은 보존하며 Git/배포물에서 제외한다. 운용 로그는 정제된 주요 이력과 진단을 기록하고, 로그 장애가 훈련을 막지 않게 한다. 보관기간 확정 전 자동 삭제는 없다. commit·push·실제 배포는 사용자가 결정한다.
