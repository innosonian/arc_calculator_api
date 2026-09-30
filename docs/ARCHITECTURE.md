# 현재 구조와 변경 원칙

기준: 2026-09-28 코드 리뷰 후속 작업(결정 D102~D121 구현, `/mock/v1` 삭제, 동작을 보존한 품질 정리)을 마친 코드. 사용자 정책의 원본은 [DECISIONS](DECISIONS.md), 앱 요청·응답은 [APP_API](APP_API.md)와 [계산 입력·결과 상세 계약](ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md), 실행은 [LOCAL_RUN](LOCAL_RUN.md), AWS는 [DEPLOY_GUIDE](DEPLOY_GUIDE.md)를 따른다. 이 문서는 현재 구조와 변경할 때 보존할 조건을 설명한다.

## 1. 현재 구현과 실행 범위

- Python 3.12 백엔드이며 iOS/Android 화면·마네킨 연결 코드는 포함하지 않는다. 앱이 수집한 실제 누적 CPR/AED 바이너리를 기존 내부 계산기로 처리한다.
- `/api/v2`가 최종 버전이다(D103). 로컬 기본 실행과 AWS API는 모두 `build_course_application`으로 `course_v2`만 조립하며 `/mock/v1`·`/cpr-analysis`는 404다. 로컬은 AWS Dev와 같은 Dummy Dev 임시 과정 15개를 제품 코드 안의 로컬 구현 기본 한도로 조립하고 같은 카탈로그 용량 검사를 쓴다. 이미 저장된 v1 시도·작업은 조회·재인가·취소·계산 마무리만 호환한다.
- VCC는 합성 공급자와 실제 내부 계산을 연결한 로컬 내부 인수를 통과했다. 공식 ARC/MuleSoft 공급자, 실물 앱, AWS 운영 인수는 별도다.
- AWS API/Worker 설정에는 `course_v2_dummy` 절이 필수다(없으면 설정 오류, Relay에는 넣지 않음). `dev_course.py`가 기존 계산 정의로 임시 과정 15개를 제공하며 Dummy만 허용한다. Dev stage에서만 허용되므로 Beta/Prod API·Worker 조립은 G-RELEASE 전까지 거부된다. 테스트 fixture나 가짜 점수를 배포물에 넣지 않는다.
- 계산 성공, 프로그램 완료, 현재 공유 진도 반영, ARC 제출 상태는 서로 독립적이다. ARC/HSTM 실제 전송은 비활성이다.
- 직접 의존성은 `requirements.txt`, 배포 간접 의존성은 `constraints-lambda.txt`, 로컬 실행과 시험은 `requirements-local.txt`·`requirements-ci.txt`를 사용한다.

## 2. 코드 지도와 의존 방향

| 위치 | 책임 |
|---|---|
| `scripts/serve_local.py` → `local_server/cli.py` | Waitress HTTP·소유 DynamoDB Local·별도 Python Worker의 준비/종료 감독. `runtime.py`는 AWS Dev와 같은 `/api/v2` 조립, `http.py`는 가드 함수 순서의 전송 방어. `constants.py`·`addresses.py`·`private_fs.py`는 표준 라이브러리만 쓰는 로컬 기본값·주소 규칙·비공개 파일 기본 기능 |
| `lambda_handler.run` → `mock_journey/handler.py` | 공개 인증 진입점(REST proxy 이벤트). handler는 `course_http` 위임과 조립·부팅 실패 시 고정 503만 담당. `lambda_handler.py`의 나머지는 내부 회귀 helper `_run_trusted_calculation`과 D126에 따라 helper가 실제로 쓰는 이름(`_build_certification`·`_get_pass_threshold`·`_convert_result_to_legacy` 등)만 재내보내기. 파서·고정 문구·허용 집합의 정본은 `services/http/legacy_request.py` |
| `mock_journey/assembly.py`, `settings.py` | `build_course_application`·`build_worker`·`build_relay`가 client·저장소·실행 정의·한도를 명시 주입. Worker adapter·기동 binding 목록은 `worker_adapters`·`worker_required_bindings`로 AWS·로컬 공유 |
| `mock_journey/auth.py`, `service.py`, `catalog.py` | 세션·소유권·재인가. `JourneyService`는 `/api/v2` hook용 typed 명령 4개만, `catalog.py`는 확정 프로그램·연령과 실행 정의 키(`definition_key`/`definition_keys`) |
| `mock_journey/calculation.py`, `projection.py`, `typed.py`, `contracts.py` | 소비 입력 검증, 입력 식별, 자료형을 보존하는 직렬화·접수·결과 조회. binding 필드·adapter/projection 버전 레지스트리는 `contracts.py` |
| `mock_journey/state.py`, `jobs.py`, `storage_keys.py` | 조건부 DB 거래, epoch, 멱등성, owner·lease·fence, 결과 확정. §4 행 키와 GSI1 due partition은 import 없는 `storage_keys.py` 한 곳에서 만듦 |
| `mock_journey/storage.py`, `internal_calculator.py` | 비공개 파일·검증된 참조, 기존 계산기 호출과 후보 결과. D28 경로 규칙은 순수 모듈 `util/legacy_layout.py`(이식 원본 `util/uploader.py`는 유지) |
| `mock_journey/worker.py`, `dispatch.py` | 작업 실행·복구·최종 확정, Outbox 전달·due 작업 재조정. `JourneyWorker._process`는 `_claim` → lease guard 안의 `_produce` → `_final_renew` → `_commit` 단계 메서드이며 호출 순서는 `tests/test_worker_call_order.py` 골든이 고정 |
| `main.py`, `services/`, `data_handlers/` | 파싱된 입력의 준비·독립 동작 검출·계산·직렬화·기존 코칭. legacy wire 파서의 정본은 `services/http/legacy_request.py`(조립이 `lambda_handler`를 적재하지 않음) |
| `calculators/`, `transformers/`, `models/`, `config/` | 계산식·구간 분리·자료형·가이드라인·점수 기준. 비고정 코드용 guideline·`TARGETS`·`TRAINING_TYPES` 식별자는 `config/guideline_registry.py`(해시 고정 표와의 일치는 `tests/test_guideline_registry.py`가 검사; `scripts/verify_reference_parity.py`의 리터럴은 참조 저장소 실행 때문에 의도적으로 유지) |
| `mock_journey/aws_*.py` | 사용자 제공 AWS 설정 검증과 역할별 SDK·저장·로그·lease 연결. AWS 식별자 규칙은 `aws_scope.py` |
| `submit_arc.py` | 기존 ARC 제출의 비활성 경계. 현재 네트워크 요청 없음 |

