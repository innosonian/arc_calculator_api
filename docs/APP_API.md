# 앱 개발자용 로컬 Journey API

2026-09-10 구현 기준. 로그인 → 프로그램 선택 → 누적 실측 업로드 → 계산·차트·별도 완료 확인에 필요한 계약이다.

## 공통 규칙

- 로그인과 발급된 차트 URL의 직접 조회를 제외한 아래 API는 `Authorization: Bearer <session_token>`을 요구한다.
- 제어 요청의 `Content-Type`은 `application/json`이다. 계산 업로드 형식은 아래 설명을 따른다.
- `session_token`·`resume_credential`은 URL, query, 로그에 넣지 않는다. API 경로에 query를 추가하지 않는다.
- 문자열·숫자·bool·`null`을 서로 바꾸지 않는다. 계산 점수·통계·코칭의 기존 자료형을 유지한다.

## 1. 로그인 — `POST /mock/v1/sessions`

필수 JSON:

```json
{"login_id":"test@test.com","password":"2222"}
```

성공 `201`. `session_token`: string, `session_id`: string, `expires_in`: integer `86400`, `expires_at`: UTC string을 받는다. 비밀번호는 숫자가 아닌 **문자열**이다. 이 Dummy 계정만 지원하며 세션은 로그인부터 24시간 유효하다.

`GET /mock/v1/session`은 현재 세션 확인용이며 성공 `200`이다. 같은 Dummy 계정으로 여러 세션을 만들 수 있지만, 다른 세션의 attempt를 자동으로 사용할 수 있는 것은 아니다. 앱 재실행 시 재로그인은 앱이 처리하며 서버가 재실행을 감지하지 않는다. 서버의 24시간 유효기간과 구별한다.

## 2. 프로그램·공유 진도 — `GET /mock/v1/programs`

성공 `200`. 응답의 `catalog_version`, `programs[].id`, `programs[].supported_targets`에서 선택 값을 가져온다. 현재 `catalog_version`은 `mock-catalog-v1`, `profile_name`은 `tester`다.

| `programs[].id` | 프로그램 | 목표 |
|---|---|---|
| `mock-cpr` | CPR Training | 3 cycles |
| `mock-compression-only` | Chest Compression Only | 압박 60회 |
| `mock-ventilation-only` | Ventilation Only | 호흡 8회 |
| `mock-two-rescuer-cpr` | 2-Rescuer CPR | 8 cycles |
| `mock-two-rescuer-aed` | 2-Rescuer CPR with AED-T | 10 cycles |

모두 `adult`, `child`, `infant`를 지원한다. 연령별 `progress_by_target`은 `completed` 또는 `not_completed`, `active_attempts_by_target`은 열린 시도 수다. **현재 공유 진도는 이 API를 다시 조회해서 확인**한다. `progress_epoch`는 초기화 구분 값, `progress_version`은 갱신 구분 값이다.

## 3. 훈련 시도 생성 — `POST /mock/v1/attempts`

필수 JSON 예시:

```json
{
  "client_request_id": "<앱이 새로 만든 요청 식별자>",
  "catalog_version": "mock-catalog-v1",
  "program_id": "mock-cpr",
  "target": "adult"
}
```

네 필드 모두 string이다. `client_request_id`는 새 시도마다 새 값을 만들며 UUID를 권장한다. 서버 한도는 UTF-8 256 bytes다. **같은 세션에서 생성 요청을 재전송할 때 같은 식별자와 같은 JSON 값을 사용**한다. 최초 `201`, 같은 요청 재전송 `200`, 같은 식별자에 다른 내용은 `409 IDEMPOTENCY_CONFLICT`다.

응답에서 다음 값을 보관한다. 계산 요청의 ID는 앱이 새로 만드는 것이 아니라 이 응답에서 받는다.

| 응답 필드 | 사용 방법 |
|---|---|
| `attempt_id`: string | 이 시도의 업로드·상태·결과 조회에 계속 사용 |
| `resume_credential`: string | 만료/로그아웃 뒤 같은 시도에 다시 연결할 때 필요. 일반 시도 조회에는 다시 나오지 않음 |
| `calculation_path`: string | 계산 업로드와 최종 결과 조회 경로 |
| `condition`: object | 계산 요청에 값과 자료형을 그대로 전달 |
| `goal`: object | `kind`와 integer `required`로 목표 표시 |

같은 프로그램·연령이 이미 완료됐다면 새 시도는 `409 PROGRAM_ALREADY_COMPLETED`다. 재전송 식별자는 세션을 넘어 자동 복원하는 수단이 아니다.

## 4. 누적 실측 업로드 — `POST /cpr-analysis`

