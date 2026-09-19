# 계산 입력·응답 및 기존 Mock API 상세 계약

갱신일: 2026-09-19. 새 `/api/v2`의 현재 요청·응답·오류 계약은 [앱 API Markdown 명세](APP_API.md)에 모았다. 이 문서는 기존 `/mock/v1` 및 공용 측정 파서·계산 JSON의 상세 계약이다. 두 버전의 경로·필드 이름·envelope를 혼용하지 않는다. 과거 VCC 설계 초안과 중복 DTO 표는 제거했다.

**현재 구현 계약이며 모든 환경의 사용 가능 선언은 아니다.** 기본 `scripts/serve_local.py`는 실제 로컬 DB·파일·계산 실행기와 15개 프로그램/연령 정의를 연결한다. 저장소의 기록된 실측 바이너리로 로그인→생성→업로드→계산 결과·실차트까지 검증했다. Only 완료는 실제 횟수+tester Pass, CPR은 점수와 별도로 `pending_policy`다. 실제 앱·마네킨 현장 인수, AWS 실자원 인수, ARC 제출은 별도다. [로컬 실행](LOCAL_RUN.md), [검증 범위](VALIDATION.md), [미정 정책](DECISIONS.md)을 따른다.

앞부분은 공개 API·진도 계약이며, 뒤의 **B부**는 기존 파서·계산 응답·호환 문서의 상세 계약이다. 내부 파서가 받는 입력 전체가 현재 Mock의 고정 condition 또는 입력 projection을 통과한다는 뜻은 아니다. 예전 실행 정의로 저장된 v1 결과와 현재 v3 adapter의 완료 근거를 구별한다.

근거 파일은 `mock_journey/handler.py`, `service.py`, `calculation.py`, `auth.py`, `catalog.py`, `state.py`, `jobs.py`, `worker.py`, `storage.py`다. 기계 대조용 인벤토리는 [P4C_ROUTE_ROLE_MANIFEST.json](implementation_execution/P4C_ROUTE_ROLE_MANIFEST.json), 시험은 `tests/test_mock_route_contract.py`다. 아래 URL은 호스트가 없는 경로 계약이며 서버 주소를 뜻하지 않는다.

## 1. 공통 규칙과 접근 보호

- Gateway가 전달하는 형식은 REST proxy event의 `httpMethod`, `path`, `headers`, `multiValueHeaders`, `body`, `isBase64Encoded`, query maps다. HTTP API v2나 임의 envelope를 자동 변환하지 않는다.
- 모든 Mock 경로는 팀원별 테스트 접근 보호를 먼저 거쳐야 한다. 이 바깥 보호 계층의 계정 발급·검증은 현재 application handler에 구현되어 있지 않다. 고정 dummy 계정만으로 외부 접근자를 제한할 수 없다. 실제 Gateway·인증 제공자·직접 Lambda 호출 차단은 배포 검증 항목이다.
- 로그인 이외의 아래 Mock 정상 경로는 `Authorization: Bearer <session_token>`이 필요하다. 별도 로컬 차트 파일 GET은 발급된 capability URL로 인증한다. `Bearer`의 대소문자는 무시한다. 헤더명이 중복되거나 multi-value에 둘 이상 있거나 단일/multi-value 표현이 다르면 `SESSION_REQUIRED`다. 같은 한 값을 Gateway가 두 표현으로 중복 전달한 것은 허용한다.
- 토큰은 문자열 전체를 그대로 전달한다. dummy 토큰과 attempt의 `resume_credential`은 용도가 다르다. 실제 ARC의 user token·identifier 연동은 아직 구현 계약이 아니다. 내부 계산 함수는 앱 토큰을 다시 검증하거나 저장하지 않는다.
- 모든 경로에서 비어 있지 않은 query map을 거절한다. query 검사는 routing·세션 인증보다 먼저 한다. 알려진 보호 경로에서는 세션 인증 뒤 제어 JSON이나 측정 body를 처리한다. 알 수 없는 경로는 인증 전에 404가 될 수 있다.
- 제어 요청은 JSON object이며 해당 경로에 명시된 필드만 허용한다. 각 필드는 비어 있지 않은 문자열이고 UTF-8 기준 최대 256 bytes다. 숫자 `2222`와 문자열 `"2222"`는 다르다. JSON 중복 key·비유한 수치·해석 불가·지나친 중첩은 400이다.
- 제어 JSON 제한은 Gateway가 넘긴 `event.body` 문자열의 UTF-8 **16,384 bytes**다. `isBase64Encoded`가 참이면 이 크기 검사 뒤 base64/UTF-8 decode한다. 측정 전송에는 별도의 명시 payload 한도를 적용한다. 운영 한도 값은 아직 이 문서에서 정하지 않는다.
- Mock handler의 성공·오류는 `Content-Type: application/json`, `Cache-Control: no-store`를 반환한다. `/cpr-analysis` 파싱 오류와 HTTP 수신부의 선행 거절은 아래 오류 예외를 따른다. 204는 빈 body다. GET 및 DELETE의 body는 handler에서 읽지 않는다. 현재 handler는 OPTIONS/CORS 응답이나 405를 별도로 만들지 않는다.
- `{attempt_id}`는 소문자 16진수 `8-4-4-4-12` UUID 형태다. 생성값은 UUID이고 handler는 별도 UUID version을 강제하지 않는다. 경로의 대소문자·끝 `/`를 정규화하지 않는다.