VCC 전용 모듈은 모두 `mock_journey/` 안에 있다. HTTP → service → provider/policy/repository 방향으로 호출하며, 계산은 bridge → 기존 CalculationService/Worker로 연결한다.

| 모듈 | 책임·금지 경계 |
|---|---|
| `course_contracts.py`, `course_schema.py` | 공통 frozen DTO·Protocol·route·exact schema. 별도 동명 DTO를 만들지 않음 |
| `course_errors.py`, `course_settings.py` | 고정 오류 코드와 명시 한도. 운영값을 fixture에서 추론하지 않음 |
| `course_primitives.py` | 과정 경계 공통 검증·비밀 키 순회·RFC3339 primitive. 오류 코드와 UTF-8 검사 여부는 호출부가 명시(D119) |
| `course_provider.py`, `course_fixture.py` | 외부 경계·합성 공급자·전체 정의 검증. DB/HTTP 응답 작성은 하지 않음 |
| `dev_course.py` | 명시적인 Dummy Dev 임시 카탈로그(로컬·AWS Dev 공용, 용량 검사 `validate_dummy_catalog`). 기존 5프로그램×3연령, 훈련→최종평가, ARC 실제 배정·영상/문서 없음 |
| `course_policy.py` | 시작 가능 여부·콘텐츠 완료·과정 집계·제출 분류의 순수 판정 |
| `course_state.py` | `DynamoCourseRepository` 조립·공개 이름 재내보내기. `CourseStore`/`CourseBlobStore` 계약과 생성자는 `course_repo_core.py`(세션·USER 가드, guarded get/commit, 공유 snapshot)와 use-case별 `course_repo_inventory/refresh/start/report.py`(읽기 snapshot과 원자적 저장), 레코드·요청 digest는 `course_records.py`이며 `state.py`를 import하지 않음. 메모리 fake는 `tests/course_store_fakes.py` |
| `course_service.py` | 검증된 명령과 조회를 조정. 외부 원문 dict를 공개하지 않음 |
| `course_http.py`, `course_response.py` | route별 입력 검증·camelCase 응답·정제 오류. 헤더 표현 규칙은 `header_representation` 한 곳. hook 계약은 `course_contracts.CourseHooks`(frozen dataclass, 12개 callable; refresh 전용 `session_check` 포함)와 DTO `SessionRecord/AttemptRecord/CalculationRecord/ChartLinkRecord`(D128). `POST /session/refresh/`는 저장된 과정 스냅샷을 읽지 않는다. 측정 업로드 base64는 HTTP 계층이 64KiB 조각 단위로 검증만 하고 해제는 파서가 한 번 한다. `CourseService.list_courses/get_item`은 dict를 반환한다 |
| `course_calculation.py` | 시작 당시 실행 정의를 기존 계산 입력으로 고정 |
| `course_submission.py` | `CourseCompletionPlan`과 비활성 ARC gateway. 단독 DB commit/실제 전송 없음 |
| `course_recovery.py`, `course_runtime_recovery.py` | 복구 순수 판정과 실제 DB·입력·후보·adapter 검증 |
| `course_storage.py`, `course_wiring.py`, `course_mode.py` | 기존 비공개 저장소의 과정 blob adapter, 명시적 조립·hook 연결, import 없는 `course_v2` 표식 |

<a id="vcc-응답을-사용하는-과정-모델-변경-설계--2026-09-17-초안"></a>
<a id="vcc-구현-착수-기술계획-v1--2026-09-18"></a>

## 3. 과정 모델과 요청 흐름

과정은 `Course → CourseItem[]`이며 영상·문서·training·assessment가 같은 위계다. 퀴즈는 없다. 같은 item이 여러 배치에 나타날 수 있으므로 **배치 ID**로 시작·진도를 구분한다. 실제 마지막 assessment만 `final_assessment` 역할이며 앞쪽 assessment는 일반 수행 역할이다.

1. 로그인/명시 갱신은 learner inventory 세대를 먼저 예약하고 공급자에서 배정 목록·정의·진도를 받는다. 전체 결과 검증 후 현재 세대에만 적용한다. 오래된 성공·실패는 무시한다.
2. 과정 목록·상세·세션 GET은 저장된 검증 결과를 조회한다. GET마다 외부 조회를 시작하지 않는다. 한 과정이라도 대기이면 해당 저장 gate를 일관되게 노출한다.
3. 영상·문서는 콘텐츠 시작 receipt를 받고 관측 근거를 보고한다. training·assessment는 attempt/resume receipt를 받은 뒤 측정을 시작한다.
4. 계산 입력은 인증·소유권·고정 실행 정의·자료형 검증 후 원본과 입력 파일을 저장하고, DB에 Job/Outbox 참조를 접수한다. 처리 중 `202`, 확정 결과 `200`을 반환한다.
5. Worker는 lease/fence를 얻고 기존 계산기로 후보를 생성·검증한다. 결과, 평가, 진도, 마지막 평가 역할, 제출 제외 근거를 **하나의 DB 거래**에서 확정한다.
6. 앱은 결과/시도/과정 진도를 조회한다. 같은 호출의 상태와 본문을 묶어 반환하며 나중 상태 재조회로 `202` 본문을 `200` 결과처럼 승격하지 않는다.