필수 헤더는 `Authorization: Bearer <session_token>`, `X-Attempt-ID: <attempt_id>`다. ID의 대소문자·형식을 바꾸지 않는다.

`Content-Type: multipart/form-data; boundary=...`로 다음 part를 보낸다. HTTP 라이브러리가 실제 body에 맞는 boundary를 생성하게 한다.

| part 이름 | 필수 | 형식 |
|---|---|---|
| `rawHexBPfile` | 예 | 훈련 종료 후 누적한 CPR 원본 binary 파일 bytes |
| `condition` | 예 | 생성 응답의 object를 JSON 문자열로 직렬화 |
| `aedHexBPfile` | 아니요 | 수집한 AED binary 파일 bytes |
| `vp_event_list` | 아니요 | 기존 VP 이벤트의 JSON array 문자열. 기존 key `event`, `timestamp`, `last_timestamp` 유지. 없으면 생략 또는 `[]` |

현재 조건은 `ARC2025`, 성인·소아 비율 문자열 `302`, 영아 문자열 `152`다. `is_2rescuers`의 bool을 0/1로 바꾸거나 `condition`을 앱에서 다시 구성하지 않는다.

기존 base64 form도 지원한다. binary를 `cpr_b64_data`/선택 `aed_b64_data`의 URL-safe base64 문자열로 넣고, `condition` 등 기존 form 필드와 함께 **form URL 인코딩 → 전체 percent 인코딩 → 전체 URL-safe base64 인코딩**한 body를 보낸다. `Content-Type: application/x-www-form-urlencoded` 또는 기존 Content-Type 생략 방식을 유지하며, 일반 JSON body로 바꾸거나 한 번 더 base64 인코딩하지 않는다.

기존 `POST /mock/v1/attempts/{attempt_id}/calculation`도 같은 작업을 수행한다. 이 경로에서는 `X-Attempt-ID`를 생략할 수 있다. 전달한다면 경로의 ID와 같아야 한다. 둘 중 어느 경로로 재전송하든 **동일한 attempt와 동일한 측정·조건**을 사용한다.

## 5. 계산 결과 — `GET /mock/v1/attempts/{attempt_id}/calculation`

| HTTP 상태 | 의미와 다음 요청 |
|---|---|
| 업로드 `202` | 영속 접수 완료, 아직 처리 중. 같은 `calculation_path`를 GET |
| 업로드 또는 조회 `200` | 계산 완료. 기존 계산 JSON을 그대로 사용 |
| 조회 `202` | 아직 처리 중. 새 attempt를 만들지 않고 같은 경로로 다시 조회 |

`202` 응답의 `attempt_id`, `state`, `status_path`는 시도 상태를 가리킨다. `status_path`는 별도 시도 평가 경로이며 계산 JSON을 반환하는 경로가 아니다. `wait_expired=false`는 서버가 30초 경과 여부를 판정했다는 뜻이 아니다. 앱은 계산·제출을 합한 대기를 최대 30초로 관리하고, 이후에도 같은 ID로 결과를 다시 조회할 수 있다. 업로드 시작과 수신 완료 중 어느 시점부터 셀지는 Q23 미정이며 이 문서에서 새로 정하지 않는다. 30초가 지나도 서버 작업이 자동 취소되지는 않는다.

`200`에는 기존 계산 필드와 다음 제출 상태가 있다. 계산 성공과 ARC 제출 성공을 구별한다.

```json
{"submit_arc":{"status":"disabled","ok":false,"error":"arc_contract_pending"}}
```

같은 입력을 다시 POST하면 처리 중에는 202, 확정 후에는 200·같은 결과를 받는다. 다른 측정은 `409 ATTEMPT_INPUT_CONFLICT`이며 기존 결과를 덮지 않는다. 전송 결과를 알 수 없으면 별도 시도 상태를 확인한다. `created`라면 같은 측정을 다시 POST할 수 있고, `queued`/`processing`이면 계산 결과를 GET한다.

## 6. 별도 완료 확인 — `GET /mock/v1/attempts/{attempt_id}`

성공 `200`. `state=evaluated`는 계산·평가가 저장됐다는 뜻이다. 평가 전에는 `evaluation`과 `progress_application`이 `null`이다. 프로그램 완료는 `evaluation`을 확인한다.

| 필드 | 의미 |
|---|---|
| `evaluation.score.decision` | 기존 tester 판정인 문자열 `pass` 또는 `fail` |
| `evaluation.goal.status` | `evaluated` 또는 `pending_policy` |
| `evaluation.goal.observed`, `met` | 판단된 횟수·목표 충족 여부. CPR 정책 대기는 둘 다 `null` |
| `evaluation.program_completed` | bool. Only는 목표+Pass 모두 충족할 때 true |
| `evaluation.reason_codes` | 문자열 배열. CPR 정책 대기는 `GOAL_POLICY_UNRESOLVED`를 포함하며, 점수 미충족이면 `SCORE_NOT_PASS` 등이 함께 올 수 있음 |