## 2. 기존 11개 경로와 인증된 계산 별칭

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
| POST | `/cpr-analysis` | Bearer와 단일 `X-Attempt-ID`, 기존 측정 전송 형식 | 200 계산 JSON 또는 202 처리 중; 기본 로컬 실행에 연결됨 |
| GET | `/mock/v1/attempts/{attempt_id}/calculation` | 없음 | 200 저장된 기존 계산 JSON 또는 202 처리 중 |
| GET | `/mock/v1/attempts/{attempt_id}/chart-link` | 없음 | 200 차트 링크 |

아래 객체 표의 `string`, `int`, `bool`, `object`, `array`, `null`은 실제 JSON 자료형이다. 계산값의 정수·소수·null·누락 여부를 문자열이나 0으로 바꾸지 않는다.

### 로그인·세션

```json
{"login_id":"test@test.com","password":"2222"}
```

위 조합만 로그인한다. 성공 시 매번 별도의 세션을 발급한다. 로그인 자체는 기존 공유 진도를 초기화하지 않는다.

| 필드 | 201 로그인 | 200 현재 세션 |
|---|---|---|
| `environment` | string `"mock"` | 동일 |
| `session_id` | string | 동일 |
| `session_token` | string, Bearer 인증용 | 필드 없음 |
| `expires_in` | int `86400` | 필드 없음 |
| `expires_at` | UTC RFC3339 string, `Z` 끝 | 동일 |
| `mock_user` | object `{id: "dummy-tester", display_id: "test@test.com"}` | 동일 |

세션은 발급 시점부터 24시간이며 조회로 연장되지 않는다. 앱 재실행 시 재로그인 정책은 서버가 프로세스 재실행을 알아내는 기능을 뜻하지 않는다. API는 별도의 자동 refresh endpoint를 제공하지 않는다.

### 프로그램·공유 진도

`GET /mock/v1/programs`의 최상위는 `catalog_version` string, `progress_epoch` string, `progress_version` int, `profile_name: "tester"`, `guideline: "ARC2025"`, `guideline_basis: "ARC2020"`, `programs` array다. 현재 `catalog_version`은 `mock-catalog-v1`이다. 이 버전과 아래 id를 서버 응답에서 사용한다.

| id | name | `goal.kind` | `goal.required` |
|---|---|---|---:|
| `mock-cpr` | CPR Training | `cycles` | 3 |
| `mock-compression-only` | Chest Compression Only | `compressions` | 60 |
| `mock-ventilation-only` | Ventilation Only | `ventilations` | 8 |
| `mock-two-rescuer-cpr` | 2-Rescuer CPR | `cycles` | 8 |
| `mock-two-rescuer-aed` | 2-Rescuer CPR with AED-T | `cycles` | 10 |

각 프로그램은 `id`, `name`, `is_mock: true`, `supported_targets: ["adult","child","infant"]`, `goal`, `progress_by_target`, `active_attempts_by_target`를 가진다. `goal.required`는 int다. 각 target별 진도는 `"completed"` 또는 `"not_completed"`; active count는 int다. 합계 15개 조합이며 동시에 여러 attempt가 열릴 수 있다. active count는 기기 연결 상태나 heartbeat가 아니라 서버가 집계 중인 attempt 수다. 평가·취소·실패·결과 불명 처리 때 집계에서 제외하며 로그아웃 때 초기화한다. 이후 결과 불명 작업이 복구 중이어도 집계에 다시 추가하지 않으므로 count=0을 모든 결과 확정으로 해석하지 않는다.