앱 대기 최대 30초는 업로드 시작부터다. 이 시간은 서버 작업 취소·파일 삭제·lease 만료 기준이 아니다. 이미 접수한 작업은 계속 처리·보관하며 조회가 재계산을 시작하지 않는다.

### 식별자와 시작 당시 정의

- `CourseScope`는 provider·tenant·learner·등록·과정의 원래 ID로 구성한다. 공개 정수 ID와 실제 source ID의 자료형·귀속은 별도다.
- `scope_key = typed.digest([provider, tenant_id, learner_id, enrollment_id, course_id])`, `placement_key = typed.digest([scope_key, source_placement_id])`다. 제목·배열 위치·프로그램 ID를 저장 키 대신 쓰지 않는다.
- `CourseBundle`은 검증된 전체 정의다. JSON bytes를 소유한 DTO와 typed digest로 호출자의 mutable dict 변경 및 int/bool 혼동을 막는다.
- START/ATTEMPT는 원래 scope·epoch·배치·내용 버전·정의 hash·`content_identity_hash`를 고정한다. 동일한 버전 문자열이라도 자산/프로그램이 다르면 다른 내용이다.
- 신규 시작은 inventory/HEAD의 동일 snapshot, source/kind/역할, template binding 전체와 7-key 실행 정의를 대조한다. 공개 link가 다른 항목으로 바뀌면 `DEFINITION_CHANGED`, 부분 생성은 0이다.
- 새 정체성 필드가 없는 과거 course 행은 전체 정의 hash가 같을 때만 동일성을 입증한다. 변경 정의의 완료 근거를 추측하지 않는다.

## 4. 저장과 경합 방지

상태·참조는 기존 DynamoDB PK/SK 테이블, 큰 정의·입력·계산 결과는 기존 비공개 파일 저장소에 둔다. 실제 조립은 `CourseBlobStore`를 사용하며 메모리 blob으로 자동 대체하지 않는다. API/Worker가 같은 저장 binding과 hash를 검증한다.

| 행 | 역할 |
|---|---|
| `COURSE_LEARNER#… / EPOCH#…#HEAD` | 전체 배정 집합·inventory generation/revision·가용 상태 |
| `COURSE#… / EPOCH#…#HEAD` | 정의 참조·refresh generation/revision·gate·완료 집합·과정 집계 |
| 같은 PK의 `EPOCH#…#ITEM#…` | 배치별 완료/통과와 원래 근거 |
| 같은 PK의 `EPOCH#…#FINAL` | 같은 등록/epoch의 마지막 평가 하나: 자기 attempt 참조와 합격 근거(`passed_attempt_id` 등, D131에 따라 재응시 중에도 보존) |
| 같은 PK의 `EPOCH#…#START#…`, `EPOCH#…#REPORT#…` | 콘텐츠 시작과 immutable 보고 receipt·요청 digest |
| `COURSE_START#… / META` | 콘텐츠 시작의 원래 scope/epoch/세션 locator |
| `SESSION#… / COURSE_CREATE#…` | 같은 세션의 시작 요청 멱등 receipt |
| `SUBMISSION#… / RESULT#…` | 결과별 비실행 제출 의도·제외 근거 |
| 기존 USER/SESSION/ATTEMPT/JOB/OUTBOX | epoch·인증·계산 상태·작업 전달. course binding은 선택 필드 |

비공개 파일 저장소의 원본·메타·AED·차트 키 레이아웃(D28)은 `util/legacy_layout.py` 한 곳이 정의하며, `.request.json`은 D28 레이아웃이 아니라 `JourneyStorage` 고유 manifest 접미사(`storage.MANIFEST_SUFFIX`)다.

현재 epoch의 권위는 USER다. `COURSE#`·`COURSE_LEARNER#`의 진도 제어 SK는 epoch를 포함한다. locator와 세션별 멱등 receipt는 원래 epoch를 값에 고정하며 키에 추가하지 않는다. 새 HEAD/FINAL은 USER·세션 조건 아래 같이 생성하며 한쪽만 존재하면 무결성 오류다. ITEM은 첫 시작/쓰기에서 조건부 생성한다.

HEAD의 `progress_json`과 완료 집합은 집계용이다. ITEM의 근거, HEAD 집계, FINAL을 각각 다른 코드에서 독립 저장하지 않는다. `course_response`만 내부 placement hash/`completed/passed`를 공개 link ID/`isCompleted/isPassed`로 변환한다.

| 작업 | commit까지 보존할 조건과 결과 |
|---|---|
| refresh 예약 | USER epoch/revision·부모 inventory generation·HEAD revision. generation과 HEAD revision을 함께 증가 |
| refresh 적용 | 예약 당시 epoch·부모/자식 generation·revision. 검증한 전체 bundle만 ready, 실패는 waiting, 의미 불명 진도는 reconciliation_required |
| 신규 시작 | 유효 세션·USER·현재 배정 scope·inventory·HEAD·ITEM/FINAL의 판정 snapshot. receipt와 START/ATTEMPT·필요 ITEM·HEAD/FINAL을 함께 생성 |
| 콘텐츠 보고 | bound session·kind/version·범위/한도·요청 digest·USER/HEAD/ITEM/START revision. REPORT/근거/완료/집계를 한 거래로 반영 |
| 계산 finalize | 기존 owner/fence/lease·candidate·attempt binding·USER와 course 행 revision. 기존 Job 결과 확정에 course write-set을 합침 |
| 접수 전 취소 | created·미접수·FINAL 자기 참조. 자기 역할만 free로 변경하며 새 attempt 역할은 건드리지 않음 |
| 재인가 | 기존 resume 증표와 동일 principal·attempt revision. 세션 귀속만 바꾸며 원래 정의/epoch/역할 유지 |
| Dummy 로그아웃 | USER epoch를 변경. 과정 행을 무제한 삭제/초기화하지 않음. 실제 ARC 학생 진도는 유지 |

