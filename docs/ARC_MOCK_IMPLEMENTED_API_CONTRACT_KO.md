# 계산 입력·결과 상세 계약

갱신일: 2026-09-28. 앱이 호출하는 `/api/v2`의 경로·요청·응답·오류는 [앱 API Markdown 명세](APP_API.md)에 모았다. 이 문서는 v2 측정 업로드가 함께 쓰는 측정 입력·파서·계산 JSON·호환 문서의 상세 계약과 계산 상태·역할·저장 경계를 설명한다. v2 제어 API의 camelCase 필드와 이 문서의 snake_case 계산 필드를 혼용하지 않는다. 과거 VCC 설계 초안과 중복 DTO 표는 제거했다.

**현재 구현 계약이며 모든 환경의 사용 가능 선언은 아니다.** 기본 `scripts/serve_local.py`와 AWS Dev는 같은 Dummy Dev 임시 과정 15개(기존 5프로그램×3연령)를 기존 실행 정의로 연결하고 업로드한 실제 바이너리로 계산한다. Only 완료는 실제 횟수+tester Pass, CPR은 점수와 별도로 `pending_policy`다. 실제 앱·마네킨 현장 인수, AWS 운영 인수, ARC 제출은 별도다. [로컬 실행](LOCAL_RUN.md), [검증 범위](VALIDATION.md), [미정 정책](DECISIONS.md)을 따른다.

앞부분은 측정 업로드 입력, 계산 상태·오류, 역할·저장 경계이며 뒤의 **B부**는 기존 파서·계산 응답·호환 문서의 상세 계약이다. 내부 파서가 받는 입력 전체가 과정 시도의 고정 condition 또는 입력 projection을 통과한다는 뜻은 아니다. 예전 실행 정의로 저장된 결과와 현재 v3 adapter의 완료 근거를 구별한다.

근거 파일은 `mock_journey/calculation.py`, `legacy_bridge.py`, `state.py`, `jobs.py`, `worker.py`, `storage.py`와 `main.py`, `services/`다. 경로·역할의 기계 대조용 인벤토리는 [P4C_ROUTE_ROLE_MANIFEST.json](implementation_execution/P4C_ROUTE_ROLE_MANIFEST.json)(schema `arc-mock-route-role-v2`, `/api/v2` 전용·D103)이며 `tests/test_vcc_wiring.py`가 코드·APP_API와 대조한다.

## 1. 삭제된 `/mock/v1` API