CPR은 계산 결과를 제공하되 완전한 cycle 완료 규칙이 미정이라 `pending_policy`·`program_completed=false`다. **계산 JSON의 점수나 null을 이 상태 때문에 바꾸지 않는다.** 로그아웃 전 시도의 결과는 남아도 새 공유 진도에는 반영되지 않는다. `progress_application.reason=PROGRESS_RESET`을 구별하고 최신 목록은 프로그램 API로 확인한다.

## 7. 차트 조회와 링크 갱신

계산 결과의 `chart_dataset_url`이 있으면 **받은 URL 그대로 GET**한다. URL 자체가 300초짜리 접근 권한이므로 세션 헤더는 필요 없다. 경로·서명·query를 변경하거나 로그에 기록하지 않는다. 로그아웃해도 이미 발급된 URL의 남은 유효시간은 유지된다.

`GET /mock/v1/attempts/{attempt_id}/chart-link`는 이 시도에 연결된 유효한 Bearer를 요구한다. evaluated 시도에서 성공 `200`, 응답은 `attempt_id`: string, `chart_dataset_url`: string|null, `expires_at`: UTC string|null이다. 새 링크를 발급해도 저장된 계산 응답은 바뀌지 않는다. 아직 계산 전이면 `409 INVALID_STATE`다.

## 8. 조기 종료·재인가·로그아웃

| Method / Path | 필수 JSON | 성공·제약 |
|---|---|---|
| `POST /mock/v1/attempts/{attempt_id}/cancel` | `{"reason":"user_stopped"}` 또는 `{"reason":"manikin_disconnected"}` | `204`. 측정을 접수하기 전 created 시도만 취소하며 이미 취소된 시도의 재요청도 204. 계산 작업 취소 API가 아님 |
| `POST /mock/v1/attempts/{attempt_id}/reauthorize` | `{"resume_credential":"<생성 시 받은 증표>"}` | 새 Bearer로 `200`. 같은 사용자이며 이전 연결 세션이 만료/폐기된 경우만 새 세션으로 이전. 다른 이전 세션이 유효하거나 시도가 cancelled면 `409`. 이미 현재 세션에 연결된 동일 재요청은 `200` |
| `DELETE /mock/v1/session` | 없음 | `204`. 이 세션 로그아웃과 Dummy 계정 전체 공유 진도 초기화 |

재인가에는 저장한 `attempt_id`와 원래 `resume_credential`이 필요하다. 새 로그인 후 재인가하고 같은 측정을 전송하거나 기존 결과를 조회한다. 재인가가 이전 시도의 `progress_epoch`를 새 값으로 바꾸지는 않는다. 다른 활성 세션은 로그아웃되지 않으며 목록을 다시 조회해 초기화된 공유 진도를 확인한다.

## 대표 오류

API 처리 중 일반 오류는 `{"error":{"code":"...","message":"...","request_id":"..."}}` 형식이다. `/cpr-analysis`의 측정 파싱 오류는 기존 `{"type":"client_error","message":"..."}` 형식을 유지한다. HTTP parser가 먼저 거절한 400/413은 JSON이 아닐 수 있으므로 HTTP 상태를 먼저 확인한다.

| HTTP | 대표 code / 처리 |
|---|---|
| `400` | `INVALID_REQUEST`, 또는 별칭의 안전한 측정 파싱 오류 |
| `401` / `403` | `SESSION_REQUIRED`, `SESSION_EXPIRED` / `SESSION_REVOKED`: 재로그인·필요 시 재인가 |
| `404` | `NOT_FOUND`: 없는 시도 또는 다른 세션 소유 시도. 차트 만료/변조도 404 |
| `409` | `IDEMPOTENCY_CONFLICT`, `ATTEMPT_INPUT_CONFLICT`, `PROFILE_MISMATCH`, `PROGRAM_ALREADY_COMPLETED`, `INVALID_STATE` |
| `413` / `422` | 크기 초과 / 기존 attempt 계산 경로의 `MEASUREMENT_INPUT_INVALID` |
| `503` | 일시 실행/저장 장애 또는 계산 실패. 같은 시도 상태·오류 code를 확인 |

실제 기본 CLI·기록된 실측 바이너리로 15개 조합과 재전송·진도·차트를 검증했다. 실제 앱 화면/실물 마네킨 연결 인수는 별도이며, CPR 완료 규칙은 대기 중이고 ARC 제출은 비활성이다.