과정 저장 거래(inventory·epoch·refresh·start·report)는 커밋 직전 시계로 세션 만료도 다시 확인한다. 세션 ConditionCheck에 `expires_at > now` 조건(`CourseStore`의 condition_check 전용 `if_greater`)을 붙이며, 읽기와 커밋 사이에 만료되면 쓰기 0건으로 `SESSION_EXPIRED`다(D116). action 수는 늘지 않는다.

같은 세션/request ID와 같은 body는 원래 receipt를 반환한다. 다른 body는 `409`다. 이미 완료된 일반 훈련은 새로 시작하지 못하지만 먼저 시작한 동시 시도는 보존한다. 늦은 Fail이 기존 완료를 지우지 않는다.

이전 epoch 보고는 원래 START/REPORT 근거만 보관하고 현재 HEAD/ITEM/FINAL을 변경하지 않는다. 늦은 계산도 원래 결과를 보관하되 `PROGRESS_RESET`과 제출 제외를 기록한다. 이미 확정된 결과를 이후 reset 때문에 소급 분류하지 않는다.

조건 충돌 시 일부만 쓰거나 새로운 revision만 끼워 넣지 않는다. 모든 의존 행을 다시 읽어 판정한다. fixture 기준 최대 4회 후 `503 TEMPORARILY_UNAVAILABLE`이다. 동일 키의 중복 transaction action은 허용하지 않는다.

### 평가 교체와 미확인 외부 진도

마지막 평가의 원래 합격 결과는 유지한다(재응시는 D130으로 허용되며 합격 근거는 D131에 따라 보존된다). 배치·내용·실행 정의 교체를 새 평가의 Pass로 복제하지 않는다. `assessment_reconciliation_required`를 HEAD와 집계에 유지하고 `PROGRESS_RECONCILIATION_REQUIRED`로 적용을 보류한다. A→B→A 또는 반복 refresh만으로 해제하지 않는다.

내용 비교는 파싱한 JSON의 typed digest를 사용한다. 단순 key 순서 변경과 실제 교체를 구별한다. 일반 항목의 기존 완료 인정(D88)과 교체된 최종 평가의 판정은 다르다. ARC 진도의 빈 객체/null을 임의로 ready·미진행으로 해석하지 않으며 합성 공급자의 명시적 계약만 별도로 허용한다.

<a id="점수와-완료의-버전-경계"></a>

## 5. 계산·완료·버전 보존

기존 parser·projection·자료형·null·코칭과 승인된 ARC 최소량 정책을 사용한다. D139로 현재 adapter(v5)는 최소량 미달 그룹을 null로 만들지 않고 점수를 표시하되 최소량(D07)을 평가의 합격 조건으로 적용한다. 보존 adapter(v4·pending-v3)의 진행 중 시도는 원래의 null 처리로 끝난다. 소비하지 않는 요청 필드를 저장 입력에 무조건 포함하거나 현재 결과로 골든 정답을 재생성하지 않는다. HSTM 호환 문서는 ARC 제출 payload가 아니다.

| 구분 | 현재 판정 |
|---|---|
| 압박/호흡 Only | 실제 정수 관측 횟수로 목표 충족 + 기존 tester Pass 모두 필요 |
| CPR/2인/AED 완료(D136) | 관측 사이클 = `CalculationEvidence.cycles` 중 `calc_case == cpr`인 수(`mock_journey/cycle_goal.py`), 기준 3/8/10. 이전 pending 어댑터로 시작한 시도만 `pending_policy`·`GOAL_POLICY_UNRESOLVED` 유지 |
| 과정 완료 | 선행 모든 항목 완료 + 마지막 assessment의 `program_completed=true` |
| 영상·문서 | 승인된 전체 재생/표시·읽음 확인 정책을 보고 근거로 판정. 상세는 DECISIONS·API 계약 |

현재 adapter는 `arc-internal-detection-v5`(D136 사이클 완료 규칙 + D138 파일 끝 호흡 + D139 최소량 미달 점수 표시·합격 조건 유지), profile은 `tester-goal-cycles-v2`, projection은 `arc-local-projection-v1`, 후보 형식은 `arc-internal-calculation-v4`다. 보존 목록(`contracts.RETAINED_ADAPTER_VERSIONS`)은 `arc-local-calculator-pending-v2` → `arc-internal-detection-pending-v3` → `arc-internal-detection-v4` 순서다. 직전 `arc-internal-detection-v4`(profile `tester-goal-cycles-v1`, 후보 `arc-internal-calculation-v3`)와 `arc-internal-detection-pending-v3`(profile `tester-goal-pending-v2`, 후보 `arc-internal-calculation-v2`)는 그 정의로 시작한 진행 중 시도를 원래 의미로 계속 계산한다(pending-v3의 CPR 목표는 `pending_policy`). 형식이 같아도 adapter의 검출 의미는 다르다.

버전별 의미는 `mock_journey/contracts.py`의 `ADAPTER_FEATURES` 표 한 곳에 있다.