프로그램 목록의 ARC2025 표시는 ARC2020을 준용하는 확정 Mock 정책이다. 내부 계산기의 연령별 최소량·null 정책은 유지한다. 현재 기본 로컬에는 15개 실행 정의가 등록되어 있다. 다른 환경의 목록 조회 성공만으로 해당 환경의 실행 구성을 검증한 것은 아니다. 훈련에는 시간 목표가 없으며 실제 마네킨 연결·목표 도달 자동 종료·연결 끊김 감지는 앱이 수행한다.

### Attempt 생성·조회

생성 body는 네 문자열만 받는다.

```json
{"client_request_id":"app-generated-request-id","catalog_version":"mock-catalog-v1","program_id":"mock-cpr","target":"infant"}
```

`client_request_id`는 같은 세션 내 생성 재시도의 식별자다. 같은 id·동일 body는 기존 attempt와 같은 resume credential을 200으로 돌려준다. 현재 실행 정의나 카탈로그가 바뀌어도 이미 성공한 생성 응답부터 복구한다. 같은 id에 다른 body는 `IDEMPOTENCY_CONFLICT`다. 새 훈련에는 새 id를 사용한다. 완료된 프로그램·연령에 새 attempt를 생성하면 `PROGRAM_ALREADY_COMPLETED`다. 아직 완료 전에는 같은 조합의 동시 생성이 가능하다.

| 공통 attempt 필드 | 자료형·의미 |
|---|---|
| `attempt_id`, `program_id`, `target`, `state` | string |
| `progress_epoch` | 생성 당시 공유 진도의 string 식별자 |
| `profile_name` | string `"tester"` |
| `condition` | 고정 실행 정의의 object, key·값·자료형 그대로 |
| `calculation_profile` | 고정 실행 정의의 object, null·정수·소수 등 그대로 |
| `goal` | object `{kind: string, required: int}` |
| `catalog_version`, `profile_version` | string |
| `calculation_path` | string, 해당 attempt의 계산 경로 |
| `evaluation` | 평가 전 null, 평가 후 아래 평가 object |
| `progress_application` | 반영 전 null, 확정 후 아래 반영 object |
| `resume_credential` | 생성·생성 재시도 응답에만 string. 일반 조회·reauthorize에는 없음 |
| `error` | `outcome_unknown` 또는 `failed`에만 `{code: string, message: string}` |

`condition`은 `mode`, `target`, `training_type`, `guideline`, `cpr_cycle_type`, `is_2rescuers`를 사용한다. 서버가 돌려준 고정값을 보존한다. 성인·소아 30:2, 영아 15:2 정책을 위해 앱이 자체 추측한 enum이나 profile을 만드는 계약이 아니다. 내부 계산 binding·projection·profile의 확정 버전은 서버 설정 사항이다. 실행 정의가 없으면 생성은 503 `CALCULATOR_CONTRACT_MISMATCH`다.

공유 진도는 모든 dummy 세션에 보이지만 개별 attempt는 연결된 `bound_session`만 조회·전송·취소할 수 있다. 다른 기기가 같은 dummy 계정으로 로그인했다는 이유만으로 개별 결과 접근이 허용되지 않는다. 존재하지 않거나 소유 세션이 다른 attempt는 404다.

### 실제 측정 데이터 전송

훈련 종료 후 앱이 누적한 전체 실제 측정 데이터를 한 번 전송한다. 서버는 동일 입력 재시도도 수용한다. 서버가 마네킨 binary를 새로 생성하거나 mock 점수를 만드는 흐름은 없다.

| Multipart part | 내용 |
|---|---|
| `rawHexBPfile` | 필수, 누적 CPR/압박/호흡 binary 파일의 bytes |
| `aedHexBPfile` | 선택, 누적 AED binary bytes |
| `condition` | JSON object. 생성 응답 condition과 key·값·자료형이 같아야 함 |
| `vp_event_list` | JSON array of objects. 해당 이벤트의 `event`, `timestamp`, `last_timestamp`만 projection 가능 |
| `Custom`, `Open_Skill`, `Usage`, `Organization` | 기존 응답 문맥용 JSON. 허용 leaf만 사용 |
| `hstm_document` 또는 `hstm_document_b64` | 선택한 기존 호환 문서의 JSON 또는 base64 JSON |
| 기존 문서의 최상위 section | `DeviceInfo`, `Dummy`, `ResultSummary`, `ResultByCycle`, `CalculationService`, `Certification`, `ResultByCriteria`, `Institution` 등 기존 parser가 받는 JSON section |

