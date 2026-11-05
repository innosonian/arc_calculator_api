# 앱 개발자 전달용 VCC API

2026-09-18 구현을 기준으로 2026-09-19 정리하고 2026-09-28 `/mock/v1` 삭제(D103)와 코드 리뷰 후속 결정을 반영한 **앱팀 전달용 Markdown 명세**다. 이 파일 안에 `/api/v2`의 **15개 경로·18개 동작, 요청·응답 자료형, 필수값·null, 전체 오류, JSON 예시**를 모았다. 별도 YAML 없이 읽고 구현할 수 있다. 예시는 합성 자료이며 실제 ARC 과정·계정·서버 주소가 아니다.

**연결할 서버는 백엔드팀과 먼저 맞춘다.** `/api/v2`가 최종 버전이다(D103). 이 코드의 로컬·AWS Dev 조립은 아래 Dummy Dev 과정 4개를 제공한다. 이 명세 작성이나 로컬 검증 완료가 배포 완료를 뜻하지 않는다. 실제 ARC/MuleSoft 인증·자료 공급·결과 전송 계약, 실물 앱·AWS 인수는 남아 있다. ARC 송신은 비활성이다.

Round 2 Dummy Dev는 실제 업로드 바이너리를 계산하고 CPR 목표를 `evaluated`로 반환한다. 과정 유형별로 아래 연습을 순서대로 배치하고 마지막 skill의 평가를 한 개 붙인다. `PALS`는 유형만 예약한다. Dummy 과정에는 영상·문서가 없다. 아래 콘텐츠 계약은 콘텐츠를 제공하는 과정에 적용한다.

| 과정 유형 | Dummy 과정 ID | skill 순서 |
|---|---|---|
| `BLS` | Adult `911001`, Infant `911002` | 압박 60, 호흡 12, CPR 3 cycle, 2인 CPR 8 cycle, 2인 CPR+AED 8 cycle |
| `CPR_AED` | Adult `911003`, Infant `911004` | 압박 30, 호흡 5세트 × 2회, CPR 3 cycle, AED 후 CPR 1 cycle |

이전 카탈로그와 공개 ID 및 `definitionHash`가 다르다. 세션을 갱신한 뒤 목록과 상세를 다시 받아 그 ID·hash로 새 시도를 시작한다. 이미 시작한 시도는 저장된 실행 정의를 사용한다. Dummy 결과는 `submit_arc.status="excluded"`, `ok=false`, `error=null`이다. 기본 제외 사유는 `dummy`이며 초기화 전 결과 등에는 해당 사유가 추가된다.

**2026-09-23 AWS Dev 연결 정보**

| 항목 | 값 |
|---|---|
| 환경 | Dummy Dev · 오하이오 `us-east-2` |
| 서버 주소 | `https://2ftxmdtrx1.execute-api.us-east-2.amazonaws.com/dev` |
| 로그인 요청 | `POST https://2ftxmdtrx1.execute-api.us-east-2.amazonaws.com/dev/api/v2/sessions/` |
| Dummy 로그인 | `test@test.com` / 비밀번호 문자열 `2222` |
| 초기 업로드 원본 한도 | CPR+AED 합계2MiB(2,097,152 bytes) |
| 초기 Gateway 요청량 | 초당10회·burst20. 엄격한 처리량/비용 보장이 아니며429 처리 필요 |

사용자 실행으로 HTTPS 로그인/조회, 기존 압박 자료31380bytes의 일반 훈련·최종평가 계산(각101회·100점)과 과정 FINISHED를 확인했다. 일반 훈련 원본/최종 결과/운용 로그·서명 차트 다운로드·익명 접근403도 확인했다. 같은 계정의 다른 유효 세션에서는 기존 훈련·계산 결과·차트 발급이 모두404로 차단됐고 원래 세션의 결과는 보존됐다. 차트의 새 링크200→실제300초 경과 후 같은 링크의 만료403→재발급200·동일 차트와 기존 결과 보존도 확인했다. 완료한 일반 훈련의 동일 시작 요청·동일 파일/조건을 순서대로 재전송했을 때 각각200으로 같은 응답을 반환하고 기존 훈련·작업·접수 기록·결과를 보존함도 확인했다. 이 확인 범위에서 앱팀의 Dummy Dev 연결 시험을 시작할 수 있다. 앱의 실제 파일·기기와 다른 훈련 종류, 동시/처리 중 재전송·세션 만료/복구·장애·용량/비용을 포함한 전체 AWS 인수는 진행 중이다. 접속 주소의 `/dev`와 각 API의 마지막 `/`를 유지한다.

2026-09-23에 시험한 이전 성인 압박 Only 과정은 당시 공유 진도에서 완료됐다. 완료한 일반 훈련과 합격한 최종평가도 다시 시작할 수 있으며(D130) 이미 얻은 완료·합격은 유지된다(D131). Dummy 진도 초기화는 `POST /api/v2/dev/progress-reset/`로만 수행한다. Dummy·실학생 모두 로그아웃은 해당 세션만 폐기하며 공유 진도를 유지한다. 과정·등록·항목 ID는 아래 일반 계약대로 실제 목록/상세 응답에서 사용한다.