| adapter | CPR 목표 상태 | profile | 후보 형식 | 파일 끝 호흡(D138) | ARC 최소량 미달 점수 | 최소량 합격 조건·사유 코드(D139) | 계산 |
|---|---|---|---|---|---|---|---|
| `arc-internal-detection-v5`(현재) | `evaluated` | `tester-goal-cycles-v2` | `arc-internal-calculation-v4` | 열린 후보를 하강 1패킷 또는 기준선 대비 상승으로 인정 | 표시(null 없음) | 적용: `decision=fail`·`MINIMUM_QUANTITY_NOT_MET` | 가능 |
| `arc-internal-detection-v4`(보존) | `evaluated` | `tester-goal-cycles-v1` | `arc-internal-calculation-v3` | 두 패킷만(D42) | 그룹 null(D07/D08) | 없음(코드 없음) | 가능(진행 중 시도) |
| `arc-internal-detection-pending-v3`(보존) | `pending_policy` | `tester-goal-pending-v2` | `arc-internal-calculation-v2` | 두 패킷만(D42) | 그룹 null(D07/D08) | 없음(코드 없음) | 가능(진행 중 시도) |
| `arc-local-calculator-pending-v2`(보존) | `pending_policy` | `tester-goal-pending-v2` | `arc-internal-calculation-v2` | — | — | 없음 | 불가(검증 전용) |

두 계산 옵션은 `services/calculation_context.CalculationOptions(eof_single_confirmation, minimum_quantity_null)`로 전달된다: `InternalCalculator`가 자기 버전의 값을 `CalculationExecutionContext(options=…)`에 넣고, `main.run_calculator`가 검출 옵션을 `services.preparers.make_pre_action_list` → `ActionDataPrepare` → `PacketActionDetector`로, `services.calculate_cpr.make_calculate_result`가 최소량 옵션을 `services.calculators.calculate_cpr`로 넘긴다. `calculate_cpr`는 옵션이 켜져 있을 때만 `NullPolicy.create(...)`를 쓰고 아니면 `NullPolicy.inactive()`를 쓴다(정책 코드 자체는 해시 고정 그대로). execution context가 없는 직접 호출(로컬 도구·회귀 helper·참고 대조 스크립트)은 현재 규칙(True, False)을 쓴다. 레지스트리에 없는 버전 이름의 `InternalCalculator`도 현재 규칙을 쓴다.

최소량 합격 조건(D139)은 계산 옵션이 아니라 평가 단계의 버전 기능(`ADAPTER_FEATURES.minimum_quantity_pass_gate`, v5만 True)이다. `mock_journey/worker.evaluate`가 계산 결과의 `action_count`와 정의의 `condition`으로 `contracts.minimum_quantity_policy`(= 점수 계산이 쓰던 `NullPolicy.create`, 임계값을 복제하지 않음)를 호출해 ARC2020/ARC2025 CPR에서 압박·호흡 중 하나라도 최소량 미만이면 `passed = 기존 tester 합격 AND 최소량 충족`을 false로 만들고 `reason_codes`의 목표 사유 뒤·`SCORE_NOT_PASS` 앞에 `MINIMUM_QUANTITY_NOT_MET`를 넣는다. 횟수를 읽을 수 없는 결과나 구성할 수 없는 조건은 합격이 아니라 `CALCULATOR_CONTRACT_MISMATCH`다. `mock_journey/jobs.check_evaluation`은 저장된 평가에 횟수가 없으므로 v5의 `fail`에 대해 목표 사유 뒤 `[SCORE_NOT_PASS]`·`[MINIMUM_QUANTITY_NOT_MET]`·`[MINIMUM_QUANTITY_NOT_MET, SCORE_NOT_PASS]` 세 꼴만 받고(최소량 코드는 정책이 적용될 수 있는 ARC CPR 조건에서만), `pass`에는 두 코드를 받지 않으며, v4 이하 정의에서는 이 코드를 거절한다.

구버전 `arc-local-calculator-pending-v2`는 저장 후보 검증·결과 복구 전용(재계산 불가)이다. 후보 없는 이전 작업을 새 core로 계산하지 않는다. v1 후보 `arc-internal-calculation-v1`의 의미와 resolver 요구도 유지한다. 원래 입력·binding·call·파일을 자동으로 새 버전으로 덮지 않는다.

현재 검출은 압박/호흡 독립, 첫 압박 패킷 기준선, 최고 호흡량 대비 두 연속 패킷의 감소 확인을 사용한다. 성인·소아 10mL, 영아 5mL 잠정값과 EOF·동시 동작 시간/cycle 정책은 DECISIONS D38~D46과 D138을 따른다. 파일 끝 규칙(D138): 반복이 끝났을 때 호흡 후보가 아직 열려 있으면(이미 확정·잠금·기준 미달 종료가 아님) ① 최고값보다 감소 기준 이상 낮은 패킷이 1개 이상 관측됐거나 ② 하강 패킷이 없어도 최고 호흡량이 후보 시작 직전 패킷의 대표 호흡량(기준선, 첫 패킷이면 0)보다 감소 기준 이상 높을 때 1회로 확정한다(`packet_index`=마지막 패킷, 근거 구간=후보 시작~파일 끝, ②는 `first_confirmation_index=None`이고 최고값은 그때까지 관측된 최대). 앱이 목표 호흡을 감지한 즉시 종료하며 끊긴 지점이 최고점 이전인지 서버가 알 수 없으므로 끊긴 지점까지의 최고값을 최고점으로 인정한다. 기준선 대비 상승이 감소 기준 미만인 후보, 잠금 상태, 이미 확정된 호흡은 추가하지 않으며 파일 길이로 추정하지 않는다. 옵션 이름 `eof_single_confirmation`은 ①만 있던 때의 이름을 그대로 쓴다. 미정 교육 완료 공식을 검출 규칙에서 추론하지 않는다.