`Content-Type: multipart/form-data; boundary=...`를 유지한다. Gateway는 전체 bytes를 손상 없이 전달해야 하며 binary event의 base64 body와 `isBase64Encoded` 일치를 실제 배포에서 확인한다. 로컬 HTTP 수신부도 같은 기존 파서로 연결하며 multipart 파일을 두 번 base64 decode하지 않는다.

기존 base64 form 방식도 parser 경로로 남아 있다. 논리 필드명은 `cpr_b64_data`, `aed_b64_data`, `condition`, `vp_event_list`와 기존 문서 필드다. CPR/AED 필드 값은 URL-safe base64이고, 외피 parser 순서는 base64 decode → URL unquote → query parse다. 이중 URL 해석으로 `+`, `&`, `%`, `=`가 변형되지 않도록 기존 encoder/배포 전달 방식을 검증해야 한다. 일반 JSON에 bytes나 base64를 넣는 새 전송 형식은 추가하지 않았다.

호환 parser의 fallback·기본값·알 수 없는 form part 무시 동작은 유지된다. 제어 JSON의 엄격한 중복 key 규칙을 legacy multipart/form 내부까지 적용했다고 해석하면 안 된다. 파싱 뒤 소비되는 필드만 versioned projection으로 저장하며, 그 단계의 알 수 없는 최상위·하위 key와 허용하지 않은 leaf는 422다. `ResultByCriteria`의 metric leaf는 검증된 schema가 필요하다. 지원되지 않는 문서 필드를 빈 값으로 보정하여 접수하지 않는다. `ResultByCycle`의 기존 입력 점수는 저장한 대체 점수로 쓰지 않고 section 존재/null만 남긴 뒤 계산 결과로 생성한다.

인증 토큰과 알려진 HSTM credential 필드는 계산 문맥·저장 manifest에서 제거된다. 원래 event 전체나 Authorization header를 저장하지 않는다. 앱은 credential을 문서에 넣지 않고 Bearer header만 사용한다. 고정 condition이 다르면 409 `PROFILE_MISMATCH`다. 본문의 점수·목표 주장으로 완료 기준을 낮추지 않는다.

입력을 고정할 때 binary bytes와 projected 문맥의 자료형까지 식별한다. 같은 attempt에 같은 입력을 다시 보내면 현재 결과/상태를 반환하고 계산 Job을 새로 접수하지 않는다. 다른 입력은 409 `ATTEMPT_INPUT_CONFLICT`다. JSON field 순서 같은 비소비 차이와 숫자·null·누락 등 소비되는 값의 차이를 단순 문자열 비교로 혼동하지 않는다. 아직 접수하지 않은 cancelled attempt는 전송할 수 없다.

### 처리 중·결과·완료 판정

처리 중 응답은 다음 필드만 가진다. `state`는 `queued` 또는 `processing`이다.

```json
{"attempt_id":"<attempt-id>","state":"queued","wait_expired":false,"status_path":"/mock/v1/attempts/<attempt-id>"}
```

**현재 구현은 요청 안에서 30초 동안 기다리지 않는다.** 입력·outbox 접수 후 이미 결과가 있으면 200, 없으면 일찍 202를 반환한다. 따라서 `wait_expired`는 false다. 사용자가 정한 계산+ARC 제출 대기는 D54에 따라 **앱의 파일 업로드 시작부터 최대 30초**다. 앱이 관리하는 대기 정책이며 서버가 업로드 시작 시각이나 경과 여부를 판정한다는 뜻은 아니다. 실제 Gateway timeout과 처리 시간 상한은 배포 인수에서 맞춰야 한다. 이 코드만으로 모든 네트워크 요청이 30초 안에 끝난다고 주장하지 않는다. `GET /mock/v1/attempts/{attempt_id}/calculation`으로 계산 JSON, `GET /mock/v1/attempts/{attempt_id}`로 처리 상태·완료 여부를 다시 조회한다. Retry-After나 별도 poll 간격은 현재 응답 계약에 없다.

