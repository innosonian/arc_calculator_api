# 검증 상태와 재현 방법

기준: **2026-09-18 VCC 고도화 후 로컬 내부 인수 PASS**. 불필요 파일 정리 후 전체 재실행도 **3251 passed, 9 subtests passed**, exit 0이다. 구현에 참여하지 않은 새 AI 페르소나 5인의 담당 범위 재심도 PASS이며 검토 범위의 미해결 확정 P1/P2는 없다.

이는 사람의 인증·실물 앱 인수·AWS 운영 승인·ARC 공식 승인이 아니다. 기본 서버는 `/mock/v1`, 새 과정 모델은 명시 조립한 `course_v2`/`/api/v2`다. 실제 외부 송신은 비활성이고 기본 서버 전환·commit·push·배포는 수행하지 않았다.

현재 구조는 [ARCHITECTURE](ARCHITECTURE.md), 확정 정책과 미정 계약은 [DECISIONS](DECISIONS.md), 앱 연결 계약은 [APP_API](APP_API.md), 실행 환경 준비는 [LOCAL_RUN](LOCAL_RUN.md)을 따른다. 이 문서는 현재 재현 가능한 시험과 최신 검토 결과만 유지한다.

<a id="14-vcc-고도화와-신규-독립-재심--2026-09-18"></a>

## 1. 최신 전체 실행

| 항목 | 결과 |
|---|---|
| 전체 시험 | 정리 후 **3251 passed, 9 subtests passed**, 231.56초, exit 0 |
| 경고 | 12463건. 기존 botocore의 `datetime.utcnow` deprecation이며 시험 실패가 아님 |
| 수집 범위 | `tests`, `scripts`, `local_server_tests`, `integration_tests`, `http_pipeline_tests`, `transport_integration_tests` |
| 실행 자원 | 새 임시 설치·DB·테이블·파일 경로·독립 loopback 포트. 자신이 시작한 자원만 정리 |
| 보호 대상 | 기존 사용자 서버·DB·키·원본 유지. 직전 고도화에서 내부 계산기·projection·골든 fixture를 시험 통과 목적으로 변경하지 않음 |
| 정적 확인 | 구조·검증 문서의 diff 공백 검사 통과. 정리 및 API 명세 검증은 7절 |

집중 재실행과 검토자별 시험 수는 겹치므로 위 전체 수에 더하지 않는다. 고도화 완료 당시에는 같은 3251개·하위 9개가 238.25초에 통과했고, 위 표는 파일 정리 후 다시 수행한 별도 실행이다. 중간 전체 실행 3221개 통과는 후속 수정 전 결과였으며 최종 수로 사용하지 않는다.

과거 단계별 로그·원본 사본·개인 리뷰·병렬 작업 프롬프트는 사용자 정리 요청으로 활성 문서에서 제거했다. 삭제된 로컬 실행 기록을 다른 개발자의 필수 근거로 요구하지 않는다. 아래 정규 시험과 실행 명령으로 다시 확인한다.

## 2. 실제로 검증한 경계