점수·코칭·파서에 쓰는 기존 소스와 prompt book·시험 입력은 `tests/fixtures/detection_revision/provenance.json`에 파일별 SHA-256(일부는 파일·함수 AST hash)으로 고정하며 `tests/test_detection_oracle.py`가 현재 파일과 대조한다. 결과가 바뀌지 않는 수정만 파일별로 재등록할 수 있고, 그 전에 기존 계산 골든·참고 구현 대조·독립 검출 oracle·원래 구현과의 무작위 대조 시험을 모두 통과해야 한다(D102). 첫 적용은 `calculators/waveform.py`·`transformers/part_divider.py`의 계산량 개선이며 미사용 `config/guidelines.py`는 삭제하고 항목을 뺐다. 시험 실패를 없애려고 해시만 바꾸거나 계산식을 교정하지 않는다(D03).

## 6. 마지막 평가와 복구

`FINAL.phase`와 attempt/JOB/call 상태는 다르다. 앱의 30초, 프로세스 종료, active count 감소, lease 만료만으로 평가 잠금을 풀지 않는다.

| FINAL phase | 의미·다음 시작 |
|---|---|
| `free` | 아직 합격 근거가 없는 휴지 단계. 시작 조건을 다시 검사하며 불합격 후 횟수 제한 없이 재응시 |
| `active` | 생성/접수/계산 중인 자기 attempt. 다른 마지막 평가 시작 차단 |
| `recovery_required` | 계산 결과·저장·구성이 불확실. 기존 증거 보존·복구 필요 |
| `policy_pending` | 목표 완료 규칙 미정. 가짜 Fail 또는 시간 경과 unlock 금지 |
| `passed` | `goal.status=evaluated`이고 `program_completed=true`인 합격 근거(`passed_attempt_id` 등)가 있는 휴지 단계. D130에 따라 재응시를 허용하며 재응시 중에는 `active`가 되지만 합격 근거는 유지된다. 재응시가 끝나거나 취소·해제되면 `free`가 아니라 `passed`로 돌아가고, 다시 합격하면 합격 근거를 최신 시도로 갱신한다(D131) |

`CourseCompletionPlan`은 `jobs.finalize`의 하나의 거래에 들어갈 write-set만 만든다. course 작업에서 이 계획이나 실제 복구 reader가 없으면 fail-closed하며 기존 Mock 완료 분기로 빠지지 않는다. binding 없는 기존 작업은 기존 경로를 유지한다.

`CourseRecoveryReader`는 실제 JOB/ATTEMPT/USER/원래 epoch HEAD/FINAL, 원래 입력·후보 파일, adapter·response를 검증한다. `RecoveryEvidence.snapshot_json`은 revision·epoch·정의·call·참조를 고정한다. typed 증거라도 close/reopen 직전 다시 읽고 대조한다.

- **후보 재개:** 유효한 원래 candidate가 있으면 재계산 0회로 차트·최종 결과·완료를 확정한다.
- **안전한 동일 작업 재호출:** 저장소 조회가 성공해 미확정 후보 부재를 확인했고 정확한 adapter가 계산 가능할 때만 새 fence/call/path를 사용한다. committed 후보 유실이나 I/O 오류는 후보 부재가 아니다.
- **구성/무결성 대기:** 잘못된 hash·없어진 확정 후보·부재 adapter는 보류한다. 파일 교체·다른 adapter·가짜 교육 Fail로 해결하지 않는다.
- **기술 종료:** 실제 calculate 호출의 확정 오류, 정확한 call/input, 현재 owner/fence/lease, 후보 조사와 결과 부재가 입증될 때만 terminal seal 거래를 쓴다. JOB/ATTEMPT 종료와 자기 FINAL 해제가 원자적이며 evaluation은 null이다.
- **재시작 한도 종료(D104):** JOB의 `calculation_restarts`(없으면 0)는 새 call/path를 발급하는 같은 조건부 거래에서 1씩 늘린다. 후보 없이 다시 계산해야 하는데 이미 5회 재시작했으면(6번째 중단) 재계산하지 않고 `CALCULATION_FAILED`로 닫는다. 과정 작업의 한도 종결 거래가 거부되면 별도 `retry_seconds` 연기 없이 lease 만료로만 다시 시도한다(D134로 현행 유지). 과정 작업은 위 terminal seal과 같은 원자 거래(근거 `calculation_restart_limit`)이며 완료 집합·점수·제출 기록은 바꾸지 않는다. binding 없는 기존 작업은 기존 실패 쓰기에 JOB `failure_basis`를 남긴다. 유효한 후보가 있으면 한도와 무관하게 재개하고, 저장소 조회 오류·구성/무결성 대기·`CALCULATION_OUTCOME_UNKNOWN`은 세지도 종결하지도 않는다. 종결된 작업은 Relay가 다시 깨우지 않는다.
- **과거 failed 후보:** seal 없는 course 작업의 유효한 candidate만 별도 CAS로 reopen한다. 다음 claim은 더 큰 fence를 얻으며 sealed 작업·기존 Mock failed에는 적용하지 않는다.

`CALCULATION_OUTCOME_UNKNOWN`은 후속 일시 오류로 덮지 않는다. 반복 전달이 재계산·Pass·역할 해제로 이어지지 않아야 한다. 같은 원래 call/input의 검증된 후보가 뒤늦게 도착하면 재계산 없이 확정할 수 있다.

## 7. 파일·보안·운용 로그