200 계산 body는 저장된 계산 필드의 값·자료형·null을 유지하고 현재 제출 상태를 응답에서 합성한다. 저장 snapshot 자체는 바꾸지 않지만 응답 전체 JSON bytes는 같지 않을 수 있다. 점수·지표·통계·평균·코칭·차트 등은 기존 결과와 `services/legacy_response.py` 후처리의 계약을 따른다. 조회 때 점수 또는 누락 필드를 다시 계산하거나 새 envelope로 감싸지 않는다. `certification`은 기존 규칙의 object다. 최상위 `submit_hstm`은 제거하고 `submit_arc: {"status":"disabled","ok":false,"error":"arc_contract_pending"}`를 붙인다. 실제 ARC 제출은 수행하지 않는다.

새 완료 정보는 계산 body에 추가하지 않고 attempt 조회로 제공한다.

현재 로컬 v2의 CPR 평가 구조 예시다. 실제 측정 결과의 점수를 뜻하지 않는다.

```json
{
  "goal":{"kind":"cycles","required":3,"status":"pending_policy","observed":null,"met":null},
  "score":{"decision":"pass"},
  "program_completed":false,
  "reason_codes":["GOAL_POLICY_UNRESOLVED"]
}
```

현재 v3 검출 adapter도 v2에서 도입한 목표 평가 형식을 유지한다. Only는 `goal.status="evaluated"`, `observed`는 int, `met`은 bool이다. CPR은 완전한 cycle의 정의가 미정이므로 `status="pending_policy"`, `observed=null`, `met=null`이다. `required`는 int, `program_completed`는 bool, 점수 `decision`은 `pass`/`fail`이다. **목표 대기 때문에 기존 계산 JSON의 점수·null·차트를 바꾸지 않는다.**

목표 미달은 `GOAL_NOT_MET`, 목표 정책 대기는 `GOAL_POLICY_UNRESOLVED`, 점수 Pass 미충족은 `SCORE_NOT_PASS`를 배열에 기록한다. 목표 사유가 점수 사유보다 앞선다. 목표를 판단해 충족했고 점수도 Pass인 경우에만 완료한다. 앱의 Passing Score나 일반 `cycle_count`를 완료 근거로 대체하지 않는다. 과거 v1의 확정 평가에는 `goal.status`가 없을 수 있으며 저장된 계약을 v2로 덮지 않는다. 버전별 처리 경계는 [구조](ARCHITECTURE.md)를 따른다.

`progress_application`은 `{applied: bool, applied_epoch: string|null, reason: string}`다. 계산의 조건 만족과 현재 공유 진도 반영은 별개다.

| reason | 의미 |
|---|---|
| `APPLIED` | 목표·Pass 만족, 현재 epoch의 미완료 조합을 완료로 변경. applied=true |
| `REQUIREMENTS_NOT_MET` | 판단된 목표 또는 Pass 미충족. applied=false |
| `GOAL_POLICY_UNRESOLVED` | 현재 pending 목표 평가에서 정책 미확정. applied=false |
| `ALREADY_COMPLETED` | 먼저 끝난 동시 attempt가 이미 완료시킴. 결과는 보관, applied=false |
| `PROGRESS_RESET` | 로그아웃으로 epoch가 바뀜. 예전 결과는 보관, 새 진도에는 반영하지 않음 |

epoch가 이미 바뀌었다면 `PROGRESS_RESET`이 우선하며 과거 완료 평가를 새 진도에 적용하지 않는다. 성공적으로 처리했다는 `state=evaluated`와 훈련 합격은 다르다. evaluated여도 프로그램이 미완료일 수 있다. 후발 실패 결과로 기존 완료 상태를 되돌리지 않는다.

### 조기 종료·재인증·로그아웃