| 경계 | 정규 시험·확인 내용 |
|---|---|
| 기존 계산·자료형·null·코칭·ARC 최소량 | `tests/test_reference_parity_regression.py`, `test_result.py` 등. 독립 기대값/승인 예외와 비교하며 현재 결과를 정답으로 복제하지 않음 |
| 기존 로컬 전체 Journey | `local_server_tests/test_journey_matrix_live.py` 등. 실제 CLI·DB·별도 Worker·기록 바이너리·차트 HTTP의 5프로그램×3연령 |
| 공급자·중첩 응답·순서·반복 배치 | `tests/test_vcc_provider.py`, `test_vcc_app_contract_hardening.py` 등. exact type, bool/int/null, ID 대응, 허용 반복 배치와 상충 정의 |
| 앱 계산 상태·본문·차트 null | `tests/test_vcc_wiring.py`. 실제 binding/HTTP GET·POST에서 같은 호출의 202 상태/본문 유지, 검증된 no-chart 쌍 null 허용 |
| 전체 과정 성공·재응시 정책 | `http_pipeline_tests/test_vcc_course_lifecycle_http.py`. HTTP→실제 DB→기존 내부 계산기→GET으로 영상·문서·일반 훈련·최종 평가·FINISHED 연결 |
| 배정·정의·reset·refresh 경합 | `tests/test_vcc_state_races.py`, `integration_tests/test_vcc_state_races_dynamodb.py`. 판정과 commit 사이 상태 변경, 공개 ID 교환, source scope 교체, 같은 FINAL 동시 생성 |
| 완료 근거와 평가 교체 | `integration_tests/test_vcc_runtime_hardening.py` 등. 같은 버전의 자산/프로그램 교체·A→B→A 보류·원래 결과 보존 |
| 영속 과정 blob | `local_server_tests/test_vcc_course_storage.py`. 실제 private file adapter 재개방, hash/binding·손상 검증 |
| Worker/복구/원자 확정 | `integration_tests/test_vcc_runtime_hardening.py`. 계획 누락, 후보 저장 전후 crash, 확정 후보 유실, 위조/오래된 복구 증거, seal/reopen, unknown 반복 전달 |
| 실제 별도 프로세스 | `integration_tests/test_vcc_process_restart.py`. 부모 파일 저장소 종료 후 두 PID에서 API/Worker 재조립, 기존 작업·결과·파일 hash 보존 |
| 세션·소유권·취소·과거 보고 | `http_pipeline_tests/test_vcc_security.py`, `tests/test_vcc_state.py`. 유효한 다른 세션의 attempt/계산/차트 404, 잘못된 취소 400, 과거 epoch 행 불변 |
| 저장 한도와 멱등 replay | 상태·실제 DB 경합 시험. 긴 문서 근거가 START 한도를 넘으면 413·부분 쓰기 0·기존 receipt replay 유지 |
| 파일/차트·HTTP·로그 보안 | 기존 object/chart/transport/privacy/log 시험. 비공개 파일, 300초 서명, 입력 한도, 비밀 정제, 로그 장애의 훈련 비차단 |

HTTP 전체 과정의 학생은 시험 소유 DB에 만든 합성 배정 세션이다. 실제 ARC 로그인 성공을 뜻하지 않는다. Pass 사례도 합성 원본 바이너리를 기존 계산기가 처리하며 점수·기준을 대역으로 주입하지 않는다. 모든 단위 시험이 실제 CLI/DB를 사용하는 것은 아니므로 위 경계를 구분한다.

실제 프로세스 재시작 시험은 legacy queued/running/candidate 3건과 course candidate 1건을 처리했다. 첫 자식 계산 2회, 다음 자식 0회였고 후보 재계산은 0이었다. 4개 결과 hash·정의 hash·기존 파일 hash가 같았다. 검토자도 이 시험을 별도 임시 DB에서 독립 실행했다.

## 3. 수정한 핵심 결함

<a id="13-vcc-구현-적대적-검토--2026-09-18"></a>

앞선 구현 검토는 **FAIL**이었다. 기존 회귀 통과만으로 새 과정 흐름의 인수를 인정하지 않았으며, 아래 수정과 같은 반례의 재검증 후 현재 PASS로 변경했다. 과거 실패를 처음부터 통과한 것으로 소급하지 않는다.