- 원본/입력/후보/결과는 공개 경로가 없는 비공개 저장소에 두고 namespace·binding·크기·SHA-256을 확인한다. 파일 저장과 DB를 하나의 transaction이라고 표현하지 않는다.
- 로컬은 0700 디렉터리·0600 키, 소유 UID, `O_NOFOLLOW`/fstat, 단일 hardlink, 제한된 envelope, flock, fsync→atomic replace→재읽기 검증을 사용한다. 기존 객체를 다른 내용으로 덮거나 실패 해결용으로 삭제하지 않는다.
- 차트만 검증된 경로로 서명한다. HMAC은 설치·host/port·GET·시간·객체/본문 hash에 결합하며 TTL 300초다. 만료 URL은 인증된 chart-link로 재발급한다. no-chart가 확인된 경우에만 URL/만료가 함께 null이다.
- Bearer·resume·차트 키를 구분한다. 다른 활성 세션의 attempt를 공유 진도라는 이유로 공개하지 않는다. 재인가는 기존 bound session 만료/폐기와 증표 검증 규칙을 따른다.
- exact peer IP/Host·Origin/Fetch Metadata·raw URI/query·HTTP framing·헤더/본문·응답 한도와 오류/Sentry 정제를 유지한다. 실제 API·Worker·Relay는 Sentry를 켜지 않으며 Sentry 정제는 내부 회귀 helper에 남아 있다(D108). LAN 평문은 명시적 시험 설정이며 운영 HTTPS/개인 권한을 대신하지 않는다.
- 비밀번호·토큰·복구 증표·raw body·바이너리·서명 URL·상류 원문을 로그에 넣지 않는다. 운용 로그는 `services/operational_logs.py` → `mock_journey/log_storage.py`의 유한 비동기 기록 경로다.
- `/api/v2`의 거절·예상 밖 오류도 기록한다. 로그인 경로는 `login_failed`, 그 밖의 경로는 `request_rejected`로 고정 `error_code`·`http_status`만 남긴다. 예상 밖 예외는 `TEMPORARILY_UNAVAILABLE`(503)로 기록하고 정제된 `request_failed` 진단을 더한다. 요청 동안 남는 기록에는 응답 `X-Request-Id`와 같은 소문자 UUID를 `http_request_id`로 붙이고, 기존 `request_id`는 런타임 ID로 유지한다. 레코드 schema 번호는 그대로다. 로컬 서버는 응답에 `X-Request-Id`를 싣지 않는다(D107).
- 로그는 업무 transaction/client와 분리한다. 장애·포화는 누락/미확정으로 집계하고 정상 훈련을 차단하지 않는다. RAM 대기 중 강제 종료까지 무손실을 보장하지 않으며 자동 TTL/삭제는 없다.
- 로컬 로그 조회는 `scripts/read_local_logs.py`다. `/healthz` 로그 수치는 API 프로세스 카운터이며 영구 감사 통계나 Worker 합계가 아니다.

## 8. 한도와 coding convention

`CourseSettings`는 호출자가 모두 명시한다. 아래는 시험 fixture 값이며 운영 정책이 아니다.

| 설정 | fixture 값 | 거절 원칙 |
|---|---:|---|
| 과정 항목 / 배정 수 | 64 / 100 | 전체 공급 응답을 거절. 일부만 ready로 만들지 않음 |
| bundle / 제어 요청 bytes | 262144 / 16384 | 공급 계약 오류 / 요청 413 |
| 보고당 / 누적 재생 구간 | 128 / 512 | 보고 전체 거절, 기존 근거 보존 |
| START당 보고 수 | 4096 | 기존 동일 receipt replay는 가능 |
| 거래 action / 충돌 재판정 | 20 / 4 | 부분 commit 없이 정제 오류 |

START의 canonical bytes는 보고 commit 전 400KiB 기술 상한으로 보수적으로 제한한다. 건수 한도보다 먼저 도달할 수 있으며 `413 PROGRESS_CAPACITY_EXCEEDED`로 REPORT/START/ITEM/HEAD를 모두 보존한다. DynamoDB 실측 byte 수와 정확히 같은 계산이라고 주장하지 않는다.

변경자는 다음 공통 규칙을 따른다.

1. 내부 snake_case, class PascalCase, constant UPPER_SNAKE_CASE, 4-space와 기존 import 배치를 사용한다. wire 변환은 응답 모듈에 모으며 `submit_arc`·기존 계산 JSON의 호환 이름은 유지한다.
2. 공용 경계는 DTO/Protocol과 자료형을 먼저 바꾼다. 외부 값은 exact type으로 검사하며 bool→int·문자열→숫자·null→0을 암묵 변환하지 않는다.
3. clock·UUID를 주입한다. 내부 UTC epoch 초, wire RFC3339 UTC, 영상 정수 ms를 구분한다. 경합 시험은 임의 sleep 대신 제어된 commit 경계를 사용한다.
4. 업무 오류는 고정 `CourseError`로 표현하고 SDK/JSON 오류는 경계에서 원문 없이 정제한다. 넓은 예외 처리로 테스트 실패를 숨기지 않는다.
5. import 시 client/socket/thread/파일 부작용을 만들지 않는다. 업무 정책에서 HTTP/SDK를 직접 부르지 않고 assembly가 의존성을 연결한다. import 순환·하위 계층의 상향 간선은 `tests/test_import_structure.py`의 허용 목록(`KNOWN_CYCLES`, `KNOWN_UPWARD_EDGES`) 밖으로 늘리지 않는다.
6. 판정에 사용한 snapshot과 쓰기 조건을 일치시킨다. 멱등 receipt, 기존 결과, epoch/lease/fence/귀속을 편의 때문에 완화하지 않는다.
7. 실제 필요 경계에 회귀를 추가한다. 독립 기대값·음성 사례·부분 쓰기 0을 검증하며 함수 호출 자체나 자기 비교를 성공 근거로 삼지 않는다. 해시 고정 파일(D102)과 특성화 골든(`tests/fixtures/v2_baseline/*`, `tests/fixtures/worker_call_order/*`)은 동작 보존의 증거이므로 현재 출력으로 재생성하지 않는다.
8. 시험 조립 루트는 `tests/journey_support.compose` 하나이며(V2Journey·runtime/application 지원·internal_http world가 공유) 각 루트의 값은 `HARNESS_*` 상수다. 계산기·DynamoDB 대역은 `tests/calculator_doubles.py`·`tests/dynamodb_doubles.py`, v2 경계 하네스는 `tests/v2_boundary_support.py`(module 준비 + 시험별 fork), DynamoDB Local fixture·과정 앱 조립 지원은 `tests/dynamodb_local_support.py`·`tests/vcc_application_support.py`다. `/api/v2` HTTP 더블은 `tests/course_hooks_support.hooks_with`로 `CourseHooks`를 만든다(키워드/dict hook 경로 없음).
9. 병렬 작업은 파일 소유자를 나누고 `state.py`, `jobs.py`, `worker.py`, `assembly.py` 공유 변경은 한 통합 담당자가 조정한다. 공용 계약·소비자·테스트를 함께 전달한다.