- `POST /mock/v1/attempts/{attempt_id}/cancel`의 reason은 `user_stopped` 또는 `manikin_disconnected`다. created에서 cancelled로 바꾸고 같은 취소 재시도는 204다. queued 이후 취소는 409 `INVALID_STATE`다. 조기 종료 시 앱은 결과 화면을 열지 않는 것으로 이미 정해졌으며 BE는 화면 전환을 수행하지 않는다.
- 세션 만료 후에는 dummy 재로그인으로 새 Bearer를 받고 해당 attempt의 `resume_credential`로 `POST /mock/v1/attempts/{attempt_id}/reauthorize`한다. 원래 연결 세션이 만료되었거나 revoked여야 다른 세션으로 이관된다. 아직 활성 세션의 attempt를 빼앗아 오면 409다. 같은 연결 세션의 재요청은 그대로 200이다. cancelled는 다시 살릴 수 없다. proof 필드 누락·빈 문자열·잘못된 JSON 자료형은 400 `INVALID_REQUEST`다. 제어 요청 형식을 통과한 proof가 맞지 않거나 대상 attempt가 없으면 404다. 성공 후 원래 attempt/input/result를 사용하며 새 계산 입력을 별개 훈련처럼 만들지 않는다.
- 같은 dummy 계정은 동시 로그인할 수 있다. 어느 한 기기의 로그아웃도 전체 15개 조합의 진도·active count를 새 epoch로 초기화한다. 로그아웃한 세션은 revoked가 되고, 다른 기기의 세션은 유지된다. 다른 기기에서 프로그램을 다시 조회하면 초기화된 공유 상태가 보인다.
- 같은 로그아웃 토큰의 재시도는 저장된 receipt를 확인해 204로 끝나며 두 번째 초기화를 하지 않는다. revoked 토큰을 일반 API에서 쓰면 403이다. 자연 만료만으로 공유 진도를 초기화하지 않는다.
- 로그아웃은 원본·기존 결과를 지우는 API가 아니다. 예전 epoch의 진행 중 계산이 나중에 끝나면 저장·조회할 수 있지만 새 진도에는 반영하지 않는다. 예전 attempt의 세션이 revoked이면 새 세션에서 resume proof로 이관한 뒤 조회한다. 다른 세션이 계속 활성인 경우 그 세션은 자신의 예전 attempt를 계속 조회한다.

### 차트

`GET /mock/v1/attempts/{attempt_id}/chart-link`는 evaluated 상태에만 가능하다. 응답은 `attempt_id` string, `chart_dataset_url` string|null, `expires_at` UTC RFC3339 string|null이다. 검증된 차트가 없다는 확정 결과라면 URL과 시각이 모두 null이다. 저장 오류나 미확정 원격 차트를 차트 없음으로 바꾸지 않는다.

발급된 링크의 만료는 **300초**다. 로그아웃이 이미 발급한 링크를 즉시 폐기하지 않으며, 300초 만료를 무기한 열림으로 바꾸지 않는다. 새 링크 발급에는 유효한 소유 세션이 필요하다. 기존 계산 200 body 안의 URL은 저장 당시 값이므로 오래된 결과를 재조회할 때 만료되었을 수 있다. 계산 body를 수정하는 대신 이 경로에서 새 링크를 받는다. 현재 로컬의 차트 파일은 발급된 `/local/v1/charts/{opaque-token}` URL 그대로 GET한다. 이 파일 경로는 session Bearer 대신 capability로 인증하고 query·변조·만료·허용되지 않은 접근은 거절한다. 원본·키·임의 파일을 제공하는 경로가 아니다. 이후 AWS의 S3 서명 URL은 같은 300초 정책에 맞춰 실제 객체 읽기 권한·만료를 검증한다. 앱 사용자에게 AWS credential을 발급하는 계약이 아니다.

## 3. 상태와 오류

| state | 의미·계산 조회 |
|---|---|
| `created` | 측정 접수 전. 계산 GET은 409 |
| `cancelled` | 조기 종료. 계산 GET/재인증은 409 |
| `queued` / `processing` | 접수·처리 중. 계산 조회는 202 |
| `evaluated` | 계산·평가가 확정 저장됨. 계산 조회는 200 |
| `outcome_unknown` | 실행·저장 뒤 결과를 확정할 수 없음. 계산 조회는 503, 임의 재호출 안 함 |
| `failed` | 입력 보관 검증·계약·계산의 확정 오류. 계산 조회는 지정 503 |

`outcome_unknown`은 반드시 영구 최종 상태라는 뜻이 아니다. 이미 수행한 같은 호출의 저장된 candidate를 찾으면 회복할 수 있지만 계산 성공이나 실패를 추측하여 완료 처리하지 않는다.

일반 Mock 오류 body는 `{ "error": { "code": string, "message": string, "request_id": string } }`다. `/cpr-analysis`의 기존 측정 parser/validator 오류는 400 `{type:"client_error",message:...}`이고, 기존 attempt 계산 경로의 대응 오류는 422 `MEASUREMENT_INPUT_INVALID`다. HTTP 수신부가 먼저 거절한 400/413은 JSON이 아닐 수 있으므로 앱은 HTTP 상태부터 확인한다. `request_id`는 Lambda context 값이며 context가 없으면 `local`이다. 서버 내부 오류 원문·token·upstream body는 응답 메시지에 넣지 않는다. 개별 경로가 아래 오류를 전부 발생시키는 것은 아니며 해당 인증/상태/데이터 조건에 따라 달라진다.

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