| 문제 | 현재 수정과 보존 조건 |
|---|---|
| 조회 실패가 waiting으로 저장되지 않거나 목록만으로 ready 판단 | resolve 전 세대 예약, 저장 gate 전체 집계, GET 외부 조회 제거 |
| 중첩 타입·순서·반복 CourseItem·event.type 검증 불일치 | 공용 exact schema와 ID/순서 검증. 다른 배치 반복은 허용, 잘못된 입력은 정제 400/계약 오류 |
| 옛 배정·옛 판정에 새 revision만 붙여 commit | USER·inventory·HEAD·FINAL의 판정 snapshot을 거래 조건까지 유지. 충돌 시 전체 재판정 |
| refresh/report 경합이 최신 세대를 되돌림 | refresh 시작에서 generation과 HEAD revision 모두 증가 |
| reset 후 과거 보고가 잘못 적용되거나 검증 생략 | 원래 epoch 근거만 보존, kind/version/범위/한도 검증, 현재 진도 변경 0 |
| 과정 정의가 메모리에만 있거나 course 완료 계획 누락 | 기존 private storage의 immutable blob과 실제 API/Worker 동일 adapter. 불완전 course 조립은 거절 |
| 첫 ITEM 누락·여러 완료 표현 불일치 | 첫 훈련/평가 ITEM 생성과 ITEM/HEAD/완료 집합/FINAL의 원자 확정 |
| 접수 전 취소가 FINAL을 남기거나 새 역할을 해제 | 자기 참조 조건의 취소 거래만 역할 해제 |
| 복구 코드가 실제 경로와 분리되거나 증거를 신뢰만 함 | 실제 파일/DB reader, typed snapshot 재검증, live lease/fence·CAS, 후보 재개·확정 오류 seal |
| 평가 교체 뒤 옛 Pass 복제 | content identity 고정, 의미 기반 digest, 평가 보류 유지. A→B→A 자동 해제 금지 |
| HTTP 202 뒤 상태 재조회로 잘못된 200 결과 생성 | status/body를 같은 호출 결과로 사용. succeeded의 계산·평가·진도 필수 객체 검증 |
| no-chart의 null 조합 처리 오류 | 검증된 `{url:null, expiresAt:null}`만 허용. 한쪽만 null이면 오류 |
| unknown 원인이 TEMP로 덮여 재계산/합격 | `CALCULATION_OUTCOME_UNKNOWN` 보존. 같은 원래 call의 늦은 유효 후보만 재개 |
| 공개 link 재배정으로 다른 실행 정의가 결합 | source/kind/역할·binding 전체·7-key 정의 일치 검사, DEFINITION_CHANGED·쓰기 0 |
| 중간 assessment를 최종 평가로 취급 | 실제 마지막 배치만 final 역할, 중간 assessment 정상 시작 회귀 |
| 큰 문서 보고가 DynamoDB 한도에서 영구 503 | START canonical byte budget 검사 후 413. 보고 전체 거절·기존 receipt 보존 |
| 오류 삼키기·자기 비교·실제 경계 없는 시험 | 실제 legacy finalize, 단일 HTTP 오류 기대, 고정 독립 hash, 소유권/행 전체 비교/프로세스 회귀로 교체 |

<a id="12-vcc-구현-착수-심사와-합격-기준--2026-09-18"></a>

## 4. 독립 AI 5인 재심

구현 담당자를 이름만 바꿔 세지 않았다. 새 검토자는 제품 파일을 수정하지 않고 반례를 제출했고, 통합 담당 수정 후 같은 검토자가 재현·재심했다. 담당 범위 PASS는 실제 사람의 전문 자격 인증이나 운영 승인이 아니다.

| 역할 | 독립 공격·재심의 핵심 | 최종 범위 판정 |
|---|---|---|
| iOS Senior API integrator | 실제 상태/본문 경합·null 디코딩 반례. GET/POST 경계와 관련 82개 시험 | PASS |
| Python/DynamoDB 동시성·보안 Senior | generation 롤백·source scope·내용 교체·JSON 순서. 실제 임시 DB 재현과 관련 94개 단위 시험 | PASS |
| SRE·복구 Senior | unknown→TEMP→재계산 반례. 실제 DB 4회 전달에서 계산 1회, 늦은 유효 후보의 재계산 0회. 관련 62개 시험 | PASS |
| 시험·품질 Senior | 약한 assertion 5곳, 공개 ID 재배정·잘못된 실행 정의. 쓰기 0 및 중간 assessment 정상 시작, 관련 269개 시험 | PASS |
| CTO 구조·인수 | 중간 평가 역할·문서 저장 용량·프로세스 재시작 공백. 실제 DB 413/불변/replay와 독립 재시작 시험 | 로컬 내부 PASS |

상호 반박도 수행했다. 확정 오류 증거 저장 직후 crash의 영구 대기 우려는 정확한 입력·adapter·후보 부재·새 lease에 따른 안전한 동일 작업 재호출 경로로 반증했다. 동시성 검토자의 A→B→A 보류를 SRE가 교차 확인했고, 품질/CTO의 중간 assessment 반론은 역할 판정 수정과 정상 시작 시험으로 해소했다. 기본 Mock 유지와 공식 계약 미정 자체를 내부 코드 결함으로 세지 않았다.