## 9. 운영 전환과 남은 확인

로컬 감독기는 자신이 띄운 DB/Worker만 소유·종료한다. 자식 준비 실패 시 계속 접수하지 않으며 같은 설치 재시작으로 복구한다. 부모 SIGKILL 뒤 Java orphan 가능성, OS/SDK의 강제 hard deadline은 해결된 것으로 주장하지 않는다.

AWS API/Worker/Relay runtime은 명시 설정으로 기존 조립에 연결한다. 역할별 IAM·자원·trigger·partial batch·due schedule·DLQ·로그 전달·Linux 패키지·용량은 실환경 인수가 필요하다. Relay의 환경별 진행 행은 create-only 초기화와 revision/owner/fence/lease를 사용하며 런타임이 손상/부재 행을 자동 생성하지 않는다. 한 호출 안에서는 OUTBOX/JOB을 한 step씩 교대하되, 한 종류가 이번 pass를 끝냈거나 자기 한도(`page_size × max_pages`)에 닿으면 남은 종류를 한도와 step 시간 예산이 허락하는 만큼 계속 처리한다. 이렇게 순서 밖에서 처리한 step은 진행 행의 다음 종류를 바꾸지 않아 멈춘 종류가 다음 호출에서 먼저 처리된다(D109).

Dev 세 Lambda의 코드는 `develop` 머지 시 `.github/workflows/deploy_dev.yml`이 배포한다(D137): 같은 revision의 오프라인 회귀 → ZIP 빌드·보관 → environment 승인 → OIDC → `scripts/deploy_dev_lambdas.py`의 설정 대조(`CONFIG_DRIFT`)·Worker→Relay→API 갱신·`CodeSha256` 대조·Dummy 스모크. 설정 JSON은 손으로 갱신하며 workflow는 쓰지 않는다.

`course_v2_dummy`는 Dev stage에서만 허용하며 API·Worker가 같은 카탈로그 버전·9개 과정 한도·저장·실행 정의를 사용해야 한다. 실제 과정 snapshot의 직렬화 크기를 저장 한도와 대조해 시작조차 불가능한 설정을 거절한다. `scripts/validate_aws_dev_bundle.py`는 역할 간 DB/환경/저장/version 혼선, Worker와 Queue의 시간 관계, Relay가 한 항목도 처리하지 못할 실행 예산을 오프라인에서 검사한다. 실제 자원·IAM 확인을 대신하지 않는다.

| 확인 경계 | 해소 전 동작 |
|---|---|
| G-ARC: 공식 인증·배정·정의·진도·제출 계약 | Unavailable provider/Disabled gateway. 빈 성공·가짜 ID·외부 송신 없음 |
| G-CONTENT: 실제 앱 관측·콘텐츠 접근 계약 | 내부 보고 계약만 시험. 실물 앱 연결 별도 |
| G-GOAL: CPR/2인/AED 교육 완료 규칙 | D136 사이클 규칙 + 실제 점수. 이전 어댑터 시도만 pending_policy |
| G-REVISION: 평가 교체·ARC 진도 정정 의미 | 원래 결과·합격 근거 보존, 새 완료 적용 보류 |
| G-DELIVERY: 제출 멱등·응답 유실·초기화 후 전달 | 비활성/제외만 기록. 실행 가능한 ARC 전송 queue 없음 |
| G-RELEASE: 자원·용량·retention·앱 전환 | 로컬 내부 PASS만 인정. Beta/Prod API·Worker 조립은 거부(fail-closed). commit/push/배포는 사용자 결정 |

전환 전에는 기존 queued/running/candidate와 private 참조의 호환 reader/worker를 검증하고 앱 전체 흐름·접수 중단·재시작을 확인한다. `/mock/v1` 삭제(D103) 뒤 새 USER 행에는 v1 진도 slot이 없어 삭제 이전 binary가 읽지 못하므로 단순 rollback하지 않는다. 이미 저장된 course binding 없는 시도·작업은 `/api/v2`의 조회·재인가·취소와 Worker의 계산 마무리만 호환한다. 신규 접수를 중단하더라도 기존 결과 조회와 호환 worker를 유지하며 데이터/키 삭제로 복구하지 않는다. 현재 검증 범위는 [VALIDATION](VALIDATION.md)을 따른다.
