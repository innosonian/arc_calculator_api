# ARC Calculator API

앱이 누적한 실제 CPR/AED 바이너리를 내부 계산기로 처리하고 점수·통계·코칭·차트를 제공하는 백엔드다. 계산 성공, 프로그램 완료, 공유 진도 반영, ARC 제출 상태를 각각 관리한다.

## 현재 상태와 최근 변경

- **새 과정 API `/api/v2`: 로컬 내부 구현·인수 PASS. AWS Dev 첫 압박 계산·훈련 완료·진도 반영과 저장 원본·운용 로그 확인.** AWS API/Worker 설정의 `course_v2_dummy` 절(필수)로 기존 5개 프로그램×3연령의 임시 과정 15개를 실제 바이너리로 계산한다. 2026-09-23 사용자 제공 출력으로 HTTPS 로그인/조회와 기존 압박 파일 한 건의 multipart 접수→계산 성공(101회·100점)→일반 훈련 완료/진도 반영, S3 원본·최종 결과 일치 및 접수/계산 시작/완료 기록을 확인했다. 실제 앱 파일이 없어 기존 저장소 자료로 시험했다(D99). 서명 차트 다운로드(101점·저장 hash 일치)와 서명 없는 접근403도 확인했다. 같은 파일의 최종평가도 101회·100점으로 통과했고 두 항목 완료/통과 및 과정 `FINISHED`를 확인했다. 2026-09-24에는 같은 계정의 다른 유효 세션에서 기존 훈련·결과·차트 발급이 모두404로 차단되고 원래 결과가 보존됨을 확인했다. 서명 차트는 새 링크200→실제300초 경과 후 같은 링크의 만료403→재발급200·같은 차트 확인도 통과했다. 완료한 일반 훈련의 같은 시작·파일 요청을 순서대로 재전송해 각각200으로 기존 응답을 받고 훈련·작업·접수 기록·결과가 보존됨도 확인했다. 이 범위의 기본 Dev 시험은 통과해 앱 연결 시험을 시작할 수 있다. 동시/처리 중 재전송·세션 만료/복구·장애·실물 앱·용량/비용 등을 포함한 전체 AWS 인수는 진행 중이다.
- **AWS Dev 초기 경보8개·시험 이메일·세 함수의 로그 수집 흔적 확인.** 사용자 출력으로 설정 검증과 시험 후8개 모두 OK·알림 동작 활성을 확인했고 후속 회신으로 시험 메일 도착을 확인했다. 2026-09-24에는 API/Worker/Relay의 CloudWatch 스트림별 이벤트·수집 시각도 확인했다. 실제 장애 탐지·로그 본문 정제·무누락까지 검증한 것은 아니다.
- **`/api/v2`가 최종 버전이다.** `/mock/v1`과 `/cpr-analysis`는 삭제되어 로컬·AWS 모든 서버에서 404다(D103). 기본 로컬 실행도 AWS Dev와 같은 임시 과정 15개의 `/api/v2`다. 앱 전환 시 사용할 서버 주소는 별도로 확인한다.
- **2026-09-28 코드 리뷰 후속 결정(D102~D121).** `/mock/v1` 삭제, 결과가 같다는 증거를 갖춘 계산 파일 해시 재등록, 계산 재시작 5회 한도(넘으면 기존 `CALCULATION_FAILED`), 과정 설정 하한·업로드 크기 관계 검사, `/api/v2` 거절 로그와 `http_request_id`, Relay 처리량 개선, PR CI의 DynamoDB Local 통합 시험, 보조 Lambda 삭제, Sentry 미사용 등을 정했다. 같은 날 이 결정 구현, `/mock/v1` 삭제, 결과가 바뀌지 않는 구조 정리(동작 보존 골든으로 대조)를 마쳤다. 로컬·CI 오프라인 시험만 수행했고 이 코드는 AWS에 배포하지 않았다. 앱에 보이는 영향은 [앱 API 명세](docs/APP_API.md), 결정은 [DECISIONS](docs/DECISIONS.md#2026-09-28-코드-리뷰-후속-결정), 작업·검증 범위는 [검증 요약](docs/VALIDATION.md#2026-09-28-코드-리뷰-후속-작업)에 있다.
- **Dev 자동 배포(D137, 2026-09-30).** `develop`에 머지되면 `.github/workflows/deploy_dev.yml`이 같은 revision의 오프라인 회귀 → x86_64 ZIP 빌드·30일 보관 → GitHub Environment `development` 승인 → OIDC 역할 위임 → `ARC_JOURNEY_CONFIG`와 코드 레지스트리 대조(`CONFIG_DRIFT`면 중단) → Worker→Relay→API 코드 갱신·`CodeSha256` 대조 → Dummy 로그인·과정 목록 스모크(로그아웃 없음) 순으로 Dev 세 Lambda의 코드만 배포한다. 설정 JSON은 자동 적용하지 않고 되돌리기는 `rollback_run_id`로 한다. 준비물(OIDC 공급자·역할·Environment·secret/variable·브랜치 보호)과 절차는 [배포 안내 9절](docs/DEPLOY_GUIDE.md)에 있다. 정적 검사·오프라인 시험만 했고 GitHub Actions·AWS 실행은 아직 없다.
- 최근 고도화는 응답 자료형·상태 일치, 콘텐츠 완료, 배정/정의 변경, 동시 시작·초기화, 파일 영속성 및 재시작 복구를 보완했다. 마지막 평가는 앞선 항목 완료 후 시작하고 합격 후 재응시를 막는다.
- 최신 전체 회귀 수는 [검증 요약](docs/VALIDATION.md)을 따른다. AWS 준비 검토와 과거 AI 5인 인수의 범위·한계도 그곳에 구별해 기록했다.
- 실물 앱·마네킨, AWS, 공식 ARC/MuleSoft 인수는 별도다. CPR 완료 규칙은 D136(사이클 3/8/10)으로 확정했고, 실제 ARC 제출은 비활성이다. 퀴즈는 범위에서 제외한다.
- 2026-09-22 배포 검토는 [검증 요약](docs/VALIDATION.md), 처음 AWS를 사용하는 사람의 순서는 [배포 안내](docs/DEPLOY_GUIDE.md#beginner-dev)에서 확인한다. 이번 Dev는 사용자 선택에 따라 공용 Dummy 계정을 사용한다.

## 앱 개발자에게 전달할 파일

1. [앱 API 명세 — Markdown](docs/APP_API.md): `/api/v2`의 호출 순서, 요청·응답, 자료형·필수값·null, JSON 예시, 오류·재시도와 기존 앱 이행을 한 파일로 제공한다.
2. [계산 입력·결과 상세 계약](docs/ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md): 바이너리/폼, 패킷 기록 규칙, 계산 JSON 상세. 삭제된 `/mock/v1` 설명은 이력 한 절로 줄였다.

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

준비된 Python 환경·Java/Javac·검증된 DynamoDB Local이 필요하다. `http://127.0.0.1:8000/healthz`로 상태를 확인한다(`mode: "course_v2"`). 이 실행은 `/api/v2`이며 Dummy 로그인(`test@test.com` / 문자열 `2222`) 뒤 AWS Dev와 같은 임시 과정 15개가 보인다. `--course-v2`는 더 이상 필요 없고 `--control-only`는 제거됐다. 예전 데이터 폴더에서 과정이 대기로 보이는 경우 등 자세한 조건은 [로컬 안내](docs/LOCAL_RUN.md)를 따른다.

사용자 DB·키·비공개 입력/결과·실행환경은 보존하며 Git/배포물에서 제외한다. 운용 로그는 정제된 주요 이력과 진단을 기록하고, 로그 장애가 훈련을 막지 않게 한다. 보관기간 확정 전 자동 삭제는 없다. commit·push·실제 배포는 사용자가 결정한다.