`GET /mock/v1/attempts/{attempt_id}`는 failed/unknown도 200 상태 object로 알려준다. 이때 `error`는 code/message만 있고 request_id는 없다. 동일 상태에서 `/calculation`은 해당 503 오류 envelope다. HTTP 503만 보고 모든 오류를 즉시 반복 계산하는 동작은 이 계약에 없다.

## 4. 역할과 저장·배포 경계

| 역할 | 진입점 | 필요한 의존성과 실제 동작 |
|---|---|---|
| API | `mock_journey.handler.run` | 세션·진도·attempt·job DB, resume keyring, 실행 정의·projection, 입력/결과 S3. 계산 실행과 분리하여 접수·조회하며 SQS로 직접 발행하지 않음 |
| Worker | `mock_journey.worker.run` | SQS가 넘긴 job_id, DB 거래·lease, S3 저장, 버전별 검증 adapter. 앱 Bearer/resume keyring은 받지 않음 |
| Relay | `mock_journey.dispatch.run` | Stream key/sequence 또는 `source=aws.events` 재조정, DB due query/거래, SQS send. S3·계산기·앱 keyring은 요구하지 않음 |

로컬 조립에는 `mock_journey.assembly.build_application`, `build_worker`, `build_relay`를 사용할 수 있다. 호출자가 client·legacy binding·설정·keyring·실행 정의·버전을 명시하며, 각 함수는 해당 역할의 기존 구성 요소를 연결한다. 필요한 client나 legacy binding이 `None`이면 조립 단계에서 거절한다. SDK 연결을 시도하거나 실제 접근 권한을 검사하는 기능은 아니다.

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
`parse_body_as_action` 같은 라이브러리 함수의 존재가 이 POST API에 JSON action 입력 경로를 추가한다는
뜻은 아니다.

### 2.3 패킷 검출과 앱 기록 계약

2026-09-11 사용자 확정 D38~D46을 AHA2020/ARC2020/ARC2025/ERC2020/STD2015 모두에 적용한다. guideline별 점수식·최소량과 Mock attempt의 ARC2025 고정 조건은 별개다.

- 첫 패킷은 압박 카운터 기준선이다. 앱은 첫 압박 전에 기준선 패킷을 넣는다. 이후 이전 값과 다른 양수 카운터를1회로 인정하며, 증가 폭으로 유실된 압박을 추정 복원하지 않는다.0은 새 기준선이며 사건을 만들지 않는다.
- 패킷의 두 호흡량 중 최대값을 사용하고 후보별 최고값을 추적한다. 성인·소아 최고값 대비10mL 이상 하강한 두 연속 패킷에서1회 확정한다. 원본 보정계수10이므로 raw50→49→49가1회다. 영아는 보정계수1, 감소5mL 잠정값이다. 공식 의학·기기 기준으로 확정한 수치가 아니다.
- 100→90→89,100→90→90,100→89→90,100→0→0은 모두1회다. 중간 패킷이 감소 조건을 벗어나거나 최고값이 갱신되면 연속 확인을 처음부터 다시 한다. 새로운 최고값을 이전 호흡에서 가져오지 않는다.
- 확정 후에는 대표량0 또는 패킷별 최대 압박 깊이의 상승 시작으로 재준비한다. 이후 새 호흡량 상승부터 후보를 시작하며 재준비한 패킷을 새 후보에 재사용하지 않는다. 아직 확인 중인 호흡을 압박 시작 때문에 취소하지 않는다. 깊이에 새 잡음 임계값을 추가하지 않는다.
- 파일 끝은 추가 관측이 아니다. 두 패킷을 확인하지 못한 마지막 호흡은 길이에 관계없이 추가하지 않는다. 이미 확정한 마지막 호흡을 중복 추가하지 않는다.
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
새 공식 평가 기준이나 ARC 연동 승인을 의미하지 않는다. P3에 따라 비ARC guideline에는 추가 CPR 최소량 null 예외를 적용하지 않는다.

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
ARC 과정 완료를 뜻하지 않는다. 생성한 HSTM 문서 전체를 새 응답 키로 노출하는 변경도 승인된 것이 아니다.