목차: [1. 공통 계약](#1-공통-계약) · [2. 경로·세션·과정·TrainingProgram](#2-경로와-기본-흐름) · [3. 시작·멱등성](#3-시작과-재전송) · [4. 콘텐츠 보고](#4-영상문서-진도-보고) · [5. 측정·업로드](#5-측정-시도와-업로드) · [6. 계산·완료·차트](#6-결과완료차트) · [7. 취소·복구·오류](#7-취소복구전체-오류) · [8. 기존 앱 이행](#8-기존-앱에서-바뀌는-지점)

## 1. 공통 계약

- 모든 새 경로의 마지막 `/`가 필수다. 빠뜨리면 redirect 없이 `404`; 알려진 경로의 잘못된 method는 `405`다.
- API Gateway stage를 사용하는 서버 주소에는 stage 경로까지 포함한다. 예를 들어 서버 주소가 `https://example.execute-api.us-east-2.amazonaws.com/dev`이면 `/api/v2/session/`의 요청 주소는 `https://example.execute-api.us-east-2.amazonaws.com/dev/api/v2/session/`이다. 목록의 `next`·`previous`도 API 기준 경로이므로 서버 주소 뒤에 연결하며 `/dev`가 사라지는 일반 URL 루트 병합을 피한다. 서버 주소는 백엔드팀이 전달한 고정 주소를 사용한다.
- 로그인 외 모든 API는 `Authorization: Bearer <accessToken>`을 요구한다. 발급받은 차트 URL 직접 GET만 예외다. 토큰·복구 증표·서명 URL을 로그, query, 분석 수집기에 기록하지 않는다.
- 제어 요청은 UTF-8 `application/json`이다. 필수 키 누락, 알 수 없는 키, 중복 키, `NaN`/`Infinity`, 잘못된 자료형은 거절한다. GET·DELETE에는 body를 보내지 않는다. 계산 업로드 형식은 5절을 따른다.
- 허용된 query 외에는 보내지 않는다. 중복·빈 값·다중값을 허용하지 않는다. 숫자 query는 선행 `0`, `+`, 공백 없는 양의 십진 문자열이다.
- 공개 ID는 `1..9007199254740991`의 JSON integer다. UUID는 소문자, 정의 hash는 소문자 hex 64자리, 시간은 UTC RFC3339다. 문자열·숫자·boolean·`null`을 임의 변환하지 않는다. `null`은 키 생략과 다르다.
- 기본 세션 TTL은 로그인부터 24시간이다. 새 세션을 발급할 때 설정한 상한이 더 짧으면 상한을 적용한다. 기존 세션의 만료 시각은 `expiresAt`이며 조회·refresh로 연장하지 않는다. 앱 재실행 시 재로그인은 앱이 처리한다. 같은 계정도 다른 세션에 연결된 시도는 자동 조회할 수 없다.

표의 자료형은 아래 뜻이다. **필드 표와 예시에 나온 DTO 키는 별도 선택 표기가 없으면 전부 필수**이며, nullable 키도 생략하지 않고 null로 포함한다. 제어 요청·일반 응답 DTO는 명시한 키 집합을 사용한다. 계산기의 조건별 가변 키와 추가 키는 6절의 예외를 따른다.

| 표기 | 정확한 계약 |
|---|---|
| `Id` | integer, `1..9007199254740991`. 숫자 문자열·boolean 불가 |
| `Uuid` | 소문자 canonical UUID 문자열, `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` 형태의 hex |
| `Hash` | 소문자 SHA-256 hex 문자열 64자리 |
| `Utc` | UTC RFC3339 string, 끝 `Z`. 소수초가 있을 수 있음 |
| `integer` / `number` | JSON 정수 / 정수 또는 소수. boolean·문자열과 구별 |
| `T 또는 null` | 키는 필수이고 값만 nullable. 상위 객체가 null이면 그 하위 키는 없음 |
| `T[]` | 배열. 별도 최소 길이 설명이 없으면 빈 배열 가능 |

성공은 다음 envelope다. `204`만 본문이 없다. 응답 헤더는 `Cache-Control: no-store`다. `X-Request-Id`는 API가 요청마다 새로 만드는 소문자 UUID이며 API가 처리한 성공·오류 응답에 모두 있다. 서버 운용 로그에 `http_request_id`로 함께 기록되므로 문의할 때 이 값을 전달하면 같은 요청의 기록을 찾을 수 있다(D107). 본문에는 들어가지 않고, 앱이 보내는 `clientRequestId`와 다르며, 앱이 요청 헤더로 보낼 필요는 없다. 로컬 서버는 응답에 이 헤더를 싣지 않는다.

```json
{
  "success": true,
  "data": {"각 API의 응답 필드": "해당 절의 DTO와 예시 참조"},
  "message": "OK",
  "timestamp": "2026-09-18T00:00:00Z"
}
```

오류는 HTTP 상태와 `error.code`로 처리한다. 메시지 문자열로 분기하지 않는다.

```json
{
  "success": false,
  "error": {"code": "DEFINITION_CHANGED", "message": "The course definition has changed.", "details": null},
  "timestamp": "2026-09-18T00:00:00Z"
}
```

HTTP 서버가 요청 자체를 먼저 거절한 `400`/`413`은 이 JSON 형식이 아닐 수 있다. JSON을 읽기 전에 HTTP 상태·Content-Type을 확인한다.

## 2. 경로와 기본 흐름

아래 응답 이름은 이 문서의 공용 DTO 이름이며 실제 응답은 `data` 안에 들어간다. 요청의 모든 필드는 필수다. 표의 `?`만 선택값이다. 모든 path ID도 필수이며 `courseId`·`courseItemLinkId`는 `Id`, `attemptId`는 `Uuid`다. query `page`·`enrollmentId`의 최대값은 `9007199254740991`이다.

| Method / 경로 | 요청 | 성공의 `data` |
|---|---|---|
| `POST /api/v2/sessions/` | `loginId`, `password` 또는 2.1절의 OIDC 대안 | `201 LoginSession` |
| `GET /api/v2/session/` | 없음 | `200 Session` |
| `POST /api/v2/session/refresh/` | 정확히 `{}` | `200 Session` |
| `DELETE /api/v2/session/` | 없음 | `204` |
| `POST /api/v2/session/organization/` | `organizationId`: string | `200` `{"organizationId":"<선택 ID>"}` |
| `POST /api/v2/dev/progress-reset/` | `clientRequestId`: `Uuid` | Dummy `204` |
| `GET /api/v2/courses/progress/` | query `page?=1`, `pageSize?=100` / 최대 1000 | `200 CoursePage` |
| `GET /api/v2/courses/{courseId}/progress/` | query `enrollmentId` | `200 CourseDetail` |
| `GET /api/v2/courses/{courseId}/items/{courseItemLinkId}/` | query `enrollmentId` | `200 ItemDetail` |
| `POST /api/v2/learning-starts/` | `StartRequest` | 최초 `201`, 재전송 `200 ContentStart` |
| `PUT /api/v2/courses/{courseId}/progress/` | `ContentReport` | `200 ProgressReceipt` |
| `POST /api/v2/attempts/` | `StartRequest` | 최초 `201`, 재전송 `200 CreatedAttempt` |
| `GET /api/v2/attempts/{attemptId}/` | 없음 | `200 Attempt` |
| `POST /api/v2/attempts/{attemptId}/reauthorize/` | `resumeCredential`: string | `200 ReauthorizedAttempt` |
| `POST /api/v2/attempts/{attemptId}/cancel/` | `reason`: 아래 두 값 중 하나 | `204` |
| `POST /api/v2/attempts/{attemptId}/calculation/` | 누적 측정 multipart | `202 CalculationPending` 또는 `200 CalculationSucceeded` |
| `GET /api/v2/attempts/{attemptId}/calculation/` | 없음 | `202 CalculationPending` 또는 `200 CalculationSucceeded` |
| `GET /api/v2/attempts/{attemptId}/chart-link/` | 없음 | `200 ChartLink` |

앱의 순서는 **로그인 → 배정 과정 목록 → 해당 등록의 과정 상세 → 배치별 항목 상세 → 콘텐츠 또는 측정 시작 → 근거 보고 또는 계산 업로드 → 결과와 최신 과정 진도 확인**이다. 퀴즈 API는 없다.

### 2.1 로그인과 학습 대기

```json
{"loginId":"test@test.com","password":"2222"}
```

현재 로그인은 위 Dummy만 제공한다. 비밀번호는 숫자가 아닌 문자열이다. 다른 `loginId`는 실제 학생 인증 계약 대기로 `503 CONTRACT_PENDING`이며, Dummy 비밀번호 오류는 `401 LOGIN_FAILED`다.

OIDC 대안의 필수 키는 아래 세 개다. 두 토큰은 비어 있지 않은 문자열이다. 이 요청은 `503 CONTRACT_PENDING`을 반환한다. 서버는 토큰을 검증하거나 저장하지 않는다.

```json
{"grant":"arc_oidc","idToken":"<ARC ID token>","accessToken":"<ARC access token>"}
```

로그인의 `data`:

```json
{
  "sessionId": "60000000-0000-4000-8000-000000000001",
  "expiresAt": "2026-09-19T00:00:00Z",
  "learningAvailability": {"state":"waiting","reason":"contract_pending"},
  "accessToken": "<발급된 불투명 토큰>",
  "tokenType": "Bearer",
  "userName": "Test User"
}
```


`LoginRequest`의 `loginId`·`password`는 빈 문자열을 허용하지 않는다. `Session`의 `sessionId`는 `Uuid`, `expiresAt`은 `Utc`, `learningAvailability`는 아래 상태 객체다. `LoginSession`만 빈 문자열이 아닌 `accessToken`, 고정 문자열 `tokenType="Bearer"`, 표시 이름 `userName`(string)을 추가한다. Dummy 계정의 `userName`은 고정 문자열 `"Test User"`다(D129). 아래 세 필드를 세션 GET/refresh에 기대하지 않는다.

`GET /api/v2/session/`과 `POST /api/v2/session/refresh/`는 기존 `sessionId`, `expiresAt`, `learningAvailability`에 다음 키를 더한다. refresh는 공급자 자료를 갱신하며 토큰을 갱신하지 않는다.

로그인 응답(`LoginSession`)에는 `organizations`, `organizationId`, `inactivityLogoutSeconds`, `hardCapSeconds`가 없다. 이 네 키는 GET/refresh에만 있다. 로그인 직후 조직 선택이 필요하면 `GET /api/v2/session/`을 한 번 호출한다. 서버의 비활동·상한 기본값은 null이며 앱의 별도 타이머 기본값을 뜻하지 않는다.

| 추가 필드 | 자료형 |
|---|---|
| `organizations` | `[{id: string, name: string}]`. Dummy는 빈 배열 |
| `organizationId` | string 또는 null |
| `inactivityLogoutSeconds` | 양의 integer 또는 null. 앱이 적용하는 비활동 로그아웃 시간 |
| `hardCapSeconds` | 양의 integer 또는 null. 로그인부터의 서버 세션 상한 |

```json
{"organizations":[],"organizationId":null,"inactivityLogoutSeconds":null,"hardCapSeconds":null}
```

`POST /api/v2/session/organization/`에 `{"organizationId":"dev-test"}`를 보내면 현재 세션에 저장하고 `200`과 `{"organizationId":"dev-test"}`를 반환한다. Dummy는 비어 있지 않은 문자열을 허용한다. 실학생의 조직이 한 개면 자동 선택한다. 선택 API에서 학생 소속이 아닌 조직 ID는 `404`다. 새 시작에서 선택한 조직과 등록 조직이 다르면 `404`, 미선택이면 `409 ORGANIZATION_REQUIRED`다. Dummy는 선택 없이 시작할 수 있다.

`POST /api/v2/dev/progress-reset/`은 Dummy 공유 진도를 초기화하고 빈 `204`를 반환한다. 재전송은 같은 활성 세션에서 같은 `clientRequestId`를 사용한다. 동일 요청의 재전송은 `204`를 다시 반환하고 epoch를 다시 바꾸지 않는다. 만료·폐기 세션에는 기존 `401`·`403`을 적용한다. 세션은 유지되며 새 시작 전에 refresh를 호출한다. 비-Dummy는 `404`다. `DELETE /api/v2/session/`은 계정 종류와 무관하게 해당 세션만 폐기하며, 자동 로그아웃도 진도를 초기화하지 않는다.

```json
{"clientRequestId":"60000000-0000-4000-8000-000000000002"}
```

| `learningAvailability.state` | `reason` | 앱 처리 |
|---|---|---|
| `ready` | `null` | 해당 등록의 조건을 확인하고 새 학습 가능 |
| `waiting` | `arc_progress_unavailable` 또는 `contract_pending` | 새 학습 대기. 기존 접수 작업·결과는 보존 |
| `reconciliation_required` | `progress_reconciliation_required` | 진도 의미 확인 필요. 완료나 합격을 앱에서 보정하지 않음 |
| `expired` | `due_date_passed` | `dueDate`가 서버의 현재 UTC 이하인 배정을 목록에 표시. 해당 배정의 새 시작은 `409 ASSIGNMENT_EXPIRED` |

로그인 `201`·refresh `200`이어도 `waiting`일 수 있다. 배정 목록 자체를 확인할 수 없으면 과정 GET은 `503`이며 이를 “배정 없음”으로 표시하지 않는다. 정상적으로 배정 0개인 경우에만 빈 목록과 `ready`다. 배정이 `expired`여도 세션의 집계 상태는 `ready`일 수 있다. 새 시작은 해당 등록의 상태를 다시 검사한다. 자동 refresh 주기나 polling 간격은 아직 계약하지 않았다.

공급 자료의 과정명(`courseName`)이나 항목 제목이 빈 문자열이면 서버는 그 과정의 갱신을 공급 계약 오류로 받지 않고 해당 과정을 `waiting`/`arc_progress_unavailable`로 둔다(D118). 이전에 저장된 과정 자료가 있으면 목록은 이전 제목과 이 대기 상태로 계속 `200`을 반환하며, 과정 하나 때문에 목록 전체를 `503`으로 만들지 않는다.

### 2.2 배정 과정 목록 — CoursePage

`GET /api/v2/courses/progress/?page=1&pageSize=100`. 목록의 단위는 **등록(enrollment)**이며 `(courseId, enrollmentId)` 순으로 정렬한다. 같은 과정의 다른 등록을 합쳐 표시하거나 진도를 공유하지 않는다.

| 필드 | 자료형 / 의미 |
|---|---|
| `results` | `CourseListRow[]`. 로그인 학생에게 배정된 등록 |
| `count` | 0 이상 integer. 전체 배정 등록 수 |
| `next`, `previous` | string 또는 null. 허용 query를 포함한 상대 URL |

`CourseListRow`와 그 `summary[]`의 필드:

| 필드 | 자료형 / 의미 |
|---|---|
| `courseId`, `enrollmentId`, `progressId` | 각각 `Id` |
| `courseName` | 빈 문자열이 아닌 string |
| `courseType` | `BLS`, `CPR_AED`, `PALS`. 이전 정의에서는 생략 가능 |
| `status` | `NOT_STARTED`, `IN_PROGRESS`, `COMPLETION_PENDING_ARC`, `FINISHED`, `CERTIFIED` |
| `certificationType` | string 또는 null |
| `learningAvailability` | 2.1절 상태 객체 |
| `summary` | `SummaryItem[]` |
| `summary[].id` | `Id`. 항목 자체 ID이며 배치 ID가 아님 |
| `summary[].itemType` | `video`, `pdf`, `training`, `assessment` |
| `summary[].title` | 빈 문자열이 아닌 string |
| `summary[].displayOrder` | integer. 응답에 주어진 순서 |

상태는 앱이 점수로 다시 계산하지 않는다. 특히 서버는 로컬 학습 완료만으로 공식 `CERTIFIED`를 만들지 않는다. 두 등록이 있는 합성 과정의 `200` 예시다.

```json
{
  "success": true,
  "data": {
    "results": [
      {
        "courseId": 101,
        "courseName": "VCC Fixture Course",
        "status": "NOT_STARTED",
        "summary": [
          {"id": 201, "itemType": "video", "title": "Orientation Video", "displayOrder": 1},
          {"id": 202, "itemType": "pdf", "title": "Safety Document", "displayOrder": 2},
          {"id": 203, "itemType": "training", "title": "Compression Practice A", "displayOrder": 3},
          {"id": 204, "itemType": "training", "title": "Compression Practice B", "displayOrder": 4},
          {"id": 205, "itemType": "assessment", "title": "Final Ventilation Assessment", "displayOrder": 5}
        ],
        "certificationType": null,
        "enrollmentId": 501,
        "progressId": 601,
        "learningAvailability": {"state": "ready", "reason": null}
      },
      {
        "courseId": 101,
        "courseName": "VCC Fixture Course",
        "status": "NOT_STARTED",
        "summary": [
          {"id": 201, "itemType": "video", "title": "Orientation Video", "displayOrder": 1},
          {"id": 202, "itemType": "pdf", "title": "Safety Document", "displayOrder": 2},
          {"id": 203, "itemType": "training", "title": "Compression Practice A", "displayOrder": 3},
          {"id": 204, "itemType": "training", "title": "Compression Practice B", "displayOrder": 4},
          {"id": 205, "itemType": "assessment", "title": "Final Ventilation Assessment", "displayOrder": 5}
        ],
        "certificationType": null,
        "enrollmentId": 502,
        "progressId": 602,
        "learningAvailability": {"state": "ready", "reason": null}
      }
    ],
    "count": 2,
    "next": null,
    "previous": null
  },
  "message": "OK",
  "timestamp": "2026-09-18T00:00:00Z"
}
```

### 2.3 과정 항목·등록 진도 — CourseDetail

`GET /api/v2/courses/101/progress/?enrollmentId=501`. 같은 `courseId`라도 `enrollmentId`를 반드시 전달한다.

| 필드 | 자료형 / 의미 |
|---|---|
| `courseItems` | 아래 `CourseItem[]` |
| `courseType` | 목록과 같은 유형. 이전 정의에서는 생략 가능 |
| `enrollment` | 아래 `Enrollment` |
| `progressId` | `Id` |
| `definitionHash` | `Hash`. 다음 시작 요청에 그대로 전달 |
| `learningAvailability` | 2.1절 상태 객체 |

`CourseItem`:

| 필드 | 자료형 / 의미 |
|---|---|
| `id` / `courseItemLinkId` | `Id` / `Id`. 항목 자체 ID와 **과정 내 배치 ID**. 같은 항목이 반복돼도 배치별로 수행 |
| `step` | integer. 응답 순서 유지 |
| `title`, `iconType` | 각각 빈 문자열이 아닌 string |
| `itemType` | `content`, `training`, `assessment` |
| `contentType` | content는 `video`/`pdf`, training·assessment는 null |
| `description` | string 또는 null |
| `isCompleted` | boolean |
| `isPassed` | boolean 또는 null. 영상·문서는 항상 null |
| `completionKind` | `watched`, `skipped` 또는 null. 완료한 영상에만 값이 있다 |

`Enrollment`:

| 필드 | 자료형 / 의미 |
|---|---|
| `id`, `courseId` | 각각 `Id` |
| `status`, `courseTitle` | 각각 string. 공급자 표시 metadata |
| `loginAt`, `finishedAt` | 각각 string 또는 null. 서버 `Utc`와 달리 현재 표시 계약은 문자열만 검증 |
| `elapsedSeconds` | integer 또는 null |
| `centerName`, `enrollStatusCode` | 각각 string 또는 null |
| `sku`, `organizationId`, `organizationName` | 각각 string 또는 null. Dummy는 null |
| `dueDate` | `Utc` 또는 null. Dummy는 null |

Round 2 응답의 추가 키 예시다. `courseType`은 목록 행과 상세 최상위에 있고, 배정 metadata는 상세의 `enrollment`에 있다.

```json
{"courseType":"BLS","enrollment":{"sku":null,"dueDate":null,"organizationId":null,"organizationName":null}}
```

`enrollment.status`를 목록 `status`와 같은 enum이라고 추정하거나 표시 metadata로 새 완료를 판단하지 않는다. 영상·문서·일반 훈련 두 배치·마지막 평가를 가진 `200` 예시다.

```json
{
  "success": true,
  "data": {
    "courseItems": [
      {
        "id": 201,
        "courseItemLinkId": 1001,
        "step": 1,
        "title": "Orientation Video",
        "iconType": "video",
        "itemType": "content",
        "contentType": "video",
        "description": null,
        "isCompleted": false,
        "isPassed": null,
        "completionKind": null
      },
      {
        "id": 202,
        "courseItemLinkId": 1002,
        "step": 2,
        "title": "Safety Document",
        "iconType": "pdf",
        "itemType": "content",
        "contentType": "pdf",
        "description": null,
        "isCompleted": false,
        "isPassed": null,
        "completionKind": null
      },
      {
        "id": 203,
        "courseItemLinkId": 1003,
        "step": 3,
        "title": "Compression Practice A",
        "iconType": "training",
        "itemType": "training",
        "contentType": null,
        "description": null,
        "isCompleted": false,
        "isPassed": null,
        "completionKind": null
      },
      {
        "id": 204,
        "courseItemLinkId": 1004,
        "step": 4,
        "title": "Compression Practice B",
        "iconType": "training",
        "itemType": "training",
        "contentType": null,
        "description": null,
        "isCompleted": false,
        "isPassed": null,
        "completionKind": null
      },
      {
        "id": 205,
        "courseItemLinkId": 1005,
        "step": 5,
        "title": "Final Ventilation Assessment",
        "iconType": "assessment",
        "itemType": "assessment",
        "contentType": null,
        "description": null,
        "isCompleted": false,
        "isPassed": null,
        "completionKind": null
      }
    ],
    "enrollment": {
      "id": 501,
      "status": "ENROLLED",
      "courseTitle": "VCC Fixture Course",
      "courseId": 101,
      "loginAt": null,
      "finishedAt": null,
      "elapsedSeconds": null,
      "centerName": null,
      "enrollStatusCode": null,
      "sku": null,
      "dueDate": null,
      "organizationId": null,
      "organizationName": null
    },
    "progressId": 601,
    "definitionHash": "5de0ca77a8007471240fb4ec49b413a4b524d0f7adb73613c0da3b6d13c2709c",
    "learningAvailability": {"state": "ready", "reason": null}
  },
  "message": "OK",
  "timestamp": "2026-09-18T00:00:00Z"
}
```

### 2.4 항목 상세 — ItemDetail

`GET /api/v2/courses/101/items/1003/?enrollmentId=501`. 경로는 항목 자체 `id`가 아닌 **`courseItemLinkId`**를 사용한다.

| 필드 | 자료형 / 의미 |
|---|---|
| `id`, `courseItemLinkId` | 각각 `Id` |
| `title` | string |
| `itemType` | `content`, `training`, `assessment` |
| `displayOrder` | integer |
| `usage` | `OWNED`, `ITEM_REFERENCE`, `COURSE_SNAPSHOT` |
| `logicalId` | `Uuid` |
| `description` | string 또는 null |
| `detail` | content는 `FileDetail` 또는 null, training·assessment는 `TrainingProgram` 또는 null |

`detail=null`은 상세 자료가 없다는 뜻이다. 앱이 설정이나 URL을 임의로 채우지 않는다. 훈련·평가 시작은 서버의 실행 정의 검사를 따른다. 콘텐츠 시작 가능 여부를 이 null 값만으로 단정하지 않는다. 퀴즈 모델은 없다.

`FileDetail`은 영상·문서의 `detail`과 TrainingProgram의 `content[]`에서 재사용한다.

| 필드 | 자료형 / 의미 |
|---|---|
| `id` | `Id` |
| `fileName` | string |
| `order` | integer |
| `url`, `contentUrl` | 각각 string 또는 null. 둘 다 필수 키이며 실제 접근 방식은 공급자 계약 확인 대상 |

영상 상세의 `200` 예시다. 문서도 같은 구조이며 과정 요약/진도의 `pdf` 표시로 구분한다. `contentVersion`은 상세에 추가하지 않고 콘텐츠 시작 응답에서 받는다.

```json
{
  "success": true,
  "data": {
    "id": 201,
    "title": "Orientation Video",
    "itemType": "content",
    "displayOrder": 1,
    "courseItemLinkId": 1001,
    "usage": "OWNED",
    "logicalId": "70000000-0000-4000-8000-000000000001",
    "description": null,
    "detail": {
      "id": 301,
      "fileName": "orientation.mp4",
      "order": 1,
      "url": "https://fixture.invalid/video/orientation.mp4",
      "contentUrl": "https://fixture.invalid/video/orientation-content.mp4"
    }
  },
  "message": "OK",
  "timestamp": "2026-09-18T00:00:00Z"
}
```

### 2.5 TrainingProgram — 훈련·평가 상세

TrainingProgram의 모든 필드가 필수이며 **`training`, `assessment`, `content` 자체는 null을 허용하지 않는다.** `trainingType`, `feedbackType`, `trainingMode`는 공급자의 표시 문자열이며 고정 enum으로 임의 번역하지 않는다.

| 필드 | 자료형 |
|---|---|
| `id` | `Id` |
| `title`, `trainingType`, `feedbackType`, `trainingMode` | 각각 string |
| `training` | 아래 `TrainingSettings` object |
| `assessment` | 아래 `AssessmentSettings` object |
| `content` | `FileDetail[]`. 빈 배열 가능 |

`TrainingSettings`의 기존 키는 필수이고 값은 nullable이다. 새 키는 Round 2 정의에 포함되며 이전 정의에서는 생략될 수 있다.

| `training` 필드 | 자료형 |
|---|---|
| `manikinType` | string 또는 null |
| `duration`, `compressionLimit`, `ventilationLimit`, `cycleLimit` | 각각 integer 또는 null. Round 2 과정의 `duration`은 null(시간 제한 없음) |
| `minimumQuantity` | `{compressions: integer, ventilations: integer}`. 각각 0 이상. Only는 해당 동작의 목표 횟수와 다른 동작 0 |
| `prompts` | `[{id: string, trigger: string, text: string, voice: boolean, screen: boolean, arcReviewed: boolean}]`. 각 키 non-null. 이전 공급 형식과 호환하기 위해 `arcReviewed` 생략을 허용하지만 새 Dummy 응답에는 항상 포함 |
| `compressionVentilationRatio` | 아래 비율 object 또는 null |
| `aed` | 아래 AED object 또는 null |
| `cprGuideline` | 아래 가이드라인 object 또는 null |
| `twoRescuers` | 아래 두 구조자 object 또는 null |

| 중첩 객체 | 모든 필수 하위 필드와 자료형 |
|---|---|
| `compressionVentilationRatio` | `title`: string 또는 null, `cvrVentilation`: integer 또는 null, `cvrCompression`: integer 또는 null |
| `aed` | `id`: Id, `cprGuide`·`shockMode`·`language`: string, `scenarioNo`·`volume`: integer, `padsDetection`: boolean, `arrivalSeconds`: integer 또는 null. Round 2 AED skill에서 non-null |
| `aed` 추가 키 | `arrivalMode`: `at_start`, `after_cycle`, `after_seconds` 중 하나. `arrivalAfterCycle`: integer 또는 null. `cyclesAfterShock`: integer. `padsAttachedDetection`: boolean |
| `twoRescuers` | `cycleChangeCount`: integer. null 불가 |
| `assessment` | `passThreshold`: object. null 불가 |
| `assessment.passThreshold` | `cpr`, `aed`: 각각 integer 또는 null |
| `assessment.passRule` | `composite`(기본) 또는 `gates`. Round 2 정의에 포함 |
| `assessment.gates` | gate ID를 0 이상의 유한한 number 임계값에 연결하는 object. 기본값을 사용할 때 빈 객체 |

`arrivalSeconds`는 `after_seconds`, `arrivalAfterCycle`은 `after_cycle`에서만 integer다. IT23 Dummy 설정은 패드 위치를 검출하지 않고 부착 여부를 검출한다. `cycleChangeCount`는 앱 교대 주기(2 cycle)다. 서버는 학습자/VP의 교대 순서를 검증하지 않는다. 1인 CPR은 영아도 30:2로 조립하고 2인 영아 CPR만 15:2다. 2인 최소량은 학습자 몫 4 cycle 기준 성인 120/8, 영아 60/8이다.

prompt `id`는 프로그램 안에서 유일하다. `trigger`는 앱 enum `drill_start`, `set_start(n)`(n=1~5), `role_switch(toCompressions|toVentilations)`, `aed_arrived`, `aed_infant_pads`, `analysis_stand_clear`, `cpr_resume`에 대응한다. `voice=true, screen=false`는 앱 `presentation=voice`, 반대는 `screen`, 둘 다 true면 `both`다. `drill_complete`는 서버 prompts에 없다. 앱 기본 문구를 쓴다.

`arcReviewed=false`는 ARC 미검토 문구다. `set_start(n)`의 "Perform 2 breaths"와 2인 `cpr_resume`의 "Perform 2 cycles of CPR"만 false이며 나머지는 제공된 검토 목록 기준 true다. 1인 AED 시작은 AED 도착 안내이고, 분석 화면 문구는 ASCII hyphen을 사용한 "Analyzing - stand clear"다. 아래는 1인 AED practice의 변경 키 예시다.

```json
{
  "training": {
    "duration": null,
    "minimumQuantity": {"compressions":30,"ventilations":2},
    "prompts": [{"id":"drill_start","trigger":"drill_start","text":"The AED has arrived. Turn on the AED and attach the pads","voice":true,"screen":true,"arcReviewed":true}],
    "aed": {
      "id":970034,"cprGuide":"ARC2025","shockMode":"semi_automatic","language":"en","scenarioNo":1,"volume":5,
      "arrivalSeconds":null,"padsDetection":false,"arrivalMode":"at_start","arrivalAfterCycle":null,
      "cyclesAfterShock":1,"padsAttachedDetection":true
    }
  },
  "assessment": {"passRule":"composite","gates":{}}
}
```

`cprGuideline`이 object일 때의 전체 필드:

| 필드 | 자료형 |
|---|---|
| `title`, `manikinType`, `name` | 각각 string 또는 null |
| `compressionDepthMax`, `compressionDepthMin` | 각각 integer 또는 null |
| `compressionRateMax`, `compressionRateMin` | 각각 integer 또는 null |
| `ventilationVolumeMax`, `ventilationVolumeMin` | 각각 integer 또는 null |
| `ventilationRateMax`, `ventilationRateMin` | 각각 integer 또는 null |
| `compressionDepthMaxInch`, `compressionDepthMinInch` | 각각 number 또는 null. 정수/소수 모두 가능 |

실제 ARC 공급 자료의 표시 설정 단위, 제한값의 0/null 의미, 여러 종료 조건의 우선순위는 공식 계약 확인 대상이다. 일반 공급 스키마는 위 표처럼 nullable을 유지한다. 앱이 이 설정으로 업로드용 `condition`을 재구성하지 않는다.

이전 15과정 카탈로그(2026-09-30 D132)의 duration 300/120/120/720/900초, 호흡 8회, 2인 AED 10 cycle은 과거 값이다. Round 2 네 과정에서는 `duration`이 모두 null이며 연습·마지막 평가가 다음 설정을 사용한다.

| 프로그램 | `compressionLimit` | `ventilationLimit` | `cycleLimit` | `minimumQuantity` 압박/호흡 (성인, 영아) |
|---|---:|---:|---:|---|
| `bls-compressions-60` | 60 | null | null | 60/0 |
| `bls-ventilations-12` | null | 12 | null | 0/12 |
| `bls-cpr-3` | null | null | 3 | 90/6 (영아도 90/6) |
| `bls-2r-cpr-8` | null | null | 8 | 120/8, 60/8 |
| `bls-2r-cpr-aed-8` | null | null | 8 | 120/8, 60/8 |
| `lay-compressions-30` | 30 | null | null | 30/0 |
| `lay-breaths-5x2` | null | 10 | null | 0/10 |
| `lay-cpr-3` | null | null | 3 | 90/6 (영아도 90/6) |
| `lay-aed-cpr-1` | null | null | 1 | 30/2 |

CPR의 `compressionVentilationRatio`는 1인 30:2(영아 포함), 2인 성인 30:2·영아 15:2다. Only의 비율은 null이다. `twoRescuers`는 2인만 `{"cycleChangeCount":2}`, 나머지는 null이다. `aed`는 AED skill만 object이며 2인은 `after_cycle`/6/충격 후 2 cycle, 1인은 `at_start`/충격 후 1 cycle이다. 두 경우 `arrivalSeconds=null`, `padsDetection=false`, `padsAttachedDetection=true`다.

공통 `assessment`는 `passThreshold={"cpr":80,"aed":null}`, `passRule="composite"`, `gates={}`다. `cprGuideline`은 기존 ARC2025 연령별 범위다. 마지막 평가의 `trainingMode`는 `"assessment"`, 연습은 `"practice"`다. 세션 refresh 또는 새 로그인 후 목록·상세를 다시 읽는다. GET만으로는 저장된 상세가 갱신되지 않는다.

아래는 모든 필드를 포함하는 합성 fixture의 `200` 예시로 일반 공급 스키마의 null 허용을 보여 준다. DummyDev의 실제 설정값은 위 설명과 표를 따르며, 이 JSON은 실제 ARC 설정값을 뜻하지 않는다.

```json
{
  "success": true,
  "data": {
    "id": 203,
    "title": "Compression Practice A",
    "itemType": "training",
    "displayOrder": 3,
    "courseItemLinkId": 1003,
    "usage": "OWNED",
    "logicalId": "72000000-0000-4000-8000-000000000001",
    "description": null,
    "detail": {
      "id": 401,
      "title": "Chest Compression Only",
      "trainingType": "chest compression only",
      "feedbackType": "standard",
      "trainingMode": "practice",
      "training": {
        "manikinType": "adult",
        "duration": null,
        "compressionLimit": null,
        "ventilationLimit": null,
        "cycleLimit": null,
        "compressionVentilationRatio": null,
        "aed": null,
        "cprGuideline": null,
        "twoRescuers": null
      },
      "assessment": {
        "passThreshold": {"cpr": null, "aed": null}
      },
      "content": []
    }
  },
  "message": "OK",
  "timestamp": "2026-09-18T00:00:00Z"
}
```

## 3. 시작과 재전송

영상·문서는 `POST learning-starts/`, 훈련·평가는 `POST attempts/`에 같은 형태의 `StartRequest`를 보낸다. 과정 상세에서 받은 식별자와 hash를 그대로 사용한다.

| StartRequest 필드 | 자료형 |
|---|---|
| `clientRequestId` | `Uuid` |
| `courseId`, `enrollmentId`, `courseItemLinkId` | 각각 `Id` |
| `definitionHash` | `Hash` |

모두 필수·non-null이다. 앱이 임의의 목표·연령·등록을 추가하지 않는다.

```json
{
  "clientRequestId": "10000000-0000-4000-8000-000000000001",
  "courseId": 101,
  "enrollmentId": 501,
  "courseItemLinkId": 1001,
  "definitionHash": "5de0ca77a8007471240fb4ec49b413a4b524d0f7adb73613c0da3b6d13c2709c"
}
```

새 시작마다 UUID를 만들되 **응답이 유실되면 같은 세션·같은 UUID·같은 body로 재전송**한다. 같은 요청은 원래 시작을 반환하고, 같은 UUID의 다른 body는 `409 IDEMPOTENCY_CONFLICT`다. 정의가 바뀌면 `409 DEFINITION_CHANGED`이므로 과정 상세를 다시 읽고 새 시작 요청을 준비한다. `target`, 점수, 학생 ID, 완료 여부를 추가하지 않는다.

영상·문서 시작 응답 `ContentStart`는 `startId:Uuid`, `courseId:Id`, `enrollmentId:Id`, `courseItemLinkId:Id`, 빈 문자열이 아닌 `contentVersion:string`, `definitionHash:Hash`다. 모두 필수·non-null이며 이후 보고에 `startId`와 `contentVersion`을 보관한다.

```json
{
  "startId": "40000000-0000-4000-8000-000000000001",
  "courseId": 101,
  "enrollmentId": 501,
  "courseItemLinkId": 1001,
  "contentVersion": "video-v1",
  "definitionHash": "5de0ca77a8007471240fb4ec49b413a4b524d0f7adb73613c0da3b6d13c2709c"
}
```

## 4. 영상·문서 진도 보고

`ContentReport`는 `enrollmentId:Id`, `courseItemLinkId:Id`, `startId:Uuid`, `reportId:Uuid`, 빈 문자열이 아닌 `contentVersion:string`, 아래 `event:object`를 포함한다. 모두 필수·non-null이다. `courseId`는 path로만 전달한다. `PUT /api/v2/courses/101/progress/` 예시:

```json
{
  "enrollmentId": 501,
  "courseItemLinkId": 1001,
  "startId": "40000000-0000-4000-8000-000000000001",
  "reportId": "30000000-0000-4000-8000-000000000001",
  "contentVersion": "video-v1",
  "event": {"type":"video_segments","intervalsMs":[[0,10000],[10000,20000]]}
}
```

보고마다 새 `reportId`를 사용하고, 같은 START의 재전송에는 원래 ID·내용·배열 순서를 유지한다. `isCompleted`나 `isPassed`는 입력하지 않는다. `intervalsMs`는 비어 있지 않은 배열이며 각 원소는 정확히 두 integer `[start,end]`다.

| `event`의 정확한 형태 | 완료 규칙 |
|---|---|
| `{"type":"video_segments","intervalsMs":[[0,10000]]}` | `[start,end)` 밀리초 구간. `0 ≤ start < end ≤ 서버의 시작 당시 영상 길이`. 겹치거나 반복된 구간은 합집합이며 전체 길이를 덮으면 자동 완료 |
| `{"type":"video_skipped","positionMs":5000}` | 영상 Skip. `positionMs`는 시작 당시 길이 이하의 0 이상 integer(ms)다. 서버가 해당 START를 완료로 처리한다 |
| `{"type":"document_displayed"}` | 문서가 실제 표시된 근거. 이것만으로 완료하지 않음 |
| `{"type":"document_confirmed","displayReportId":"<표시 보고 UUID>"}` | 같은 START·버전의 표시 보고를 참조하는 읽음 확인. 확인이 먼저 도착하면 근거를 보관하고 표시 보고 수신 후 재평가 |

전체 재생은 `watched`, Skip은 `skipped`로 완료한다. 이미 `watched`인 영상에 Skip을 보내도 `watched`를 유지한다. 실제 콘텐츠 접근·앱 관측 방식은 앱팀과 공급자의 인수 대상이다. 단순히 영상 끝 위치로 이동한 것을 재생 구간으로 보고하지 않는다.

진도 보고 응답 `ProgressReceipt`의 `startId`·`reportId`는 `Uuid`, `courseItemLinkId`는 `Id`, `isCompleted`는 boolean, `isPassed`는 null, `courseStatus`는 2.2절 상태 enum, `application`은 아래 적용 상태 문자열이다. 추가 키 `completionKind`는 `watched`, `skipped` 또는 null이다. 미완료·문서·초기화 전 보고는 null이다. 응답의 모든 키가 필수다.

```json
{
  "startId": "40000000-0000-4000-8000-000000000001",
  "reportId": "30000000-0000-4000-8000-000000000001",
  "courseItemLinkId": 1001,
  "isCompleted": true,
  "isPassed": null,
  "courseStatus": "IN_PROGRESS",
  "application": "applied",
  "completionKind": "watched"
}
```

`application`은 `applied`, `unchanged`, `pending_evidence`, `pending_reconciliation`, `historical_only` 중 하나다. 마지막 값은 초기화 전 근거만 보관했다는 뜻이며 이때 `isCompleted`, `isPassed`, `courseStatus`, `completionKind`는 **모두 null**이다. 현재 진도를 지운다는 뜻이 아니다. 같은 보고 재전송은 당시 응답을 반환하므로 **최신 진도는 과정 GET으로 확인**한다.

콘텐츠 시작은 해당 세션 소유이며 attempt 재인가 API로 다른 세션에 옮길 수 없다. 보고 개수·구간·누적 저장 용량을 넘으면 `413 PROGRESS_CAPACITY_EXCEEDED`이고 부분 저장하지 않는다. 한도 도달 후에도 이미 접수한 같은 보고의 재전송은 원래 응답을 반환한다. 로컬 fixture 한도를 운영값으로 사용하지 않는다.

## 5. 측정 시도와 업로드

`POST /api/v2/attempts/`의 `data` 예시:

```json
{
  "attemptId": "50000000-0000-4000-8000-000000000001",
  "state": "created",
  "courseId": 101,
  "enrollmentId": 501,
  "courseItemLinkId": 1003,
  "definitionHash": "5de0ca77a8007471240fb4ec49b413a4b524d0f7adb73613c0da3b6d13c2709c",
  "createdAt": "2026-09-18T00:00:00Z",
  "role": "training",
  "condition": {
    "mode":"training", "target":"adult", "training_type":"compression_only",
    "guideline":"ARC2025", "cpr_cycle_type":"302", "is_2rescuers":false
  },
  "resumeCredential": "<이 시도의 복구 증표>"
}
```

`attemptId`, `resumeCredential`, `condition`을 안전하게 보관한다. `condition`의 6개 필드·자료형을 업로드에 그대로 돌려준다. 과정의 마지막 assessment만 `role=final_assessment`이며 중간 assessment는 `training` 역할이다. 일반 조회는 같은 외곽이지만 `resumeCredential`을 반환하지 않는다. 생성 요청 재전송은 생성 당시 receipt이므로 현재 `state`는 별도 GET으로 확인한다.

`Attempt`의 전체 자료형:

| 필드 | 자료형 / null 규칙 |
|---|---|
| `attemptId` | `Uuid` |
| `state` | 아래 7개 상태 중 하나인 string |
| `courseId`, `enrollmentId`, `courseItemLinkId` | 각각 `Id`. 기존 과정 귀속 없는 시도만 null |
| `definitionHash` | `Hash`. 기존 과정 귀속 없는 시도만 null |
| `createdAt` | `Utc` |
| `role` | `training` 또는 `final_assessment`. 기존 과정 귀속 없는 시도만 null |
| `condition` | 아래 6개 키를 가진 object. null 불가 |

`CreatedAttempt`는 새 과정 시도의 `Attempt`에 빈 문자열이 아닌 `resumeCredential:string`을 추가한다. `ReauthorizedAttempt`도 이 필드를 추가하지만 기존 과정 귀속 없는 시도의 null은 유지한다. `GET Attempt`에는 이 키 자체가 없다.

| `condition` 필드 | 자료형 / 해석 |
|---|---|
| `mode` | 빈 문자열이 아닌 string. 받은 값을 그대로 사용 |
| `target` | 빈 문자열이 아닌 string. 현재 `adult`, `child`, `infant` |
| `training_type` | 빈 문자열이 아닌 string. 현재 `cpr`, `compression_only`, `ventilation_only` |
| `guideline` | 빈 문자열이 아닌 string. 현재 조립은 `ARC2025`; 기존 파서는 AHA2020/ARC2020/ARC2025/ERC2020/STD2015 지원 |
| `cpr_cycle_type` | 빈 문자열이 아닌 string. 문자열 `"152"`는 15:2, 현재 성인·소아 예시 `"302"`는 30:2. 값·자료형 변경 금지 |
| `is_2rescuers` | boolean. 0/1로 변환하지 않음 |

| `Attempt.state` | 의미 / 계산 GET |
|---|---|
| `created` | 측정 접수 전 / `409 INVALID_STATE` |
| `queued` | 영속 접수 후 처리 대기 / `202 pending` |
| `processing` | 계산·판정 처리 중 / `202 pending` |
| `evaluated` | 계산·평가 저장 / 검증된 결과가 있으면 `200 succeeded` |
| `cancelled` | 접수 전 취소 / `409 INVALID_STATE` |
| `failed` | 계산 실패 / 고정 `503` 오류 |
| `outcome_unknown` | 계산 결과 확인 필요 / `503 CALCULATION_OUTCOME_UNKNOWN` |

서버의 계산이 중단돼 다시 시작하는 일이 5번을 넘으면(6번째 중단) 그 시도는 `failed`로 끝나고 계산 GET은 `503 CALCULATION_FAILED`다(D104). 교육상 불합격이 아니며 점수·완료·진도에 반영하지 않는다. 마지막 평가였다면 그 응시의 진행 표시가 해제되어 시작 조건을 만족하면 새로 응시할 수 있다.

일반 훈련은 완료·합격 여부와 관계없이 다시 시작할 수 있고, 같은 항목·다른 항목의 동시 측정을 허용한다(D130). 마지막 평가는 앞선 모든 항목 완료가 필요하고 같은 등록에서 한 번에 한 시도만 진행하며, 합격·불합격과 관계없이 횟수 제한 없이 다시 응시할 수 있다. 다시 수행한 결과가 더 나빠도 이미 얻은 항목 완료·마지막 평가 합격·과정 완료는 유지되며 그 결과는 `progressApplication.reason=ALREADY_COMPLETED`로만 기록된다(D131). 계산·판정 중이나 복구/정책 확인 중에는 새 응시를 임의 생성하지 않는다.

`POST /api/v2/attempts/{attemptId}/calculation/`은 **누적 실제 바이너리**를 받는다. Dummy는 가짜 점수를 뜻하지 않는다. `X-Attempt-ID`를 추가할 필요가 없고 경로의 ID를 사용한다.

| multipart part | 필수 | 내용 |
|---|---|---|
| `rawHexBPfile` | 예 | CPR 원본 binary 파일 bytes |
| `condition` | 예 | 위 객체를 JSON 문자열로 직렬화 |
| `aedHexBPfile` | 아니요 | 수집한 AED binary 파일 bytes |
| `vp_event_list` | 아니요 | 기존 VP 이벤트 JSON array 문자열. 없으면 생략 또는 `[]` |

boundary는 HTTP 라이브러리가 만들게 한다. `Content-Type` 헤더는 하나만 보낸다. 대소문자만 다른 중복 헤더, 여러 값, 서로 다른 두 표현처럼 모호하거나 값이 비어 있거나 쉼표·줄바꿈(CR/LF)을 포함하면 파서와 시도 조회 전에 `400 INVALID_REQUEST`다(D103). 파일을 hex 문자열이나 일반 JSON body로 바꾸지 않는다. VP 이벤트는 압박 시작/종료 `0/1`, 호흡 `10/11`, AED `20/21`이며 `timestamp`·`last_timestamp` 단위는 ms다. 기존 우선순위는 truthy인 `last_timestamp`다. `last_timestamp`가 `0`, `null`이거나 없으면 `timestamp`를 사용한다.

호환용 base64 form도 유지한다. 파일 bytes를 URL-safe base64 `cpr_b64_data`/선택 `aed_b64_data`로 만들고, `condition`·선택 `vp_event_list`의 JSON 문자열과 함께 **form URL 인코딩 → 전체 percent 인코딩 → 전체 URL-safe base64 인코딩**한다. 이 최종 문자열을 `application/x-www-form-urlencoded`로 보낸다. 일반 form body·일반 JSON·Swagger의 자동 form 생성과 다르다. 새 앱은 multipart를 사용한다. 기존 파서의 추가 호환 입력이 필요한 경우 [상세 계산 계약](ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md)을 확인한다.

**현재 계산 규칙: HSTM `92f56dc` legacy(R00), DEPTH_V1r3.** 모든 지원 guideline에 같은 검출 규칙을 적용하며 점수 경계와 가중치는 guideline별 원본을 사용한다.

- 압박은 패킷별 깊이 최대값의 지그재그에서 정점 대비 30 raw(15mm) 하강한 Record에서 확정한다. 파일 끝의 미확정 압박도 골 대비 30 raw 이상 상승했으면 센다. 첫 패킷을 카운터 기준선으로 버리지 않으며 펌웨어 `Ccnt`·`CRate`는 계산에 쓰지 않는다.
- 압박 속도는 연속 정점 표본의 간격 × 5ms로 계산한다. 사이클과 AED 다음 파트의 첫 압박 속도는 0이다.
- 호흡은 패킷의 호흡량 최대값(10mL 미만은 0)이 직전 비교값보다 작고, 현재 패킷의 호흡량 최소값도 10mL 미만일 때 확정한다. 압박 또는 호흡 확정 시 비교값과 관측 버퍼를 초기화한다.
- 하강 없이 끊긴 파일 끝 호흡은 마지막 비교값이 10mL 이상이고, 패킷 처리 중 직전 압박·호흡 확정 Record부터 남은 버퍼가 15패킷 이상일 때만 1회 추가한다. 확정 Record를 포함하며 이전 확정이 없으면 파일 시작부터 센다. 15패킷은 50ms 주기 기준 750ms이며, timestamp 차이로 검사하는 조건은 아니다.
- 호흡과 같은 Record에서 확정된 압박은 시간 0 창이다. AED 창 뒤 첫 압박의 하강 구간은 복원한다(R6).
- 손 위치는 하강·이완 표본 전체로 판정한다. 복부 `2`를 우선하고 좌우 `4/8` 중 다수, 동수면 right다. 영아는 하위 4비트를 사용한다.

**앱 기록 권고:** 종료 신호 뒤에도 호흡량이 0으로 돌아올 때까지 또는 최소 2패킷을 더 기록한다. 앱 로컬 Count Limit(`Ccnt`)과 서버 `action_count`·`minimumQuantity`는 기준이 다르다. 서버가 정본이며 로컬 카운트는 종료용이다. 앱팀은 [DEPTH_V1r3 공통 벡터 54건](../tests/fixtures/depth_boundary_v1/)으로 같은 파일의 압박·호흡·차트를 대조한다.

CPR 원본은 다음 순서로 검사한다. 길이는 bytes 단위다. 접수 시에는 패킷 목록을 만들지 않고 같은 검사를 수행하며 Worker가 저장 파일을 읽을 때도 다시 검사한다.

| 검사 | 거절 조건 | `/api/v2` 오류 |
|---|---|---|
| 크기 | 4,000,000 bytes 초과 | `413 PAYLOAD_TOO_LARGE` |
| 빈 파일 | 0 bytes | `422 MEASUREMENT_INPUT_INVALID` |
| 길이 | 28의 배수 또는 그 길이 +20이 아님 | `422 MEASUREMENT_INPUT_INVALID` |
| 헤더 | 첫 바이트가 `0xA8`이 아님 | `422 MEASUREMENT_INPUT_INVALID` |
| 끝 20 bytes | +20 형식의 trailer에 0이 아닌 값이 있음 | `422 MEASUREMENT_INPUT_INVALID` |

외부 요청/저장 크기 한도는 이 검사보다 먼저 적용될 수 있다. 저장된 입력에서 같은 원본 오류가 확인되면 Worker는 `STORED_INPUT_INVALID`로 처리한다.

**AED 스트림과 VP 이벤트.** AED를 사용할 때 앱이 도착 시점에 AED 스트림 `ARRIVED(0)`을 기록한다. 서버는 첫 AED 이벤트부터 shock/no-shock까지 AED part의 압박을 CPR cycle에서 제외한다. 첫 이벤트가 `ARRIVED(0)`이면 bridge 전체를 제외하며, 전원 이벤트부터 기록하면 전원 전 압박이 CPR 묶음에 남을 수 있다. 2인 AED는 6번째 cycle 뒤 도착하고 shock/no-shock 뒤 2 cycle을 수행한다. `vp_event_list`의 `20(START_AED)`, `21(END_AED)`는 2인 bridge의 VP 구간 표지다. 이 스트림의 21을 AED 스트림의 21과 혼동하지 않는다.

| 코드 | 앱 `AedTrainerEvent` | 서버 AED 스트림 | IT23 펌웨어 |
|---|---|---|---|
| 0 | 앱이 도착 시 합성 기록 | ARRIVED | 앱 생성 |
| 1 | Power on | POWER_ON | 공식 스펙 확인 필요 |
| 10 | Pads attached | PADS_ON | 공식 스펙 확인 필요 |
| 13 | Analyzing | ANALYZING | 공식 스펙 확인 필요 |
| 15 | — | PRESS_BUTTON_SHOCK | 공식 스펙 확인 필요 |
| 16 | Shock delivered | SHOCK_DELIVERED | 공식 스펙 확인 필요 |
| 19 | Begin CPR | BEGIN_CPR | 공식 스펙 확인 필요 |
| 21 | — | NO_SHOCK_ADVISE | 공식 스펙 확인 필요 |

앱은 AED 도착 시 `0(ARRIVED)`, 무충격 안내 시 `21(NO_SHOCK_ADVISE)`를 합성해 기록한다. `15`는 기록하지 않아도 된다. `13(ANALYZING)`은 서버 상수에 있으므로 그대로 기록한다.

앱은 `0`, `13`, 종점인 `16` 또는 `21`을 반드시 기록한다. 13이 없거나 13 뒤에 16/21이 없으면 `aed_no_contact_during_analysis`는 `applicable=true, value=null, passed=false`다. 분석 구간이 둘 이상이면 각 구간을 모두 검사한다. 이 gate는 기록된 압박 유무만 검사하며 실기기의 모든 접촉을 검출했다는 뜻은 아니다.

시험 호출 예시다. `VCC_API_BASE`는 팀에서 받은 서버 주소이며, 인증 헤더 파일과 실제 바이너리는 저장소 밖의 접근 제한된 위치에 둔다. 토큰을 명령 인자에 직접 적지 않는다.

```sh
curl --request POST "$VCC_API_BASE/api/v2/sessions/" \
  --header 'Content-Type: application/json' \
  --data '{"loginId":"test@test.com","password":"2222"}'

curl --request GET "$VCC_API_BASE/api/v2/courses/$COURSE_ID/progress/?enrollmentId=$ENROLLMENT_ID" \
  --header @/path/to/private/auth.headers

curl --request POST "$VCC_API_BASE/api/v2/attempts/$ATTEMPT_ID/calculation/" \
  --header @/path/to/private/auth.headers \
  --form 'rawHexBPfile=@/path/to/measurement.bin;type=application/octet-stream' \
  --form 'condition=</path/to/condition.json;type=application/json'
```

## 6. 결과·완료·차트

v5 이하 정의로 시작해 아직 계산하지 않은 시도는 계산할 수 없다. 상세를 다시 받아 새 시도로 시작한다. 필요하면 `POST /api/v2/session/refresh/` 또는 새 로그인 후 목록·상세를 조회한다. 기존 저장 결과와 유효한 저장 후보의 복구는 유지하며 재계산하지 않는다. 후보 없는 구버전 작업은 자동 실패·취소되지 않는다. 해당 과정 시도는 `outcome_unknown`으로 보류되고 계산 GET은 `503 CALCULATION_OUTCOME_UNKNOWN`이다. 마지막 평가라면 별도 FINAL 상태가 `recovery_required`가 된다.

업로드가 영속 접수되면 `202`다. GET도 처리 중에는 같은 형태를 반환한다.

```json
{
  "attemptId": "50000000-0000-4000-8000-000000000001",
  "calculationStatus": "pending",
  "calculation": null,
  "evaluation": null,
  "progressApplication": null,
  "submit_arc": {"status":"disabled","ok":false,"error":"arc_contract_pending","exclusionReasons":[]}
}
```

앱 대기 한도는 **파일 업로드 시작부터 최대 30초**다. 업로드 시간도 포함하며 POST 수신·202·각 조회에서 시간을 다시 시작하지 않는다. 한도가 지나도 이미 접수된 서버 작업은 계속되고 같은 ID로 결과를 다시 조회할 수 있다. 전송 응답이 유실되면 시도 GET을 먼저 확인한다. `created`면 같은 측정을 다시 POST하고 `queued`/`processing`이면 결과 GET을 이어간다. 같은 시도에 다른 측정은 `409 ATTEMPT_INPUT_CONFLICT`다.

확정되면 `200`, `calculationStatus=succeeded`이며 아래 세 객체가 모두 non-null이다.

| 필드 | 앱이 확인할 내용 |
|---|---|
| `calculation` | 기존 계산 JSON 전체: `cpr_score`, `metrics`, `aed_score`, `training_stats`, `action_count`, `guide_prompts`, `certification`, `chart_dataset_url` |
| `evaluation` | `goal`, `score.decision`, `program_completed`, `reason_codes`. 점수 Pass와 완료는 별개 |
| `progressApplication` | `applied`, `applied_epoch`, `reason`. 현재 공유 진도 반영 여부 |
| `submit_arc` | ARC 제출 적격성/비활성. 계산 객체 밖의 별도 필드 |

### 6.1 계산·평가 DTO의 정확한 자료형

`CalculationPending`과 `CalculationSucceeded`는 `attemptId:Uuid`, `calculationStatus:string`, `calculation`, `evaluation`, `progressApplication`, `submit_arc`의 **6개 키가 모두 필수**다. pending은 `calculationStatus="pending"`이며 세 객체가 모두 null이고 제출 상태는 disabled다. succeeded는 `calculationStatus="succeeded"`이며 세 객체가 모두 non-null이다. 다른 계산 상태는 성공 enum에 추가하지 않고 오류 envelope로 반환한다.

`CalculationSnapshot`은 기존 계산 JSON이며 다음 8개 최상위 키가 모두 필수다.

| 필드 | 자료형과 하위 필수 키 |
|---|---|
| `cpr_score` | object. `total_score`: 아래 ScoreFields object, `part_scores`: CprPartScore[] |
| `metrics` | 아래 Metrics object. 값·키는 훈련 조건에 따라 다름 |
| `aed_score` | object. `overall`: number 또는 null, `part_scores`: object[] |
| `training_stats` | object. `cycle_count`: 0 이상 integer, `elapsed_seconds`: number |
| `action_count` | object. `comp`, `vent`: 각각 0 이상 integer |
| `guide_prompts` | string[] |
| `certification` | object. `Target`: `adult`, `child`, `baby`, `N/A` 중 하나인 string |
| `chart_dataset_url` | string 또는 null |

계산 snapshot 안에 `submit_arc`를 추가하지 않는다. 제출 상태는 `data.submit_arc` 하나뿐이다. 예전 형식으로 저장된 결과에 `submit_hstm` 같은 `submit_*` 키가 남아 있어도 응답의 `calculation`에서는 빠진다(D103, 저장 파일은 바꾸지 않음). 그 밖에 저장된 과거 결과의 키를 새로 만들거나 자료형을 강제 변환하지 않는다.

**ScoreFields와 CprPartScore는 가변 하위 객체다.** 아래는 알려진 키의 자료형이며 **모든 레벨에 모든 키가 필수라는 뜻이 아니다.** 훈련 종류·calc_case·total/part/cycle에 따른 누락·추가 키를 보존한다.

| 객체 / 필드 | 자료형 |
|---|---|
| ScoreFields의 `score_comp_depth`, `score_comp_rate`, `score_comp_no`, `score_comp_count` | 각각 number 또는 null |
| `score_recoil`, `score_hand_position`, `score_vent_vol`, `score_vent_rate` | 각각 number 또는 null |
| `score_vent_count`, `score_vent_speed`, `score_ccf`, `score_rescue_vent`, `overall` | 각각 number 또는 null |
| `judg_result`, `calc_case` | 각각 string. 존재하는 레벨에서 사용 |
| `total_action_ms` | number. 존재하는 레벨에서 사용 |
| CprPartScore의 `part_num` | integer |
| `action_with_score_list` | 동작별 object[]. 각 동작의 `comp_depth`, `vent_rate` 등 키가 아래 ActionMetric을 가리킴 |
| `cycle_with_score_list` | ScoreFields[] |
| `score` | ScoreFields object |
| ActionMetric의 `criterion`, `grade`, `value` | 각각 string, number, number/string/boolean/null. 동작 종류별 키 존재 여부 유지 |

`aed_score.part_scores[]`는 조건별 객체 배열이며 비어 있을 수 있다. v6 AED part는 끝 이벤트에 따른 `outcome`(`"shock"` 또는 `"no_shock"`)을 포함한다. 이전 결과에서는 생략될 수 있다.

**Metrics**는 이름별 값이 number, 분포 object 또는 null인 객체다. 분포 object 안의 값은 number 또는 null이며 빈 object도 유효하다.

| 대표 지표 이름 | 값의 모양 / 단위 |
|---|---|
| `CompressionDepth`, `CompressionRate`, `Recoil`, `HandPosition`, `CompressionNo`, `CompressionCount` | `%_*`, `n_*` 등의 분포 object |
| `VentilationVolume`, `VentilationRate`, `VentilationSpeed`, `VentilationCount` | 분포 object·빈 object·null 가능. 조건별 원래 형태 유지 |
| `AvgCompressionDepth`, `AvgCompressionRate`, `AvgVentilationVolume`, `AvgVentilationSpeed` | number 또는 원본 null. 각각 기존 계산기의 깊이(mm), 분당 횟수, 부피(mL), 주입 시간(ms) 지표 |
| `CompressionActionNumber`, `VentilationActionNumber`, `CompressionCycleNumber`, `VentilationCycleNumber` | number. 기존 실측 횟수 |
| `TotalEventTime`, `TotalHandsOffTime` | number. ms |
| `HandsOffTimeSec` | number. 초 |
| `ScoreOfCCF` | number. 점수 |
| `CCF` | `%_CCF`, `ccf_count`, `score`, `sum_ccf`, `sum_score` 등의 집계 object |

`Evaluation`의 기존 키는 필수다. 과거 결과에서 `goal.status`가 생략될 수 있다. `gates`는 v6 결과에 추가한다.

| 필드 | 자료형 |
|---|---|
| `goal.kind` | `compressions`, `ventilations`, `cycles` |
| `goal.required` | 1 이상 integer |
| `goal.observed` | 0 이상 integer 또는 null |
| `goal.met` | boolean 또는 null |
| `goal.status` | v6·v5·v4의 결과는 항상 `evaluated`. 더 이전 어댑터로 시작한 CPR 계열 시도는 `pending_policy`일 수 있고 과거 결과는 키 생략 가능 |
| `score.decision` | `pass` 또는 `fail` |
| `program_completed` | boolean |
| `reason_codes` | 해당 사유의 string[]. 목표 사유, 최소량 사유, 합격 규칙 사유 순서 |
| `gates` | 아래 GateResult 객체 배열. v6 결과에 포함 |

| `reason_codes` 값 | 의미 |
|---|---|
| `GOAL_POLICY_UNRESOLVED` | 목표 정책 대기(이전 pending 어댑터로 시작한 CPR 시도만). 완료 아님 |
| `GOAL_NOT_MET` | 목표 횟수·사이클 미달(`goal.met=false`) |
| `MINIMUM_QUANTITY_NOT_MET` | 최소 수행량 미달. v6은 고정한 프로그램 정의를, v5는 ARC CPR의 성인·소아 90/6, 영아 45/6을 사용한다. 총점이 80 이상이어도 합격하지 않는다 |
| `SCORE_NOT_PASS` | composite 점수가 기존 tester 합격 기준에 못 미침. 최소량 때문에만 불합격이면 이 값은 없다 |
| `GATE_NOT_PASSED` | gates 모드에서 적용하는 gate 중 하나 이상 실패. 실패 ID는 `passed=false`인 행으로 확인 |

`GateResult`의 모든 필드는 필수다. `id`·`unit`은 string, `applicable`은 boolean, `value`·`threshold`는 number 또는 null, `passed`는 boolean 또는 null이다. 비적용 행의 세 값은 null이다. 기준값이 있는 적용 gate에서 측정 근거가 없으면 `value=null`, `passed=false`다. AED gate는 AED 정의에 적용한다. 도착 시간 gate는 첫 `ARRIVED(0)`가 있어야 적용되고, 그 시점 이후의 `POWER_ON(1)`이 있어야 값이 계산된다. 기본 합격 기준이 없어 `threshold`·`passed`는 null이다. `assessment.gates`에 상한을 지정하면 판정에 사용할 수 있다.

| gate ID | 기본 기준 / unit |
|---|---|
| `repetitions` | 관측 횟수 ≥ 목표 / `count` |
| `compression_depth_in_range_pct`, `compression_rate_in_range_pct`, `ventilation_volume_in_range_pct` | ≥ 70 / `%` |
| `recoil_pct`, `hand_position_pct` | ≥ 80 / `%` |
| `compressions_per_cycle_tolerance` | 닫힌 cycle당 압박 수의 최대 편차 ≤ 2 / `count`. 2인은 학습자가 압박하는 cycle을 사용 |
| `ventilation_interval_score` | ≥ 70 / `score`. 호흡 Only BLS skill에만 적용. action 기반 분당 환기율을 `cprGuideline.ventilationRateMin/Max` band로 판정한 0~100점 |
| `aed_pads_attached` | 이벤트 10 / `count` |
| `aed_no_contact_during_analysis` | 분석(13)부터 shock/no-shock(16/21) 구간과 겹친 압박 0 / `count` |
| `aed_shock_delivered` | 이벤트 16 / `count`. 스트림에 no-shock이 있고 shock이 없으면 비적용 |
| `aed_arrival_to_power_on` | 도착(0)부터 전원(1)까지 / `seconds`. 0이 없으면 비적용 |

CPR skill의 `ventilation_interval_score`는 비적용이다. CPR의 호흡 간격 gate는 ARC Science band 확정(U-4) 뒤 별도 ID(예: `breath_interval_seconds`)로 추가할 대상으로 남기며 지금은 구현하지 않는다. 성인 호흡 Only의 기존 범위는 8~12회/분(5.0~7.5초)이며 CPR Blow Timing 표시는 현재 gate 합격 근거가 아니다.

v6은 비적용 행을 포함해 위 12개 행을 반환한다. 아래는 첫 행을 발췌한 예시다.

```json
{"gates":[{"id":"repetitions","applicable":true,"value":1,"threshold":1,"unit":"count","passed":true}]}
```

`ProgressApplication`은 `applied:boolean`, `applied_epoch:빈 문자열이 아닌 string 또는 null`, `reason:아래 고정 enum`의 세 키가 필수다. epoch는 앱이 증가시키거나 새로 만드는 값이 아니다.

v5 완료 평가 예시:

```json
{
  "goal": {"kind":"compressions","required":60,"observed":60,"met":true,"status":"evaluated"},
  "score": {"decision":"pass"},
  "program_completed": true,
  "reason_codes": []
}
```

v5 최소 수행량 미달 예시(ARC CPR, 압박 61·호흡 6, 표시 총점 83). 사이클 목표는 충족했고 합격은 실패했다.

```json
{
  "goal": {"kind":"cycles","required":3,"observed":3,"met":true,"status":"evaluated"},
  "score": {"decision":"fail"},
  "program_completed": false,
  "reason_codes": ["MINIMUM_QUANTITY_NOT_MET"]
}
```

`goal.observed`는 압박·호흡 Only의 실제 동작 수 또는 닫힌 CPR cycle 수다. CPR에서는 압박 뒤 호흡이 이어진 cycle만 센다. 2인 VP cycle은 목표 횟수에 포함하며 AED part의 동작은 제외한다. `program_completed`는 목표를 충족하고 선택된 합격 규칙을 통과했을 때 true다. `composite`는 기존 tester 점수 판정을 쓰며 gate 행은 참고용이다. 따라서 composite에서 AED gate만 실패한 결과도 목표·최소량·점수 조건을 충족하면 완료될 수 있다. `gates`는 적용 gate의 실패 여부로 합격을 판정한다. v6은 프로그램 정의의 최소량을 사용하며 2인에서는 VP 동작을 최소량에서 제외한다.

보존 v5의 저장 평가는 기존 ARC CPR 고정 최소량으로 검증하고, v4·pending-v3의 저장된 점수 null도 그대로 읽는다. pending-v3·pending-v2의 저장 CPR 결과는 `observed=null`, `met=null`, `status=pending_policy`, `program_completed=false`일 수 있다. 네 보존 어댑터 모두 새 계산은 하지 않는다. 확정된 저장 결과는 완료 미정·불합격이어도 `200`으로 조회한다. 점수는 number 또는 null이며 정밀도, 누락, 0, 빈 객체는 그대로 읽는다. `certification.Target="baby"`는 업로드 `condition.target="infant"`에 대응한다.

`progressApplication.reason`은 `APPLIED`, `PROGRESS_RESET`, `GOAL_POLICY_UNRESOLVED`, `ALREADY_COMPLETED`, `REQUIREMENTS_NOT_MET`, `PROGRESS_RECONCILIATION_REQUIRED`다. `APPLIED`일 때만 `applied=true`와 해당 epoch가 있고, 나머지는 false/null이다. 사유는 `PROGRESS_RESET` → `GOAL_POLICY_UNRESOLVED` → `ALREADY_COMPLETED` → `APPLIED`/`REQUIREMENTS_NOT_MET` 순서로 먼저 해당하는 하나다(D117). 이미 완료한 항목을 다시 수행해 기준에 못 미쳐도 `ALREADY_COMPLETED`다. 평가가 교체되어 현재 과정에 반영을 보류하면 초기화 전 결과가 아닌 한 `PROGRESS_RECONCILIATION_REQUIRED`다. 이때도 원래 결과·합격 근거는 보존된다. 앱이 새 평가 합격을 복제하지 않는다.

`submit_arc`의 Dummy 런타임 응답은 다음 두 가지다.

```json
{"status":"disabled","ok":false,"error":"arc_contract_pending","exclusionReasons":[]}
```

```json
{"status":"excluded","ok":false,"error":null,"exclusionReasons":["dummy"]}
```

`submit_arc`의 `status`, `ok`, `error`, `exclusionReasons`는 네 키 모두 필수다. 현재 생성되는 `disabled/excluded/pending`은 `ok=false`다. `acknowledged`만 `ok=true`이며 CRE 계약 전에는 발생하지 않는다. disabled의 error는 문자열 `arc_contract_pending`이고 사유는 빈 배열이다. excluded의 error는 null이며 사유 배열은 중복 없이 하나 이상 포함한다. 제외 사유는 `dummy`, `non_arc_guideline`, `progress_reset_before_result` 순서다. 나머지 상태는 `error=null`, `exclusionReasons=[]`다. 예약 상태 `sent/acknowledged/failed`의 읽기·직렬화는 지원하지만 현재 생성·저장 계획은 거절한다.

Round 2 보고 대상은 실학생의 최초 과정 완료 한 건이며 확인 대기 과정 상태는 `COMPLETION_PENDING_ARC`다. Dummy 런타임은 `disabled/excluded`만 내리고 실제 ARC 전송 경로는 비활성이다.

차트는 `calculation.chart_dataset_url`을 받은 그대로 GET한다. URL 자체가 접근 권한이므로 Bearer가 필요 없고 300초 뒤 만료된다. 갱신은 `GET .../chart-link/`로 한다. `ChartLink`의 `url`은 빈 문자열이 아닌 string, `expiresAt`은 `Utc`이며 둘 다 필수다. 응답은 `{"url":"<발급 URL>","expiresAt":"<UTC>"}` 또는 검증된 차트 없음일 때 `{"url":null,"expiresAt":null}`이다. 한쪽만 null인 응답은 없다. 새 발급이 저장된 계산 응답을 바꾸지는 않는다. 로그아웃도 이미 발급된 URL의 남은 유효시간을 없애지는 않는다.

### 6.2 계산 성공 200 — 전체 JSON 예시

아래는 기존 v5 계산 회귀자료를 사용하는 **합성 호흡 Only 예시**다. 점수·동작·지표·null을 생략하지 않은 응답 모양이다. 목표 8회는 충족했고 점수 판정은 fail이어서 프로그램은 미완료다.

```json
{
  "success": true,
  "data": {
    "attemptId": "50000000-0000-4000-8000-000000000001",
    "calculationStatus": "succeeded",
    "calculation": {
      "action_count": {"comp": 0, "vent": 8},
      "aed_score": {
        "overall": 0,
        "part_scores": []
      },
      "chart_dataset_url": null,
      "cpr_score": {
        "part_scores": [
          {
            "action_with_score_list": [
              {
                "vent_rate": {"criterion": "high", "grade": 0, "value": 600.0},
                "vent_vol": {"criterion": "good", "grade": 100, "value": 520}
              },
              {
                "vent_rate": {"criterion": "high", "grade": 0, "value": 240.0},
                "vent_vol": {"criterion": "good", "grade": 100, "value": 520}
              },
              {
                "vent_rate": {"criterion": "high", "grade": 0, "value": 240.0},
                "vent_vol": {"criterion": "good", "grade": 100, "value": 520}
              },
              {
                "vent_rate": {"criterion": "high", "grade": 0, "value": 240.0},
                "vent_vol": {"criterion": "good", "grade": 100, "value": 520}
              },
              {
                "vent_rate": {"criterion": "high", "grade": 0, "value": 240.0},
                "vent_vol": {"criterion": "good", "grade": 100, "value": 520}
              },
              {
                "vent_rate": {"criterion": "high", "grade": 0, "value": 240.0},
                "vent_vol": {"criterion": "good", "grade": 100, "value": 520}
              },
              {
                "vent_rate": {"criterion": "high", "grade": 0, "value": 240.0},
                "vent_vol": {"criterion": "good", "grade": 100, "value": 520}
              },
              {
                "vent_rate": {"criterion": "high", "grade": 0, "value": 240.0},
                "vent_vol": {"criterion": "good", "grade": 100, "value": 520}
              }
            ],
            "cycle_with_score_list": [
              {
                "calc_case": "only_vent",
                "overall": 75,
                "score_ccf": null,
                "score_comp_count": null,
                "score_comp_depth": null,
                "score_comp_no": null,
                "score_comp_rate": null,
                "score_hand_position": null,
                "score_recoil": null,
                "score_vent_count": null,
                "score_vent_rate": 0,
                "score_vent_speed": null,
                "score_vent_vol": 100,
                "total_action_ms": 1850
              }
            ],
            "part_num": 1,
            "score": {
              "overall": 0,
              "score_ccf": 0,
              "score_comp_depth": 0,
              "score_comp_no": 0,
              "score_comp_rate": 0,
              "score_hand_position": 0,
              "score_recoil": 0,
              "score_rescue_vent": 0,
              "score_vent_count": 0,
              "score_vent_rate": 0,
              "score_vent_speed": 0,
              "score_vent_vol": 0
            }
          }
        ],
        "total_score": {
          "judg_result": "N/A",
          "overall": 75,
          "score_ccf": null,
          "score_comp_count": null,
          "score_comp_depth": null,
          "score_comp_no": null,
          "score_comp_rate": null,
          "score_hand_position": null,
          "score_recoil": null,
          "score_rescue_vent": 0,
          "score_vent_count": null,
          "score_vent_rate": 0,
          "score_vent_speed": null,
          "score_vent_vol": 100
        }
      },
      "guide_prompts": ["• Ventilation volume score 100 ", "• Perform ventilations every 6 seconds"],
      "metrics": {
        "AvgCompressionDepth": 0,
        "AvgCompressionRate": 0,
        "AvgVentilationSpeed": 0,
        "AvgVentilationVolume": 520,
        "CCF": {"%_CCF": 0, "ccf_count": 0, "score": 0, "sum_ccf": 0, "sum_score": 0},
        "CompressionActionNumber": 0,
        "CompressionCount": {"%_Good": 0, "%_TooFew": 0, "%_TooMany": 0, "n_Good": 0, "n_TooFew": 0, "n_TooMany": 0},
        "CompressionCycleNumber": 0,
        "CompressionDepth": {"%_Good": 0, "%_TooDeep": 0, "%_TooShallow": 0, "n_Good": 0, "n_TooDeep": 0, "n_TooShallow": 0},
        "CompressionNo": {"%_Good": 0, "%_TooFew": 0, "%_TooMany": 0, "n_Good": 0, "n_TooFew": 0, "n_TooMany": 0},
        "CompressionRate": {"%_Good": 0, "%_TooFast": 0, "%_TooSlow": 0, "n_Good": 0, "n_TooFast": 0, "n_TooSlow": 0},
        "HandPosition": {"%_Good": 0, "%_IncorrectLR": 0, "%_IncorrectStomach": 0, "n_Good": 0, "n_IncorrectLR": 0, "n_IncorrectStomach": 0},
        "HandsOffTimeSec": 0,
        "Recoil": {"%_Good": 0, "%_Incomplete": 0, "n_Good": 0, "n_Incomplete": 0},
        "ScoreOfCCF": 0,
        "TotalEventTime": 1850,
        "TotalHandsOffTime": 1850,
        "VentilationActionNumber": 8,
        "VentilationCount": {"%_Good": 0, "%_TooFew": 0, "%_TooMany": 0, "n_Good": 0, "n_TooFew": 0, "n_TooMany": 0},
        "VentilationCycleNumber": 1,
        "VentilationRate": {"%_Good": 0, "%_InFrequently": 0, "%_TooFrequently": 100, "n_Good": 0, "n_InFrequently": 0, "n_TooFrequently": 7},
        "VentilationSpeed": null,
        "VentilationVolume": {"%_Good": 100, "%_TooLittle": 0, "%_TooMuch": 0, "n_Good": 8, "n_TooLittle": 0, "n_TooMuch": 0}
      },
      "training_stats": {"cycle_count": 1, "elapsed_seconds": 2.2},
      "certification": {"Target": "N/A"}
    },
    "evaluation": {
      "goal": {"kind": "ventilations", "required": 8, "observed": 8, "met": true, "status": "evaluated"},
      "score": {"decision": "fail"},
      "program_completed": false,
      "reason_codes": ["SCORE_NOT_PASS"]
    },
    "progressApplication": {"applied": false, "applied_epoch": null, "reason": "REQUIREMENTS_NOT_MET"},
    "submit_arc": {
      "status": "disabled",
      "ok": false,
      "error": "arc_contract_pending",
      "exclusionReasons": []
    }
  },
  "message": "OK",
  "timestamp": "2026-09-18T00:00:00Z"
}
```

## 7. 취소·복구·전체 오류

- 취소 요청은 `{"reason":"user_cancelled"}` 또는 `{"reason":"connection_lost"}`다. 후자는 **마네킨 연결 단절**이며 앱↔API/ARC 통신 장애가 아니다. `created`만 취소하고 이미 취소된 재요청은 `204`다. 접수 후 계산 작업 취소 API는 없다.
- 재로그인 후 원래 `attemptId`에 `{"resumeCredential":"<원래 증표>"}`를 보내 재인가한다. 같은 사용자이며 이전 연결 세션이 만료·폐기된 경우만 허용한다. 다른 이전 세션이 유효하거나 시도가 cancelled면 `409`다. 새 세션에 이미 연결된 동일 재요청은 `200`이다. 새 응답 증표를 보관한다.
- Dummy·실학생 로그아웃은 해당 세션만 폐기하고 공유 진도·다른 활성 세션·저장 결과를 유지한다. Dummy 초기화는 전용 reset API로만 수행한다. 재인가해도 옛 시도의 epoch를 새 진도로 바꾸지 않는다. 실제 ARC 인증 연동은 별도다.

| HTTP / 대표 코드 | 앱 처리 |
|---|---|
| `400 INVALID_REQUEST` | 요청 필드·자료형·query·헤더 확인 |
| `401 LOGIN_FAILED`, `SESSION_REQUIRED`, `SESSION_EXPIRED` | 로그인 확인·재로그인, 필요 시 원래 시도 재인가 |
| `403 SESSION_REVOKED` | 폐기된 세션. 재로그인 및 필요한 재인가 |
| `404 NOT_FOUND` | 경로·배정·연결 세션 확인. 다른 소유자의 존재를 노출하지 않음 |
| `409 DEFINITION_CHANGED` | 과정 상세 재조회 후 새 시작 요청 준비 |
| `409 IDEMPOTENCY_CONFLICT`, `ATTEMPT_INPUT_CONFLICT`, `PROFILE_MISMATCH` | 같은 ID의 내용을 바꾸지 말고 원래 요청·condition 확인 |
| `409 PREREQUISITES_NOT_COMPLETED` | 앞선 항목 완료 후 마지막 평가 시작 |
| `409 FINAL_ASSESSMENT_ACTIVE`, `FINAL_ASSESSMENT_RECOVERY_REQUIRED`, `COMPLETION_POLICY_PENDING` | 기존 평가·복구/정책 대기. 임의 새 응시·완료 처리 금지 |
| `409 PROGRESS_RECONCILIATION_REQUIRED`, `CONTENT_VERSION_MISMATCH` | 진도/버전 확인. 원래 결과를 보존하고 의미를 임의 보정하지 않음 |
| `409 EXECUTION_DEFINITION_MISSING`, `422 EXECUTION_DEFINITION_UNSUPPORTED` | 해당 항목 시작 불가. 설정 확인 요청 |
| `409 INVALID_STATE` | 취소·결과 조회·재인가가 현재 상태에서 가능한지 확인 |
| `413 PAYLOAD_TOO_LARGE`, `PROGRESS_CAPACITY_EXCEEDED` | 업로드·콘텐츠 근거 한도 확인. 부분 성공으로 표시하지 않음 |
| `422 MEASUREMENT_INPUT_INVALID` | 파일·인코딩·측정 필드 확인 |
| `503 CONTRACT_PENDING`, `ARC_PROGRESS_UNAVAILABLE`, `UPSTREAM_CONTRACT_MISMATCH` | 연동·진도·공급자 계약 대기. 빈 과정이나 새 성공으로 대체하지 않음 |
| `503 CALCULATION_OUTCOME_UNKNOWN`, `CALCULATION_FAILED`, `STORED_INPUT_INVALID`, `CALCULATOR_CONTRACT_MISMATCH`, `TEMPORARILY_UNAVAILABLE` | 같은 시도의 상태와 고정 코드를 보존. 재조회·서버 복구로 확인하며 결과 유실을 새 점수로 덮지 않음 |

### 7.1 전체 오류 코드와 고정 메시지

아래는 새 과정 오류와 재사용하는 Journey 오류의 전체 목록이다. 실제 가능한 오류는 호출 경로·상태에 따른다. 같은 HTTP의 모든 코드가 모든 경로에서 발생한다는 뜻은 아니다. `PROGRAM_ALREADY_COMPLETED`는 삭제된 `/mock/v1` 시도 생성의 legacy 이력 코드로 표에만 남아 있으며 새 요청에서는 발생하지 않는다. 완료한 일반 항목·합격한 마지막 평가의 재시작은 D130에 따라 허용되므로 `ITEM_ALREADY_COMPLETED`·`ASSESSMENT_ALREADY_PASSED`는 더 이상 존재하지 않는다.

| HTTP | `error.code` | 고정 `error.message` |
|---|---|---|
| `400` | `INVALID_REQUEST` | Invalid request. |
| `401` | `LOGIN_FAILED` | Login failed. |
| `401` | `SESSION_EXPIRED` | The session has expired. |
| `401` | `SESSION_REQUIRED` | A valid session is required. |
| `403` | `SESSION_REVOKED` | The session has been revoked. |
| `404` | `NOT_FOUND` | Not found. |
| `405` | `METHOD_NOT_ALLOWED` | Method not allowed. |
| `409` | `ASSIGNMENT_EXPIRED` | The assignment has expired. |
| `409` | `ORGANIZATION_REQUIRED` | Select an organization before starting. |
| `409` | `ATTEMPT_INPUT_CONFLICT` | The attempt already has different input. |
| `409` | `COMPLETION_POLICY_PENDING` | The completion policy is not available. |
| `409` | `CONTENT_VERSION_MISMATCH` | The content version does not match the learning start. |
| `409` | `DEFINITION_CHANGED` | The course definition has changed. |
| `409` | `EXECUTION_DEFINITION_MISSING` | The execution definition is missing. |
| `409` | `FINAL_ASSESSMENT_ACTIVE` | A final assessment is already active. |
| `409` | `FINAL_ASSESSMENT_RECOVERY_REQUIRED` | The final assessment requires recovery. |
| `409` | `IDEMPOTENCY_CONFLICT` | The request identifier has different input. |
| `409` | `INVALID_STATE` | The operation is not allowed in this state. |
| `409` | `PREREQUISITES_NOT_COMPLETED` | Prerequisite items are not completed. |
| `409` | `PROFILE_MISMATCH` | The attempt profile does not match. |
| `409` | `PROGRAM_ALREADY_COMPLETED` | This program and target are already completed. |
| `409` | `PROGRESS_RECONCILIATION_REQUIRED` | Progress reconciliation is required. |
| `413` | `PAYLOAD_TOO_LARGE` | The request exceeds the verified payload limit. |
| `413` | `PROGRESS_CAPACITY_EXCEEDED` | The progress evidence limit is exceeded. |
| `422` | `EXECUTION_DEFINITION_UNSUPPORTED` | The execution definition is not supported. |
| `422` | `MEASUREMENT_INPUT_INVALID` | The measurement input is invalid. |
| `503` | `ARC_PROGRESS_UNAVAILABLE` | Learning is waiting for ARC progress. |
| `503` | `CALCULATION_FAILED` | The calculation could not be completed. |
| `503` | `CALCULATION_OUTCOME_UNKNOWN` | The calculator outcome is unknown. |
| `503` | `CALCULATOR_CONTRACT_MISMATCH` | The calculator contract is not verified. |
| `503` | `CONTRACT_PENDING` | The integration contract is not available. |
| `503` | `STORED_INPUT_INVALID` | The stored input could not be verified. |
| `503` | `TEMPORARILY_UNAVAILABLE` | The service is temporarily unavailable. |
| `503` | `UPSTREAM_CONTRACT_MISMATCH` | The upstream data contract is not verified. |

오류 envelope의 `success`는 false, `details`는 항상 null, `timestamp`는 Utc다. 실패 envelope에 계산 결과가 섞여 오지 않는다. `GET attempt`에서 성공적으로 조회한 `failed`/`outcome_unknown` 상태와 계산 GET의 `503`을 구별한다.

## 8. 기존 앱에서 바뀌는 지점

`/mock/v1/*`와 `/cpr-analysis`는 2026-09-28 삭제되어(D103) 로컬·AWS 모든 서버에서 `404`다. 자동 redirect는 없다. 아래 대응표는 기존 앱 코드를 옮길 때의 참고용이다. 파서·계산 JSON의 상세는 [계산 입력·결과 상세 계약](ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md)에 있다.

| 기존 `/mock/v1` | 새 `/api/v2` |
|---|---|
| `login_id`, `session_token` | `loginId`, 응답 `data.accessToken` |
| 프로그램·연령 선택 `/programs` | 배정 과정 → 등록의 항목 → TrainingProgram. 실제 기존 프로그램을 ARC 과정으로 자동 변환하지 않음 |
| 생성 `client_request_id`, `catalog_version`, `program_id`, `target` | `clientRequestId`, `courseId`, `enrollmentId`, `courseItemLinkId`, `definitionHash` |
| `attempt_id`, `resume_credential` | `attemptId`, `resumeCredential` |
| cancel `user_stopped`, `manikin_disconnected` | **`user_cancelled`, `connection_lost`** |
| 계산 JSON 직접 반환·별도 attempt 평가 | 성공 envelope의 `data.calculation`, `data.evaluation`, `data.progressApplication` |
| 차트 `chart_dataset_url`, `expires_at` | chart-link의 `data.url`, `data.expiresAt` |

이미 저장된 기존 시도도 새 경로로 조회·재인가·취소·계산 복구할 수 있다(D103 호환 범위). 아직 측정을 올리지 않은 기존 시도(`created`)에는 재인가 뒤 새 경로로 첫 측정을 올릴 수 있다(D23과 같은 취지). 기존 형식의 새 시도는 만들 수 없다. 이때 `courseId`, `enrollmentId`, `courseItemLinkId`, `definitionHash`, `role`은 모두 null이다. 새 과정 시도는 이 필드가 전부 채워진다. 저장 계산 결과의 snake_case 키·숫자·null은 그대로 유지하며 새 envelope만 보고 전체를 camelCase로 변환하지 않는다.