2026-09-28 삭제(D103). 아래 표는 삭제된 경로의 목록(이력)이며 모든 서버에서 `404`다. 대응 v2 경로는 [APP_API §8](APP_API.md#8-기존-앱에서-바뀌는-지점)을 따른다. 이미 저장된 기존 시도는 v2 경로로 조회·재인가·취소·계산 마무리만 호환한다. 삭제 전 202 본문의 `wait_expired`·`status_path`는 v2에 없다. 세션 24시간, 차트 링크 300초, 파일 업로드 시작부터 최대 30초인 앱 대기 규칙은 APP_API를 따른다.

| Method | 경로 | 요청 body | 성공 응답 |
|---|---|---|---|
| POST | `/mock/v1/sessions` | `login_id`, `password` | 201 로그인 |
| GET | `/mock/v1/session` | 없음 | 200 현재 세션 |
| DELETE | `/mock/v1/session` | 없음 | 204 로그아웃·공유 진도 초기화 |
| GET | `/mock/v1/programs` | 없음 | 200 프로그램·공유 진도 |
| POST | `/mock/v1/attempts` | `client_request_id`, `catalog_version`, `program_id`, `target` | 201 생성, 같은 요청 재전송 200 |
| GET | `/mock/v1/attempts/{attempt_id}` | 없음 | 200 상태·별도 완료 판정 |
| POST | `/mock/v1/attempts/{attempt_id}/reauthorize` | `resume_credential` | 200 새 세션으로 연결 |
| POST | `/mock/v1/attempts/{attempt_id}/cancel` | `reason` | 204 조기 종료 |
| POST | `/mock/v1/attempts/{attempt_id}/calculation` | 기존 측정 전송 형식 | 200 저장된 기존 계산 JSON 또는 202 처리 중 |
| POST | `/cpr-analysis` | Bearer와 단일 `X-Attempt-ID`, 기존 측정 전송 형식 | 200 계산 JSON 또는 202 처리 중(시도 계산 경로의 별칭) |
| GET | `/mock/v1/attempts/{attempt_id}/calculation` | 없음 | 200 저장된 기존 계산 JSON 또는 202 처리 중 |
| GET | `/mock/v1/attempts/{attempt_id}/chart-link` | 없음 | 200 차트 링크 |

아래 객체 표의 `string`, `int`, `bool`, `object`, `array`, `null`은 실제 JSON 자료형이다. 계산값의 정수·소수·null·누락 여부를 문자열이나 0으로 바꾸지 않는다.

## 2. 측정 업로드 입력

`POST /api/v2/attempts/{attemptId}/calculation/`은 훈련 종료 후 앱이 누적한 전체 실제 측정 데이터를 한 번 받는다. 서버는 동일 입력 재시도도 수용한다. 서버가 마네킨 binary를 새로 생성하거나 가짜 점수를 만드는 흐름은 없다. 앱이 쓰는 기본 part와 호출 예시는 [APP_API §5](APP_API.md#5-측정-시도와-업로드)에 있고, 아래는 공용 파서가 받는 전체 호환 입력이다.

| Multipart part | 내용 |
|---|---|
| `rawHexBPfile` | 필수, 누적 CPR/압박/호흡 binary 파일의 bytes |
| `aedHexBPfile` | 선택, 누적 AED binary bytes |
| `condition` | JSON object. 시도 생성 응답 condition과 key·값·자료형이 같아야 함 |
| `vp_event_list` | JSON array of objects. 해당 이벤트의 `event`, `timestamp`, `last_timestamp`만 projection 가능 |
| `Custom`, `Open_Skill`, `Usage`, `Organization` | 기존 응답 문맥용 JSON. 허용 leaf만 사용 |
| `hstm_document` 또는 `hstm_document_b64` | 선택한 기존 호환 문서의 JSON 또는 base64 JSON |
| 기존 문서의 최상위 section | `DeviceInfo`, `Dummy`, `ResultSummary`, `ResultByCycle`, `CalculationService`, `Certification`, `ResultByCriteria`, `Institution` 등 기존 parser가 받는 JSON section |

`Content-Type: multipart/form-data; boundary=...`를 유지한다. 헤더가 모호하거나(대소문자 중복·여러 값·두 표현 충돌) 값이 비었거나 쉼표·CR·LF를 포함하면 파서와 시도 조회 전에 `400 INVALID_REQUEST`다([APP_API §5](APP_API.md#5-측정-시도와-업로드)). Gateway는 전체 bytes를 손상 없이 전달해야 하며 binary event의 base64 body와 `isBase64Encoded` 일치를 실제 배포에서 확인한다. 로컬 HTTP 수신부도 같은 기존 파서로 연결하며 multipart 파일을 두 번 base64 decode하지 않는다.

기존 base64 form 방식도 parser 경로로 남아 있다. 논리 필드명은 `cpr_b64_data`, `aed_b64_data`, `condition`, `vp_event_list`와 기존 문서 필드다. CPR/AED 필드 값은 URL-safe base64이고, 외피 parser 순서는 base64 decode → URL unquote → query parse다. 이중 URL 해석으로 `+`, `&`, `%`, `=`가 변형되지 않도록 기존 encoder/배포 전달 방식을 검증해야 한다. 일반 JSON에 bytes나 base64를 넣는 새 전송 형식은 추가하지 않았다.

호환 parser의 fallback·기본값·알 수 없는 form part 무시 동작은 유지된다. 제어 JSON의 엄격한 중복 key 규칙을 legacy multipart/form 내부까지 적용했다고 해석하면 안 된다. 파싱 뒤 소비되는 필드만 versioned projection으로 저장하며, 그 단계의 알 수 없는 최상위·하위 key와 허용하지 않은 leaf는 422다. `ResultByCriteria`의 metric leaf는 검증된 schema가 필요하다. 지원되지 않는 문서 필드를 빈 값으로 보정하여 접수하지 않는다. `ResultByCycle`의 기존 입력 점수는 저장한 대체 점수로 쓰지 않고 section 존재/null만 남긴 뒤 계산 결과로 생성한다.

인증 토큰과 알려진 HSTM credential 필드는 계산 문맥·저장 manifest에서 제거된다. 원래 event 전체나 Authorization header를 저장하지 않는다. 앱은 credential을 문서에 넣지 않고 Bearer header만 사용한다. 고정 condition이 다르면 409 `PROFILE_MISMATCH`다. 본문의 점수·목표 주장으로 완료 기준을 낮추지 않는다.

입력을 고정할 때 binary bytes와 projected 문맥의 자료형까지 식별한다. 같은 attempt에 같은 입력을 다시 보내면 현재 결과/상태를 반환하고 계산 Job을 새로 접수하지 않는다. 다른 입력은 409 `ATTEMPT_INPUT_CONFLICT`다. JSON field 순서 같은 비소비 차이와 숫자·null·누락 등 소비되는 값의 차이를 단순 문자열 비교로 혼동하지 않는다. 아직 접수하지 않은 cancelled attempt는 전송할 수 없다.

**현재 구현은 요청 안에서 30초 동안 기다리지 않는다.** 입력·outbox 접수 후 이미 결과가 있으면 200, 없으면 일찍 202를 반환한다. 계산+ARC 제출 대기는 D54에 따라 **앱의 파일 업로드 시작부터 최대 30초**이며 앱이 관리하는 정책이다. 서버가 업로드 시작 시각이나 경과 여부를 판정한다는 뜻은 아니다. 실제 Gateway timeout과 처리 시간 상한은 배포 인수에서 맞춘다. Retry-After나 별도 poll 간격은 현재 응답 계약에 없다.

## 3. 상태와 오류

| state | 의미·계산 조회 |
|---|---|
| `created` | 측정 접수 전. 계산 GET은 409 |
| `cancelled` | 조기 종료. 계산 GET/재인증은 409 |
| `queued` / `processing` | 접수·처리 중. 계산 조회는 202 |
| `evaluated` | 계산·평가가 확정 저장됨. 계산 조회는 200 |
| `outcome_unknown` | 실행·저장 뒤 결과를 확정할 수 없음. 계산 조회는 503, 임의 재호출 안 함 |
| `failed` | 입력 보관 검증·계약·계산의 확정 오류 또는 계산 재시작 한도 초과(D104). 계산 조회는 지정 503 |

`outcome_unknown`은 반드시 영구 최종 상태라는 뜻이 아니다. 이미 수행한 같은 호출의 저장된 candidate를 찾으면 회복할 수 있지만 계산 성공이나 실패를 추측하여 완료 처리하지 않는다. 계산 중단 뒤 재시작이 5번을 넘으면(6번째 중단) 재계산 없이 `failed`/`CALCULATION_FAILED`로 끝낸다. 교육상 Fail이 아니며 evaluation·진도 반영은 null이다(D104).

공개 오류 envelope와 과정 오류를 포함한 전체 코드표는 [APP_API §7](APP_API.md#7-취소복구전체-오류)을 따른다. 아래는 과정 오류와 함께 재사용하는 Journey 오류 코드(`mock_journey/errors.py`)다. `PROGRAM_ALREADY_COMPLETED`는 삭제된 `/mock/v1` 시도 생성의 legacy 이력 코드이며 새 요청에서는 발생하지 않는다. HTTP 수신부가 먼저 거절한 400/413은 JSON이 아닐 수 있으므로 앱은 HTTP 상태부터 확인한다. 서버 내부 오류 원문·token·upstream body는 응답 메시지에 넣지 않는다. 개별 경로가 아래 오류를 전부 발생시키는 것은 아니며 해당 인증/상태/데이터 조건에 따라 달라진다.

| HTTP | code | 고정 message |
|---:|---|---|
| 400 | `INVALID_REQUEST` | Invalid request. |
| 401 | `LOGIN_FAILED` | Login failed. |
| 401 | `SESSION_REQUIRED` | A valid session is required. |
| 401 | `SESSION_EXPIRED` | The session has expired. |
| 403 | `SESSION_REVOKED` | The session has been revoked. |
| 404 | `NOT_FOUND` | Not found. |
| 409 | `PROGRAM_ALREADY_COMPLETED` | This program and target are already completed. |
| 409 | `IDEMPOTENCY_CONFLICT` | The request identifier has different input. |
| 409 | `ATTEMPT_INPUT_CONFLICT` | The attempt already has different input. |
| 409 | `PROFILE_MISMATCH` | The attempt profile does not match. |
| 409 | `INVALID_STATE` | The operation is not allowed in this state. |
| 413 | `PAYLOAD_TOO_LARGE` | The request exceeds the verified payload limit. |
| 422 | `MEASUREMENT_INPUT_INVALID` | The measurement input is invalid. |
| 503 | `TEMPORARILY_UNAVAILABLE` | The service is temporarily unavailable. |
| 503 | `CALCULATOR_CONTRACT_MISMATCH` | The calculator contract is not verified. |
| 503 | `CALCULATION_OUTCOME_UNKNOWN` | The calculator outcome is unknown. |
| 503 | `STORED_INPUT_INVALID` | The stored input could not be verified. |
| 503 | `CALCULATION_FAILED` | The calculation could not be completed. |

`GET /api/v2/attempts/{attemptId}/`는 `failed`/`outcome_unknown`도 200 상태로 알려준다. 같은 상태에서 계산 GET은 해당 503 오류 envelope다. HTTP 503만 보고 모든 오류를 즉시 반복 계산하는 동작은 이 계약에 없다.

### 평가와 진도 반영

계산 결과 JSON은 저장된 값·자료형·null을 유지한다. 조회 때 점수 또는 누락 필드를 다시 계산하지 않는다. 완료 판정은 계산 JSON 밖의 `evaluation`, 현재 공유 진도 반영은 `progressApplication`으로 제공한다. 아래는 CPR 평가 구조 예시이며 실제 측정 결과의 점수를 뜻하지 않는다.

```json
{
  "goal":{"kind":"cycles","required":3,"status":"evaluated","observed":3,"met":true},
  "score":{"decision":"pass"},
  "program_completed":true,
  "reason_codes":[]
}
```

현재 v5 검출 adapter(`arc-internal-detection-v5`, D138·D139)와 직전 v4 adapter(`arc-internal-detection-v4`, 보존)는 v2 adapter에서 도입한 목표 평가 형식을 유지하며 모든 종류에서 `goal.status="evaluated"`, `observed`는 int, `met`은 bool이다. CPR 계열의 `observed`는 계산기가 `cpr`로 분류한 사이클 수다(D136). v4로 시작한 진행 중 시도는 같은 평가 형식이지만 계산은 원래 규칙(파일 끝 호흡 두 패킷, ARC 최소량 null)을 쓰고 `MINIMUM_QUANTITY_NOT_MET`를 내지 않는다. 이전 pending-v3 adapter로 시작한 진행 중 CPR 시도만 `status="pending_policy"`, `observed=null`, `met=null`이다. `required`는 int, `program_completed`는 bool, 점수 `decision`은 `pass`/`fail`이다. **목표 대기 때문에 기존 계산 JSON의 점수·null·차트를 바꾸지 않는다.**

목표 미달은 `GOAL_NOT_MET`, 목표 정책 대기는 `GOAL_POLICY_UNRESOLVED`, 점수 Pass 미충족은 `SCORE_NOT_PASS`를 배열에 기록한다. ARC CPR 최소 수행량 미달은 `MINIMUM_QUANTITY_NOT_MET`다(D139, v5 adapter 결과만). 순서는 목표 사유 → `MINIMUM_QUANTITY_NOT_MET` → `SCORE_NOT_PASS`로 고정이며, `SCORE_NOT_PASS`는 점수 자체가 기존 tester 기준에 못 미칠 때만 들어간다. 최소량 미달이면 표시된 총점과 무관하게 `score.decision="fail"`이고, `pass`에는 `MINIMUM_QUANTITY_NOT_MET`·`SCORE_NOT_PASS`가 모두 없다. 목표를 판단해 충족했고 `decision=pass`인 경우에만 완료한다. 앱의 Passing Score나 일반 `cycle_count`를 완료 근거로 대체하지 않는다. 과거 v1 adapter의 확정 평가에는 `goal.status`가 없을 수 있으며 저장된 계약을 v2로 덮지 않는다. 버전별 처리 경계는 [구조](ARCHITECTURE.md)를 따른다.

진도 반영은 `{applied: bool, applied_epoch: string|null, reason: string}`다. 계산의 조건 만족과 현재 공유 진도 반영은 별개다. 사유는 `PROGRESS_RESET` → `GOAL_POLICY_UNRESOLVED` → `ALREADY_COMPLETED` → `APPLIED`/`REQUIREMENTS_NOT_MET` 순서로 먼저 해당하는 하나이며, 이미 저장된 기존 시도의 계산 마무리에도 같은 순서를 쓴다(D117).

| reason | 의미 |
|---|---|
| `APPLIED` | 목표·Pass 만족, 현재 epoch의 미완료 항목(기존 시도는 프로그램·연령 조합)을 완료로 변경. applied=true |
| `REQUIREMENTS_NOT_MET` | 판단된 목표 또는 Pass 미충족. applied=false |
| `GOAL_POLICY_UNRESOLVED` | 현재 pending 목표 평가에서 정책 미확정. applied=false |
| `ALREADY_COMPLETED` | 먼저 끝난 동시 attempt가 이미 완료시켰거나 완료한 항목의 재수행. 기준 미달이어도 이 사유. 결과는 보관, applied=false |
| `PROGRESS_RESET` | 로그아웃으로 epoch가 바뀜. 예전 결과는 보관, 새 진도에는 반영하지 않음 |
| `PROGRESS_RECONCILIATION_REQUIRED` | 과정 평가 교체 확인이 필요해 반영 보류. 초기화 전 결과가 아닐 때 위 사유 대신 기록. applied=false |

epoch가 이미 바뀌었다면 `PROGRESS_RESET`이 우선하며 과거 완료 평가를 새 진도에 적용하지 않는다. 성공적으로 처리했다는 `state=evaluated`와 훈련 합격은 다르다. evaluated여도 프로그램이 미완료일 수 있다. 후발 실패 결과로 기존 완료 상태를 되돌리지 않는다.

## 4. 역할과 저장·배포 경계

| 역할 | 진입점 | 필요한 의존성과 실제 동작 |
|---|---|---|
| API | `mock_journey.handler.run` | 세션·진도·attempt·job DB, resume keyring, 실행 정의·projection, 과정 공급자·한도, 입력/결과 S3. 계산 실행과 분리하여 접수·조회하며 SQS로 직접 발행하지 않음 |
| Worker | `mock_journey.worker.run` | SQS가 넘긴 job_id, DB 거래·lease, S3 저장, 버전별 검증 adapter. 앱 Bearer/resume keyring은 받지 않음 |
| Relay | `mock_journey.dispatch.run` | Stream key/sequence 또는 `source=aws.events` 재조정, DB due query/거래, SQS send. S3·계산기·앱 keyring은 요구하지 않음 |

Gateway가 API에 전달하는 형식은 REST proxy event의 `httpMethod`, `path`, `headers`, `multiValueHeaders`, `body`, `isBase64Encoded`, query maps다. HTTP API v2나 임의 envelope를 자동 변환하지 않는다.

API 조립에는 `mock_journey.assembly.build_course_application`, Worker·Relay 조립에는 `build_worker`, `build_relay`를 사용한다. 로컬 기본 실행과 AWS API는 모두 `build_course_application`으로 `/api/v2`를 조립한다. 호출자가 client·계산 binding·설정·keyring·실행 정의·버전과 과정 공급자·한도를 명시하며, 각 함수는 해당 역할의 기존 구성 요소를 연결한다. 필요한 client나 binding이 없으면 조립 단계에서 거절한다. SDK 연결을 시도하거나 실제 접근 권한을 검사하는 기능은 아니다.

`ExecutionCatalog`는 프로그램·연령의 실행 정의, projection 자료형과 버전 연결을 검사한다. 카탈로그 생성만으로 CPR 완료 정책이나 모든 실행 구성이 검증된 것은 아니다. 현재 로컬 CLI와 명시적 AWS 역할 설정은 승인된 15개 정의와 v3 검출 adapter를 조립하되 CPR 목표를 `pending_policy`로 명시한다. 이전 v2는 저장 후보 복구용이며 새 core로 재계산하지 않는다. [버전 보존 경계](ARCHITECTURE.md#점수와-완료의-버전-경계)를 따른다. 실제 AWS 자원·권한·trigger 검증은 별도다.

SDK 호출 목록은 manifest에 기록한다. 이는 IAM 정책이나 배포 ARN이 아니다. SQS 수신·삭제, Stream 읽기, trigger partial-batch/DLQ/retry, schedule 연결은 플랫폼 설정으로 따로 검증해야 한다. 실제 역할별 리소스 범위·암호화·credentials·네트워크 제한·실행 시간은 이 문서에서 만들어내지 않는다.

API는 접수 전에 입력 저장을 확인하고 DB에 입력 참조·Job·Outbox를 고정한다. 실행자는 내부 계산기와 저장 candidate를 사용하고 Relay는 Job 참조만 전달한다. 같은 입력 재전송은 같은 Job을 가리키며 최종 채택 결과와 완료 반영은 한 번이다. 확정 전 장애 복구의 재계산 가능성과 정상 GET의 읽기 전용 동작을 구별한다. 전체 HTTP body·사용자 token을 queue에 싣지 않는다.

원본 저장은 기존 `util/uploader.py`의 `directory/stage/org/UTC날짜/stem` 규칙을 사용한다. CPR `.bin`, 선택 AED `.aed.bin`, `.meta.json`, 소비된 입력의 `.request.json`, 확정 차트 및 작업 artifact를 연결해 보관한다. meta에는 기존 정책상 조직/이름이 포함될 수 있으므로 파일 접근 보호와 로그 정제를 같은 의미로 취급하지 않는다. `stage=test`에서 원본 uploader는 저장을 생략하므로, 접수 경로는 실제 저장 확인 없이 성공하지 않는다.

이 코드에는 새 보관 기간·삭제 schedule이나 로그아웃 시 S3 삭제가 없다. HSTM과 같은 보관·삭제 운영 요구는 실제 기존 infrastructure/lifecycle 설정을 대조하여 배포 시 충족해야 한다. 확정 DB 참조의 필수 파일이 사라지면 `STORED_INPUT_INVALID`, 새 쓰기 확인 실패나 일시 저장 서비스 오류는 `TEMPORARILY_UNAVAILABLE`로 처리한다.

공개 `lambda_handler.run`은 항상 `mock_journey.handler.run`의 인증 경계로 연결된다. `_run_trusted_calculation`은 내부 계산 회귀용 helper이며 공개 handler로 구성하지 않는다. runtime 누락을 이유로 인증을 생략하지 않는다. 실제 Gateway·역할·자원 연결은 [AWS·Dev 안내](DEPLOY_GUIDE.md), 완료한 시험과 미검증 경계는 [검증 요약](VALIDATION.md)을 따른다.

## B. 기존 파서·계산 응답·호환 문서 상세

이하 §2~§7은 기존 파서·계산 호환 계약을 통합한 것이다. 승인 예외 P1~P4는 [결정 문서](DECISIONS.md), 비교 범위는 [검증 문서](VALIDATION.md)에 보존한다. 내부 파서의 AHA2020 기본값은 앱 요청의 기본 추천값이 아니다. 앱은 생성 응답의 condition을 그대로 보낸다.

인증된 접수에서는 파싱 후 고정 condition·허용 projection을 추가 확인한다. 현재 로컬 `metric_fields={}`는 임의 `ResultByCriteria` 지표 leaf를 허용하지 않는다. 아래 파서의 전체 HSTM 호환 필드 목록을 현재 로컬에서 모든 문서 내용의 접수까지 허용한다는 의미로 읽지 않는다. 문서는 계산 문맥으로 조립할 수 있지만 실제 HSTM/ARC 외부 전송이나 새 문서 전체 응답 필드를 만들지 않는다.

## 2. 요청 인코딩

### 2.1 Multipart

Content-Type에 `multipart/form-data`가 있으면 multipart 파서로 처리한다.
`isBase64Encoded=true`인 Gateway 이벤트는 전체 body를 base64 디코드한 뒤 multipart를 해석한다.
`rawHexBPfile`·`aedHexBPfile`의 payload는 추가 base64 디코딩 없는 raw 바이너리다.

REST API의 binaryMediaTypes 설정과 앱이 보낸 원본 바이트의 보존을 함께 확인해야 한다.
설정 이름만으로 원격 전송 성공을 검증했다고 볼 수 없다.

| part | 기대 입력·reference 해석 |
|---|---|
| `rawHexBPfile` | CPR raw 바이너리. 누락 시 파서 내부 값은 빈 bytes; 최종 오류는 유지한 ARC 입력 검증에 따라 처리 |
| `aedHexBPfile` | AED raw 바이너리. 누락 시 빈 bytes |
| `condition` | JSON 객체면 기본 condition 사본에 부분 merge. JSON 해석 실패나 비객체 입력은 기본 condition 유지 |
| `vp_event_list` | JSON. 미전송·해석 실패는 빈 목록. 정상 사용 형태는 이벤트 객체 배열 |
| `hstm_document` | JSON 문서. 해석 실패는 null |
| `hstm_document_b64` | URL-safe base64를 디코드한 UTF-8 JSON 문서. 해석 실패는 null |
| HSTM top-level JSON 필드 | §3의 이름 그대로 JSON 해석. 실패는 null |
| token·secret·URL·flag | §3.2의 문자열 및 별칭 규칙 |

반복된 multipart 필드는 reference의 순회 순서대로 처리된다. 같은 내부 슬롯을 쓰는 원래 이름과
별칭, `hstm_document`/`hstm_document_b64`가 함께 오면 뒤에서 처리한 값이 앞의 값을 바꿀 수 있다.
폼 경로의 우선순위와 같다고 가정하지 않는다.

### 2.2 Reference base64 폼

Content-Type이 multipart가 아니면 reference 폼 파서를 사용한다. 이 형식은 일반적인 JSON 또는
평문 `application/x-www-form-urlencoded` body와 다르다. 파서 순서는 다음과 같다.

1. 이벤트의 `body` 문자열 전체를 `urlsafe_b64decode`한다.
2. 디코드 결과에 `urllib.parse.unquote`를 적용한다.
3. 결과 문자열에 `parse_qs`를 적용한다.
4. `cpr_b64_data`와 `aed_b64_data` 필드의 첫 값을 다시 URL-safe base64 디코드한다.
5. condition·이벤트·문서 필드의 첫 값을 JSON 등으로 해석한다.

따라서 multipart의 `rawHexBPfile`과 폼의 `cpr_b64_data`는 필드명과 인코딩이 다르다.
폼의 바깥 디코딩은 이 프로토콜 자체의 단계이며 multipart의 Gateway `isBase64Encoded` 분기와 구분한다.
폼 경로는 해당 flag를 보고 별도의 추가 디코딩 단계를 수행하지 않는다.

`fields`의 바이너리 값은 먼저 URL-safe base64 문자열로 바꾼다. reference가 parse_qs 전에 unquote를
한 번 더 수행하므로, 한 번만 URL 인코딩한 값의 리터럴 `+`는 최종적으로 공백이 되는 동작도 있다.
HTTP 왕복 테스트는 `urlsafe_b64encode(quote(urlencode(fields), safe="").encode("utf-8"))`로
두 번의 URL 디코딩에 맞춘 입력을 검증한다. 별도 파서 테스트는 단일 인코딩에서 값이 바뀌는 원본 동작도 고정한다.
`&`, `%`와 중첩 JSON의 escaping 역시 일반 폼과 같다고 가정하지 않는다. 호환 복원 과정에서
임의로 codec을 고치거나 실제 앱의 인코딩을 검증 완료로 표시하지 않는다.

| 항목 | 폼 경로의 reference 동작 |
|---|---|
| CPR/AED 누락 | 각각 빈 bytes |
| 중복 키 | `parse_qs` 결과의 첫 값 사용. 빈 값은 기본 parse_qs 동작에 따라 생략될 수 있음 |
| `condition` | JSON 해석 성공 값을 그대로 사용. multipart와 달리 기본값과 부분 merge하지 않음. 누락·JSON 오류 시 기본 condition |
| `vp_event_list` | JSON 해석. 누락·오류는 빈 목록 |
| `hstm_document` | 직접 JSON 값이 truthy면 사용. 그렇지 않으면 `hstm_document_b64`로 fallback |
| 문자열 원래 이름/별칭 | 원래 이름의 truthy 값 우선, 없으면 `hstm_` 별칭 |
| `token_expired` | 원래 이름을 bool로 해석한 결과가 None일 때만 별칭 사용. False는 별칭으로 대체하지 않음 |

`condition`이 정상 객체인지, 필수 키가 있는지 등의 실패 동작에는 P1의 기존 ARC 보호 예외가 적용된다.
이 POST API에는 JSON action 입력 경로가 없다. 예전의 미사용 `parse_body_as_action` 함수는 2026-09-28 정리로 삭제했다.

### 2.3 패킷 검출과 앱 기록 계약

2026-09-11 사용자 확정 D38~D46과 2026-09-30 D138을 AHA2020/ARC2020/ARC2025/ERC2020/STD2015 모두에 적용한다. guideline별 점수식과 과정 시도의 ARC2025 고정 조건은 별개다.

- 첫 패킷은 압박 카운터 기준선이다. 앱은 첫 압박 전에 기준선 패킷을 넣는다. 이후 이전 값과 다른 양수 카운터를1회로 인정하며, 증가 폭으로 유실된 압박을 추정 복원하지 않는다.0은 새 기준선이며 사건을 만들지 않는다.
- 패킷의 두 호흡량 중 최대값을 사용하고 후보별 최고값을 추적한다. 성인·소아 최고값 대비10mL 이상 하강한 두 연속 패킷에서1회 확정한다. 원본 보정계수10이므로 raw50→49→49가1회다. 영아는 보정계수1, 감소5mL 잠정값이다. 공식 의학·기기 기준으로 확정한 수치가 아니다.
- 100→90→89,100→90→90,100→89→90,100→0→0은 모두1회다. 중간 패킷이 감소 조건을 벗어나거나 최고값이 갱신되면 연속 확인을 처음부터 다시 한다. 새로운 최고값을 이전 호흡에서 가져오지 않는다.
- 확정 후에는 대표량0 또는 패킷별 최대 압박 깊이의 상승 시작으로 재준비한다. 이후 새 호흡량 상승부터 후보를 시작하며 재준비한 패킷을 새 후보에 재사용하지 않는다. 아직 확인 중인 호흡을 압박 시작 때문에 취소하지 않는다. 깊이에 새 잡음 임계값을 추가하지 않는다.
- 파일 끝은 추가 관측이 아니며 길이로 호흡을 추정하지 않는다. 현재 adapter(v5, D138)는 파일 끝에 호흡 후보가 아직 남아 있으면(이미 확정·잠금·기준 미달 종료가 아님) 다음 중 하나일 때1회로 확정한다. ① 최고값보다 감소 기준(성인·소아10mL, 영아5mL) 이상 낮은 패킷이1개 이상 관측됐다. 예: 250→380→440→480(최고)→380 뒤 파일 종료. ② 하강 패킷이 하나도 없어도 그때까지의 최고 호흡량이 후보 시작 직전 패킷의 대표 호흡량(기준선, 첫 패킷이면0)보다 같은 감소 기준 이상 높다. 예: 0→250→380→440→480에서 종료, 평탄 구간 480→478에서 종료, 100→90→95(하강 뒤 재상승)에서 종료. 이때 끊긴 지점까지의 최고값이 그 호흡의 최고점이다(앱이 목표 호흡 감지 즉시 종료하며 끊긴 지점이 최고점 이전인지 서버가 알 수 없기 때문). 인정하지 않는 경우: 기준선 대비 상승이 감소 기준 미만(성인 9mL, 영아 4mL 등), 확정 뒤 재준비되지 않은 잠금 상태의 잔여 호흡량, 이미 두 패킷으로 확정한 마지막 호흡의 중복 추가. 재준비(D43)가 0이 아닌 호흡량에서 일어났으면 그 패킷의 호흡량이 다음 후보의 기준선이다. 보존 adapter(`arc-internal-detection-v4`, `arc-internal-detection-pending-v3`)의 진행 중 시도는 D42 그대로 두 패킷을 확인하지 못한 마지막 호흡을 추가하지 않는다.
- 앱은 목표 호흡을 스스로 검출한 순간 자동 종료하고 종료 신호 뒤에는 기록하지 않으므로(앱팀 확인, 2026-09-30) 최고값 직후 또는 그 이전에 끝나는 기록이 자동 종료 세션(CPR 사이클 한도, 호흡 Only8회)의 정상 형태다. 앱이 종료 신호 뒤 호흡량이0으로 돌아올 때까지 또는 최소2패킷을 더 기록하면 마지막 호흡의 실제 최고값·시간이 기록되므로 여전히 권장한다. 성인·소아는 보정계수10이라 raw1(10mL)이 곧 감소 기준이어서, 파일이 끝나는 순간 열려 있는 후보는 기준선보다 raw1만 높아도1회로 인정된다.
- 압박·호흡 사건과 측정 증거를 각각 보존한다. 겹친 전체 시간은 합집합으로 한 번만 합산하고 호흡률 분모와 구별한다. 같은 패킷에서 확정된 두 사건은 같은 계산 cycle에 넣으며 직전 동작이 호흡이면 둘 다 다음 cycle로 이동한다. 이 cycle 규칙은 CPR 프로그램 완료 규칙 Q22를 대신하지 않는다.

앱팀 검증 기록은 **시험 자료와 백엔드 검출 결과를 대조하기 위한 제안**이다. 이 저장소 밖의 앱에 로그를 설치하거나 실제 기기를 검증한 것은 아니다. 실물 시험 전에 앱 저장소/빌드 식별자, 마네킨 모델·펌웨어, binary codec 버전, 기록 시작/종료 절차를 확보한다. 시험 담당자가 동일한 누적 binary와 아래 진단을 비공개로 보관하고 정상·경계·유실·반등·압박 동시 진행 사례의 인정 시점을 패킷 단위로 대조한다.

| 기록 필드 | 단위·용도 |
|---|---|
| 시험용 임의 식별자, 앱/기기 버전 | 사람·로그인·시도 복구 증표와 연결하지 않는 재현 식별 |
| 원본 파일의 패킷 index·sequence·timestamp | 입력 순서를 보존하고 누락/중복/시간 역행을 확인. 임의 정렬·보간하지 않음 |
| 두 원본 volume·보정계수·mL 두 값·대표 최대값 | 성인·소아 raw1=10mL, 영아 raw1=1mL 변환을 분리해 확인 |
| 압박 counter·원본 깊이 배열·보정 깊이 최대값 | 카운터 기준선/변화와 깊이 상승 시작을 따로 확인 |
| 후보 시작·최고값/위치·감소 threshold·연속 확인0/1/2 | 최고값 기준 하강과 미확정 EOF를 재현 |
| 확정 packet index·재준비 사유·다음 후보 시작 | 같은 하강 중복, 동시 두 사건·cycle·측정 구간을 대조 |

인증정보·개인정보·원문 HTTP 요청·서명 URL은 이 진단에 넣지 않는다. 원본 binary는 기존 비공개 파일 보관을 사용한다. 패킷 진단 전체를 일반 stdout/Sentry/운용 DB 로그에 자동 전송하는 새 계약은 추가하지 않는다. 보관기간과 접근 권한은 실제 시험 전에 사용자가 정한다.

## 3. 조건·호환 필드

### 3.1 Condition

```json
{
  "mode": "training",
  "target": "adult",
  "training_type": "cpr",
  "guideline": "AHA2020",
  "cpr_cycle_type": "302",
  "is_2rescuers": false
}
```

| 필드 | 값·의미 |
|---|---|
| `target` | `adult`, `child`, `infant` |
| `training_type` | `cpr`, `compression_only`, `ventilation_only` |
| `guideline` | `AHA2020`, `ARC2020`, `ARC2025`, `ERC2020`, `STD2015`; 기본 AHA2020 |
| `cpr_cycle_type` | 정확히 문자열 `"152"`이면15:2, 그 외는30:2. 숫자152·공백 포함 값 등을 정규화하지 않음 |
| `mode` | 기본 training. assessment는 HSTM 호환 문서의 JudgResult 및 Usage 완성에 사용됨 |
| `is_2rescuers` | reference 필드 유지. 이 flag만으로 실제 VP 이벤트를 대신하지 않음 |

ARC2025는 reference에서 ARC2020과 같은 계산 설정을 사용한다. 지원 이름의 복원이 해당 기관의
새 공식 평가 기준이나 ARC 연동 승인을 의미하지 않는다. CPR 최소량은 비ARC guideline에 적용한 적이 없고(P3), ARC guideline에서는 D139로 v5부터 점수 null 대신 합격 조건으로 적용한다(§5).

VP 이벤트의 정상 입력 형태는 `[{"event":0,"timestamp":1000}, ...]`다. 이벤트 ID는 압박0/1,
호흡10/11, AED20/21이며 timestamp 단위는 ms다. 실제 코드가 사용하는 `last_timestamp` 우선순위와
잘못된 이벤트의 오류 처리도 호환성 검증 대상이다.

### 3.2 HSTM JSON과 인증 관련 호환 입력

다음 top-level 이름을 유지한다.

`DeviceInfo`, `Organization`, `Dummy`, `Open_Skill`, `ResultSummary`, `Custom`, `ResultByCycle`,
`CalculationService`, `Certification`, `Usage`, `ResultByCriteria`, `Institution`.

| 필드 | reference에서 소비하는 주요 값 |
|---|---|
| `Organization` | `org_id`, `org_name`, `First_name`, `Last_name`. 원본 메타데이터·문서에 사용될 수 있으며 사용자 인증 증거가 아님 |
| `Open_Skill` | `Passing_Score` 등. 합격선과 hStream 판정에 사용 |
| `Custom` | `PassThreshold`, `PassThresholdChild`, `CertificateAdult`, `CertificateBaby`, `CertificateChild`, `CertificateInfant`, `TrainCourse` |
| `Usage` | `Type`, `Regional_Option`, `Email`, `Comment`, `validNum`, `hstreamId` |
| `Dummy` | `DeviceID`, `Hardware` |
| `ResultSummary` | 입력 summary를 기반으로 계산 필드를 갱신. HstreamId 등 입력 식별자도 처리 |

문자열 호환 필드는 `access_token`, `refresh_token`, `client_id`, `client_secret`, `access_token_url`,
`send_result_url`, `source_endpoint`다. 각각 `hstm_` 접두 별칭도 같은 내부 필드로 해석한다.
`token_expired`/`hstm_token_expired`는 문자열을 소문자로 바꿔 `1`, `true`, `yes`, `y`이면 True,
그 외 있던 값은 False, 누락이면 None이다. 앞뒤 공백을 제거하는 새 규칙을 추가하지 않는다.

**이 필드들은 파싱 호환성만 유지한다.** 실제 HSTM 요청, refresh 교환, 앱이 지정한 URL로의 중계,
ARC 사용자 인증에 사용하지 않는다. 값을 로그나 오류 응답에 그대로 반환할 계약도 아니다.

`Usage.Regional_Option`은 reference 프롬프트 선택을 따른다. 누락·falsy는 british, 알려진 키는
`british`/`usa`/`korean`, 미지 문자열은 해당 guideline의 default 선택이다. 문자열은 strip/lower 후
비교한다. guideline과 reference의 프롬프트북 선택 조건까지 함께 적용하므로 모두 ARC 책을 쓴다고
단정하지 않는다. 부적절한 자료형의 오류 처리에는 P1의 기존 ARC 보호 예외가 적용된다.

## 4. 응답 자료형

아래는 **구현된 계약 구조**다. 로컬 검증 범위는 [검증 요약](VALIDATION.md)을 참조한다.
number/int/string 등의 기호는 자료형이며 전송할 JSON 값이 아니다.
선택적·calc_case별 키를 모두 나열한 JSON Schema도 아니다.

```text
{
  cpr_score: {
    total_score: {점수 필드, score_rescue_vent, overall, judg_result},
    part_scores: [{part_num, action_with_score_list, cycle_with_score_list, score}]
  },
  metrics: {지표별 분포, 평균·횟수·시간·CCF 등},
  aed_score: {overall, part_scores},
  training_stats: {cycle_count: int, elapsed_seconds: number},
  action_count: {comp: int, vent: int},
  guide_prompts: [string, ...],
  certification: {Target: "adult" | "child" | "baby" | "N/A"},
  chart_dataset_url: string | null,
  submit_arc: {status: "disabled", ok: false, error: "arc_contract_pending"}
}
```

`submit_arc`는 위 비활성 응답으로 고정한다. 이 필드가 있다는 사실은 외부 제출이나
ARC 과정 완료를 뜻하지 않는다. `/api/v2` 응답은 제출 상태를 계산 객체 밖의 `data.submit_arc`로 따로 반환한다([APP_API §6](APP_API.md#6-결과완료차트)). 생성한 HSTM 문서 전체를 새 응답 키로 노출하는 변경도 승인된 것이 아니다.

대표 점수 키는 `score_comp_depth`, `score_comp_rate`, `score_comp_no`, `score_comp_count`,
`score_recoil`, `score_hand_position`, `score_vent_vol`, `score_vent_rate`, `score_vent_count`,
`score_vent_speed`, `score_ccf`, `score_rescue_vent`, `overall`이다. **훈련종류·calc_case·집계 레벨에 따라
키 존재 여부와 0/null이 다르므로 단일 고정 키 집합을 모든 레벨에 적용하지 않는다.**

reference의 HTTP 변환은 `total_score.score_rescue_vent`를0으로 설정한다. 계산 core와 part/cycle의
rescue 값·누락 여부가 이 HTTP 값과 같다는 뜻은 아니다. `score_vent_rate_measured`는 total의 코칭
내부 신호로 응답 직전에 제거된다.

metrics에는 지표 분포 객체, 평균·합계·횟수·CCF 값 등이 들어간다. `VentilationSpeed` 등은 조건에 따라
객체·빈 객체·null이 다를 수 있다. score가 null이라는 이유로 해당 실측 평균까지 일괄 null로 바꾸지 않는다.

## 5. CPR 최소량: 합격 조건(현재)·점수 null 예외(보존 adapter 전용)와 원본 동작 복원

**2026-09-30 D139: 최소량 미달 시 점수는 표시하고 합격만 인정하지 않는다.** 현재 adapter(`arc-internal-detection-v5`)와 execution context 없는 직접 호출(로컬 도구·회귀 helper·참고 대조)은 ARC2020/ARC2025 CPR에서도 다른 guideline과 같은 방식으로 압박·호흡 그룹 점수와 총점을 계산한다(그룹 null 없음). 대신 v5의 평가(`evaluation`)가 아래 표의 최소량을 합격 조건으로 적용한다: ARC2020/ARC2025의 CPR 계열(CPR·2인 CPR·2인 CPR+AED)에서 압박·호흡 중 하나라도 표의 값 미만이면 표시된 총점이 80 이상이어도 `score.decision="fail"`, `program_completed=false`이고 `reason_codes`에 `MINIMUM_QUANTITY_NOT_MET`가 들어간다. 다른 guideline과 압박 전용·호흡 전용에는 적용하지 않는다. 판정은 계산 결과의 `action_count`와 점수 계산과 같은 단일 출처(`NullPolicy.create`)로 한다. 직접 호출 경로(평가 단계가 없는 `run_calculator` 결과)에는 합격 판정 자체가 없으므로 점수만 나온다. 아래 "미만은 해당 점수 그룹 null" 규칙들은 보존 adapter(`arc-internal-detection-v4`, `arc-internal-detection-pending-v3`)의 정의로 시작한 진행 중 시도가 원래 의미로 끝나도록 남겨 둔 동작이며(`CalculationOptions.minimum_quantity_null=True`, 새 사유 코드 없음), 저장된 과거 결과는 다시 계산하지 않는다. HSTM 호환 문서 내부의 조기 종료 게이트(`services/legacy_document.py`: 압박 Only60회·호흡 Only12회·CPR3사이클 미만이면 Fail)는 바뀌지 않았다.

| CPR target | 압박 최소량 | 호흡 최소량 |
|---|---:|---:|
| adult | 90 | 6 |
| child | 90 | 6 |
| infant | 45 | 6 |

**보존 adapter에서 ARC2020/ARC2025의 CPR에만 적용한다.** 기준 이상은 해당 최소량 조건 충족, 미만은 해당 점수 그룹 null이다.
파싱된 데이터에 계산 가능한 cycle이 없으면 원본의 조기 반환을 유지해 총점0을 반환한다.

- chest 그룹: `score_comp_depth`, `score_recoil`, `score_comp_no`/`score_comp_count`, `score_hand_position`.
- vent 그룹: `score_vent_vol`, `score_vent_count`, `score_vent_rate`, `score_vent_speed`.
- 해당 CPR 예외는 cycle·part·total 점수에 전파한다. 한쪽만 미달이면 제외한 그룹의 가중치를 빼고
  남은 가중치로 overall을 재정규화한다. 양쪽 미달이면 overall도 null이다.
- CCF·압박 속도·AED를 이 예외 때문에 일괄 null로 만들지 않는다.
- 압박 전용·호흡 전용에는 이 CPR 최소 횟수를 적용하지 않는다. 전용 훈련은 reference의
  calc_case·사이클/집계별 계산·0/null/누락 동작을 복원했다. 전 레벨을 일괄 null로 만든다고 약속하지 않는다.
- 양쪽 그룹이 충족된 경우에도 실제 데이터·calc_case에 따라 원래 산출되지 않는 값은 남을 수 있다.
  최소 횟수 충족이 모든 지표의 존재나 합격을 보장하지 않는다.
- AHA2020/ERC2020/STD2015는 이 추가 예외 없이 reference 계산을 유지하며 ERC rescue도 reference를 따른다.
- D139 이후 현재 규칙의 ARC CPR 점수(계산 결과)는 null 예외를 뺀 계산과 같다(합격 조건은 평가 단계에서 따로 적용). 참고 대조27건(`tests/fixtures/reference_parity/approved_expectations.json`)에 승인 예외 산식을 다시 적용하면 보존 옵션 결과와 정확히 일치하고, 예외가 null로 만들던 그룹 점수는 참고 구현 기록값으로 돌아온다. 단 호흡률 점수(`score_vent_rate`)3건(기록 자료 cpr_1·cpr_2·cpr_4)과 CCF·총점은 D45 시간·호흡률 구간 개정의 값(독립 기대값과 일치)이며 참고 기록값과 다르다. 이 차이는 D139 이전에도 비ARC guideline에서 같은 값으로 존재했다.
- 호환 문서에는 계산 결과의 실제 null을 보존한다. 실제 숫자인 평균·횟수·시간을 해당 점수 그룹이
  null이라는 이유로 null로 바꾸지 않는다. overall null이면 입력 문서의 Pass보다 Fail이 우선한다.

이전 ARC 각색의 B1 환기속도 분자 변경, B3 호흡 횟수 초과 criterion 변경, ONLY_VIRTUAL_PARTNER의
metric CCF 제외, rescue 경로 삭제, 전용 훈련의 일괄 null 정책은 새로운 승인 예외로 남기지 않는다.
해당 동작은 reference 기준으로 복원했으며 검증 사례·범위는 별도 기록한다.

## 6. Certification과 HSTM 호환 문서

### 6.1 최상위 certification

reference는 해당 대상이 `Custom.Certificate*`에서 요구되지 않으면 `{"Target":"N/A"}`를 반환한다.
필요한 대상이고 합격하면 adult/child를 그대로, infant를 `baby`로 반환한다.

- `CertificateAdult`, `CertificateChild`, `CertificateInfant`의 truthy 값이 해당 대상 요구를 만든다.
  문자열 `"false"`를 bool False로 재해석하는 규칙은 원본에 없다.
- `CertificateBaby`는 원본에서 `baby`라는 별도 대상 이름을 추가한다. 이것만으로 `target=infant`를
  요구했다고 바꾸지 않는다. 정상 지원 target은 adult/child/infant다.
- 최상위 certification 호출은 result_summary 없이 총점·합격선 fallback으로 판정한다.
- 합격선은 `Open_Skill.Passing_Score` → child의 `Custom.PassThresholdChild` → `Custom.PassThreshold`
  →80 순서다. 원본은 `int(float(value))` 변환을 사용하며 별도 범위 검증이 없다.
- 해당 경로의 overall이 None이면 합격하지 않는다. 이는 과거 `"Fail"` 문자열 응답을 유지한다는 뜻이 아니다.

### 6.2 문서 조립과 별도 판정

이 절은 reference 문서 조립과 승인된 P4 예외를 함께 적용하는 확정 계약이며 HTTP에 연결되어 있다.

truthy `hstm_document`가 있으면 그것을 기반으로 계산 필드를 갱신하고, 없으면 HSTM top-level JSON
필드의 non-null 값으로 문서를 만든다. 입력으로 문서가 구성되지 않으면 원본처럼 문서가 None일 수 있다.

`ResultSummary`, `ResultByCycle`, `ResultByCriteria`, 대문자 `Certification`, `Guide_prompts`를
reference에 맞게 조립한다. 존재하는 `Usage`·`Dummy`도 완성한다.

| 항목 | 호환 문서 동작(P4 포함) |
|---|---|
| `ResultSummary` | 압박/호흡 횟수, 실측 평균·시간·CCF, cycle 수와 판정·HstreamId 등을 반영. 대응하는 계산값이 명시적 null이면 null 보존 |
| `JudgResult` | Custom.TrainCourse 인증 평가 조건 우선. 그 외 Usage/condition의 Assessment 조건과 총점·사이클 수 사용. 나머지는 N/A |
| `hStreamResult/Reason` | Passing_Score와 overall이 숫자로 해석될 때 최소 시도량·총점·사이클 압박 횟수 점수·hand/recoil 게이트 판정 |
| 최소 시도량 게이트 | 압박 전용60회, 호흡 전용12회, CPR3cycles. 새 연령별 null 기준과 동일한 검사가 아님 |
| 문서 `Certification` | overall null이면 먼저 Target N/A. 그 외는 JudgResult의 Pass/Fail → hStreamResult의 Pass/Fail → 총점 비교 순서 |
| `ResultByCycle` | 원본의 None→0 치환을 적용하지 않고 계산 결과의 실제 None을 대응 Overall/ByCycle에 보존. 숫자0은 그대로0 |
| `ResultByCriteria` | 계산 결과가 제공한 지표 값과 실제 null을 반영. 숫자로 계산된 실측 지표를 점수 null 때문에 null로 바꾸지 않음 |
| `Usage` | Email 누락은 빈 문자열, Comment/Regional_Option 기본값, 훈련 Type, validNum 누락 시10자리 난수 등 원본 완성 규칙 |
| `Dummy` | Hardware가 falsy이면 `"1.0"` |

P4는 `ResultSummary`, `ResultByCycle`, `ResultByCriteria`로 옮기는 계산값의 명시적 null에도 적용된다.
reference가 None을0으로 치환하던 위치도 null을 보존하는 승인 예외다. 계산값의 키 자체가 누락되면
원본의 기본값·빈 객체·기존 값 유지 규칙을 따르며 누락을 새 null로 만들지 않는다. 압박 점수 그룹이
null이어도 실측 압박 깊이가 숫자로 계산되었다면 그 숫자를 유지한다. 평균 환기량이 명시적 null이면
LungGraph도 null이고, 압박 전용에서는 원본처럼 LungGraph 키를 제거한다.

`Custom.TrainCourse.Certification`은 문자열로 바꾼 소문자 값이 `true`인지 검사한다.
StopCondition의 `finish_cycle`은 cycle 수와, `finish_compression`/`finish_ventilation`은95% 조건으로
횟수와 비교한다. `Usage.Type` 누락·빈 값도 reference에서는 Assessment로 취급될 수 있으므로
`mode="training"`만 보고 모든 문서 판정이 N/A라고 단정하지 않는다.

`HSTM_V2_WITHHOLD_SCORE`가 활성화되면 특정 게이트 실패의 `hStreamScore`가 숫자 대신 `"--"`가 되는
reference 옵션이 있다. 기본은 꺼짐이며 이것은 실제 제출 활성화 옵션이 아니다. overall null일 때는
이 옵션보다 null 보존과 Fail 판정이 우선한다.

P4에 따라 overall=None이면 `JudgResult`와 `hStreamResult`를 Fail로 처리하고 입력의 이전 Pass를
사용하지 않고 기존 `hStreamReason`도 제거한다. `hStreamScore`는 null이며, 최상위 certification과
문서 Certification의 Target은 N/A다.
합격선0이나 이전 summary가 이 처리를 우회할 수 없다. 한쪽 점수 그룹만 null이고 overall은 숫자인
경우에는 그 총점과 원본의 적용 가능한 판정 게이트를 사용한다.

## 7. 오류·운영 부작용·검증 한계

P1에 따라 기존 ARC의 입력 검증·오류 정제·Sentry 보호를 유지한다. 실제 API·Worker·Relay에서는 Sentry를 켜지 않으며 Sentry 보호는 내부 helper에 남아 있다(D108). 내부 회귀 helper의 대표 오류는 `This guideline is not supported.`, `This target is not supported.`, `This training type is not supported.`, `CPR file is required.`, `Invalid request data.`다. 공개 측정 업로드 경로는 기존 parser/validator 오류에 422 `MEASUREMENT_INPUT_INVALID`를 반환한다. 삭제된 `/cpr-analysis`의 400 `{type:"client_error",...}` 형식은 더 이상 없다(D103). 인증·ID·소유권·입력/조건 충돌·크기 오류는 공개 오류 계약([APP_API §7](APP_API.md#7-취소복구전체-오류))을 따른다. 입력 검증 뒤 projection 오류도 422다. 공개 계산 경계의 실행/저장 오류는 현재 API 계약의 정제된 503이며 내부 helper의 모든 500 표현을 공개 응답으로 약속하지 않는다.

정상적인 multipart가 아닌 요청은 폼 파서로 가므로 비multipart라는 이유만으로400을 반환하지 않는다.
무한대 합격선의 정수 변환 오류, Sentry 초기화 장애, 응답 직렬화 실패 처리는 기존 ARC 동작을 유지한다.
일반 ValueError는 정제 문구로 응답하며, Sentry 초기화 실패 때문에 계산을 중단하지 않는다.

기존 ARC 원본·차트 저장과 Sentry 보호는 유지한다. 내부 진단 uploader의 저장 실패와 attempt API의 영속 접수·완료 저장은 다른 책임이다. 실제 입력 보관 확인 없이 202로 성공 접수라고 알리지 않는다. presigned URL의 코드 기본300초는 데이터 보존기간이나 로그아웃 즉시 철회 보장이 아니다.

요청 최대 크기는 Gateway·Lambda의 전체 페이로드와 multipart/URL/base64 오버헤드에 좌우된다.
원격 환경에서 검증한 최대 바이너리 크기·세션 길이는 아직 없다. 단위 테스트 결과만으로
모바일 codec, AWS 권한, 실제 앱 Journey 또는 ARC 연동이 검증되었다고 표시하지 않는다.


<a id="d-vcc-구현-착수용-내부앱-계약-v1--미구현"></a>

## 새 과정 API 계약의 위치

`/api/v2`는 [APP_API Markdown 명세](APP_API.md)를 따른다. 내부 DTO·저장 거래·복구 불변조건은 [ARCHITECTURE](ARCHITECTURE.md), 확정 정책과 외부 계약 대기는 [DECISIONS](DECISIONS.md)에 둔다. 이 문서의 snake_case 계산 필드를 v2 camelCase 제어 API 필드와 혼용하지 않는다. `calculation` 안의 기존 계산 JSON은 원래 자료형·null·키를 유지한다.