대표 점수 키는 `score_comp_depth`, `score_comp_rate`, `score_comp_no`, `score_comp_count`,
`score_recoil`, `score_hand_position`, `score_vent_vol`, `score_vent_rate`, `score_vent_count`,
`score_vent_speed`, `score_ccf`, `score_rescue_vent`, `overall`이다. **훈련종류·calc_case·집계 레벨에 따라
키 존재 여부와 0/null이 다르므로 단일 고정 키 집합을 모든 레벨에 적용하지 않는다.**

reference의 HTTP 변환은 `total_score.score_rescue_vent`를0으로 설정한다. 계산 core와 part/cycle의
rescue 값·누락 여부가 이 HTTP 값과 같다는 뜻은 아니다. `score_vent_rate_measured`는 total의 코칭
내부 신호로 응답 직전에 제거된다.

metrics에는 지표 분포 객체, 평균·합계·횟수·CCF 값 등이 들어간다. `VentilationSpeed` 등은 조건에 따라
객체·빈 객체·null이 다를 수 있다. score가 null이라는 이유로 해당 실측 평균까지 일괄 null로 바꾸지 않는다.

## 5. CPR 최소량 예외와 원본 동작 복원

| CPR target | 압박 최소량 | 호흡 최소량 |
|---|---:|---:|
| adult | 90 | 6 |
| child | 90 | 6 |
| infant | 45 | 6 |

**ARC2020/ARC2025의 CPR에만 적용한다.** 기준 이상은 해당 최소량 조건 충족, 미만은 해당 점수 그룹 null이다.
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

P1에 따라 기존 ARC의 입력 검증·오류 정제·Sentry 보호를 유지한다. 내부 회귀 helper의 대표 오류는 `This guideline is not supported.`, `This target is not supported.`, `This training type is not supported.`, `CPR file is required.`, `Invalid request data.`다. 공개 `/cpr-analysis`는 기존 parser/validator 오류에 정제된 400 `{type:"client_error",message:...}`를 유지하고 기존 attempt 경로는 422 `MEASUREMENT_INPUT_INVALID`를 반환한다. 인증·ID·소유권·입력/조건 충돌·크기 오류는 양쪽 모두 Mock 오류 계약이다. 입력 검증 뒤 projection 오류도 Mock 422다. 공개 계산 경계의 실행/저장 오류는 현재 API 계약의 정제된 503이며 내부 helper의 모든 500 표현을 공개 응답으로 약속하지 않는다.

정상적인 multipart가 아닌 요청은 폼 파서로 가므로 비multipart라는 이유만으로400을 반환하지 않는다.
무한대 합격선의 정수 변환 오류, Sentry 초기화 장애, 응답 직렬화 실패 처리는 기존 ARC 동작을 유지한다.
일반 ValueError는 정제 문구로 응답하며, Sentry 초기화 실패 때문에 계산을 중단하지 않는다.

기존 ARC 원본·차트 저장과 Sentry 보호는 유지한다. 내부 진단 uploader의 저장 실패와 attempt API의 영속 접수·완료 저장은 다른 책임이다. 실제 입력 보관 확인 없이 202로 성공 접수라고 알리지 않는다. presigned URL의 코드 기본300초는 데이터 보존기간이나 로그아웃 즉시 철회 보장이 아니다.

요청 최대 크기는 Gateway·Lambda의 전체 페이로드와 multipart/URL/base64 오버헤드에 좌우된다.
원격 환경에서 검증한 최대 바이너리 크기·세션 길이는 아직 없다. 단위 테스트 결과만으로
모바일 codec, AWS 권한, 실제 앱 Journey 또는 ARC 연동이 검증되었다고 표시하지 않는다.


<a id="d-vcc-구현-착수용-내부앱-계약-v1--미구현"></a>

## 새 과정 API 계약의 위치

`/api/v2`는 [APP_API Markdown 명세](APP_API.md)를 따른다. 내부 DTO·저장 거래·복구 불변조건은 [ARCHITECTURE](ARCHITECTURE.md), 확정 정책과 외부 계약 대기는 [DECISIONS](DECISIONS.md)에 둔다. 이 문서의 snake_case 응답과 취소 사유를 v2 camelCase 제어 API에 그대로 사용하지 않는다. `calculation` 안의 기존 계산 JSON은 원래 자료형·null·키를 유지한다.