## 5. 다시 실행하는 방법

Python 3.12·Java/Javac·검증된 DynamoDB Local 배포판을 사용한다. 별도 검증 환경에 `requirements-local.txt`와 `requirements-ci.txt`를 설치하는 절차는 [LOCAL_RUN](LOCAL_RUN.md)을 따른다. `requirements-dev.txt`만으로 전체 시험 의존성이 충족된다고 가정하지 않는다.

기본 `pytest.ini`는 `tests scripts`만 수집한다. 전체 인수는 아래 실행기를 사용해야 한다. 실행기는 새로운 DB·임시 경로·loopback 포트를 생성하고 사용자 실행 서버/DB를 공유하지 않는다.

```sh
STAGE=test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python scripts/validate_local_integration.py \
  --dynamodb-home var/dynamodb-local-3.3.1 --suite all
```

위 `python`과 DynamoDB 경로는 준비한 검증 환경에 맞춘다. 마지막 3251개 실행은 `/tmp/vcc-w5-python/bin/python`을 사용했으며 다른 PC에 이 임시 환경이 있다고 가정하지 않는다. 네트워크 제한 환경에서는 소유 loopback 시험 실행 권한이 필요하다.

변경 범위별 집중 검증은 다음 기준으로 고른다.

- 응답/입력: `test_vcc_contract`, `test_vcc_provider`, `test_vcc_api`, `test_vcc_wiring`, `test_vcc_app_contract_hardening`.
- 정책/상태: `test_vcc_policy`, `test_vcc_state`, `test_vcc_state_races`와 실제 `integration_tests/test_vcc_state_races_dynamodb.py`.
- 계산/복구/저장: `test_vcc_calculation`, `test_vcc_recovery` 계열과 실제 runtime/프로세스/파일 시험.
- 공유 코드: 위 해당 경계 시험 후 전체 회귀. 기존 계산·projection·auth·storage·jobs·worker의 영향 범위를 반드시 포함.

실제 DB 집중 검증은 실행기의 `--suite integration`, 로컬 경계 검증은 `--suite boundary`를 사용할 수 있다. race는 제어된 판정/commit 경계로 재현하고 무작위 sleep에만 의존하지 않는다. 오류는 정확한 코드, 결과·원본 보존, 부분 쓰기 0, 다른 scope/epoch 영향 0까지 확인한다.

새 참고 비교가 필요할 때만 `scripts/verify_reference_parity.py`를 사용한다. 참고 checkout·환경을 먼저 확인하고 별도 output directory에 생성한다. 기존 fixture·dataset·골든 기대값을 덮어서 통과시키지 않는다. 승인된 계산 예외와 실제 동작 정책은 DECISIONS를 따른다.

<a id="8-코드-품질과-배포-준비--2026-09-11"></a>
<a id="9-dependabot-검증-ci--2026-09-11"></a>
<a id="10-기능-보존-로컬-배포-준비성-감사--2026-09-11"></a>

## 6. 인수 완료로 확대하면 안 되는 항목

| 대상 | 남은 확인 |
|---|---|
| 실물 iOS/Android·마네킨 | 실제 누적 입력, 재생/문서 관측, 세션 복귀, 30초 대기, 디코딩·차트와 저장 용량 |
| 교육 완료 | CPR cycle·두 구조자·AED 완료 규칙. 현재 점수 제공과 pending_policy 유지 |
| ARC/MuleSoft | 공식 인증·배정·정의·진도·수정 의미·결과/완료 전달·멱등·응답 유실 계약과 승인 시험 |
| AWS | 실제 IAM·Gateway binary·S3·DynamoDB·Queue/Stream·Lambda·Relay schedule·DLQ·로그·성능·복구 |
| 운영 전환 | 앱 전환 일정·실제 자원·용량/비용·보관/삭제 정책·승인·호환 rollback |
| 실행 환경 | 실제 Linux 배포물·GitHub hosted CI 결과는 로컬 대역 시험과 별도 |
| 로컬 한계 | 부모 SIGKILL 후 Java orphan, OS/SDK hard deadline, 전체 DB/디스크 고갈, 로그의 무손실 보장 |

과거 감사의 예상 실패 4개와 보조 Lambda 결함 목록은 당시 관측이었다. 이후 승인된 독립 검출과 오류/로그 정제 구현이 반영됐으므로 이를 현재 미해결 결함으로 복사하지 않는다. 현재는 정규 회귀와 위 외부 확인 경계로 판단한다.

계산·상태·네트워크를 변경하면 관련 회귀와 실제 경계 시험을 다시 수행한다. 로컬 통과만으로 ARC 전송을 활성화하거나 운영값을 정하지 않는다. 배포/CI의 실제 설정과 후속 인수는 [DEPLOY_GUIDE](DEPLOY_GUIDE.md)를 따른다.

## 7. commit 전 정리와 앱 계약 검증

2026-09-18 사용자 요청으로 개발 기록·캐시·빌드 산출물·미사용 조사 도구를 정리하고 문서를 현재 방향과 구현 계약 중심으로 축약했다. 과거 개인 리뷰나 병렬 작업 프롬프트를 실행 지침으로 남기지 않았다. 당시 앱 명세의 요청·응답·오류·예시를 OpenAPI와 실제 구현으로 대조했다. 2026-09-19 후속 요청에 따라 동일 계약을 APP_API.md의 Markdown으로 통합하고 YAML 파일을 제거했다. 아래 수치는 형식 전환 전 실행 기록이다.

| 확인 | 결과 |
|---|---|
| 불필요 파일 정리 | 과거 작업 사본·검토 기록·빌드 ZIP·캐시·미사용 분석 스크립트 2개, 약 408 MiB 삭제. 중복 배포 안내는 통합 후 삭제 |
| 문서 축약 | `docs/*.md` 약 59% 축약(4758 → 약 1960줄). 당시 핵심 Markdown 9개와 OpenAPI 작성; 후속 Markdown 통합으로 YAML 대체 |
| 보존 대조 | 소스·설정·fixture 345개 중 동작/자료 변경 0. `lambda_handler.py` 설명의 문서 경로 1줄만 정정했고 실행 AST 동일 |
| 삭제 후 전체 회귀 | **3251 passed, 9 subtests passed**, 12463 warnings, 231.56초, exit 0 |
| 전환 전 OpenAPI 정식 검증 | OpenAPI 3.1 검사 통과, 구현 route 16개 일치 |
| 명세 예시 | 46개 예시의 schema 오류 0 |
| 실제 구현 응답 대조 | 임시 DB/Worker 기반 CourseHttp 응답 105건·요청 25건의 schema 오류 0. 경로 전체 선언 대조와 실제 관측 범위는 구별 |
| 독립 계약 교차검토 | 16개 경로·필수/null·enum·진도 단위를 코드와 대조. 기존 계산 fixture 하위 객체 3878개 schema 오류 0(전체 HTTP 응답 검증과 구별) |
| 최종 문서·배포물 검사 | 문서 계약·artifact 관련 89개 시험 통과, Markdown 파일 링크·앵커 누락 0, diff 공백 검사 통과 |

이 절의 정리·명세 검증은 앞선 AI 5인 재심과 구별되는 후속 작업이다. OpenAPI 예시 통과는 실물 앱 또는 실제 ARC 공급자 인수가 아니다. 제품 코드·fixture·운영 데이터 보존과 commit/push/배포 미실행 원칙을 유지한다.

**2026-09-19 Markdown 전환 확인:** 앱 API를 `APP_API.md` 한 파일로 통합하고 전달용 YAML을 삭제했다. 기존 계약의 16개 동작·168개 필드명·34개 오류 코드/고정 메시지 누락 0, JSON 예시 18개 문법 정상, 문서 링크·앵커 누락 0을 확인했다. 중첩 자료형·필수값·null은 기존 검증 계약 및 읽기 전용 교차검토로 대조했다. 문서 계약 회귀 57개가 통과했다. 이번 변경은 문서 형식·참조 정리이며 제품 코드·정책·운영 데이터 변경, 전체 회귀 재실행, commit/push/배포는 수행하지 않았다.
