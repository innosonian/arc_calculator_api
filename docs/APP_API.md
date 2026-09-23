# 앱 개발자 전달용 VCC API

2026-09-18 구현을 기준으로 2026-09-19 정리한 **앱팀 전달용 Markdown 명세**다. 이 파일 안에 `/api/v2`의 **13개 경로·16개 동작, 요청·응답 자료형, 필수값·null, 전체 오류, JSON 예시**를 모았다. 별도 YAML 없이 읽고 구현할 수 있다. 예시는 합성 자료이며 실제 ARC 과정·계정·서버 주소가 아니다.

**연결할 서버는 백엔드팀과 먼저 맞춘다.** 새 과정 API는 `build_course_application`으로 명시 조립한 `course_v2`에서 제공한다. 기본 로컬·AWS 조립은 `/mock/v1`이며, AWS API/Worker에 `course_v2_dummy`를 명시하면 아래 Dev 임시 과정을 제공한다. 이 명세 작성이나 로컬 검증 완료가 배포 완료를 뜻하지 않는다. 실제 ARC/MuleSoft 인증·자료 공급·결과 전송 계약, 실물 앱·AWS 인수는 남아 있다. ARC 송신은 비활성이다.

2026-09-22 추가한 AWS Dummy Dev 카탈로그 `arc-dummy-dev-v1`에는 기존 5개 프로그램×성인·소아·영아의 **15개 임시 과정**이 있다. 이름에 `[Dummy Dev]`가 붙고 각 과정은 훈련 1개→마지막 평가 1개다. 이 카탈로그에는 영상·문서 자료가 없으며 아래 콘텐츠 API 설명은 해당 자료를 공급하는 별도 조립의 계약이다. 앱은 과정 목록에서 실제 ID와 `definitionHash`를 받아 사용한다. 실제 ARC 배정이나 공식 수료 과정으로 표시하지 않는다. 점수는 업로드한 실제 바이너리로 계산하고 CPR 완료는 계속 `pending_policy`다. Dummy 계산 결과의 `submit_arc`는 `status="excluded"`, `ok=false`, `error=null`이며 외부로 보내지 않는다. 기본 제외 사유는 `exclusionReasons=["dummy"]`이고, 계산 확정 전 진도 초기화 등의 조건에서는 `progress_reset_before_result` 같은 사유도 함께 들어간다. 요청·응답 경로와 자료형은 기존 명세와 같다. 공용 Dummy 로그아웃 뒤 다른 활성 세션은 기존 결과를 계속 조회할 수 있지만, 새 공유 진도는 `POST /api/v2/session/refresh/` 후 다시 시작한다.

**2026-09-23 AWS Dev 연결 정보**

| 항목 | 값 |
|---|---|
| 환경 | Dummy Dev · 오하이오 `us-east-2` |
| 서버 주소 | `https://2ftxmdtrx1.execute-api.us-east-2.amazonaws.com/dev` |
| 로그인 요청 | `POST https://2ftxmdtrx1.execute-api.us-east-2.amazonaws.com/dev/api/v2/sessions/` |
| Dummy 로그인 | `test@test.com` / 비밀번호 문자열 `2222` |
| 초기 업로드 원본 한도 | CPR+AED 합계2MiB(2,097,152 bytes) |
| 초기 Gateway 요청량 | 초당10회·burst20. 엄격한 처리량/비용 보장이 아니며429 처리 필요 |

사용자 실행으로 HTTPS 로그인/조회, 기존 압박 자료31380bytes의 일반 훈련·최종평가 계산(각101회·100점)과 과정 FINISHED를 확인했다. 일반 훈련 원본/최종 결과/운용 로그·서명 차트 다운로드·익명 접근403도 확인했다. 앱의 실제 파일·기기와 다른 훈련 종류, 소유권/재전송/실제 만료·장애·용량/비용을 포함한 전체 AWS 인수는 진행 중이다. 접속 주소의 `/dev`와 각 API의 마지막 `/`를 유지한다.

현재 시험한 성인 압박 Only 과정은 공유 진도에서 이미 완료됐다. 같은 등록의 새 일반 훈련/합격 후 최종평가 시작 거절은 정상이다. 공용 Dummy 로그아웃은 다른 기기와 공유하는 진도를 초기화하므로 앱의 자동 로그아웃이나 무조건 초기화로 이를 우회하지 않는다. 과정·등록·항목 ID는 아래 일반 계약대로 실제 목록/상세 응답에서 사용한다.

목차: [1. 공통 계약](#1-공통-계약) · [2. 경로·세션·과정·TrainingProgram](#2-경로와-기본-흐름) · [3. 시작·멱등성](#3-시작과-재전송) · [4. 콘텐츠 보고](#4-영상문서-진도-보고) · [5. 측정·업로드](#5-측정-시도와-업로드) · [6. 계산·완료·차트](#6-결과완료차트) · [7. 취소·복구·오류](#7-취소복구전체-오류) · [8. 기존 앱 이행](#8-기존-앱에서-바뀌는-지점)

## 1. 공통 계약

- 모든 새 경로의 마지막 `/`가 필수다. 빠뜨리면 redirect 없이 `404`; 알려진 경로의 잘못된 method는 `405`다.
- API Gateway stage를 사용하는 서버 주소에는 stage 경로까지 포함한다. 예를 들어 서버 주소가 `https://example.execute-api.us-east-2.amazonaws.com/dev`이면 `/api/v2/session/`의 요청 주소는 `https://example.execute-api.us-east-2.amazonaws.com/dev/api/v2/session/`이다. 목록의 `next`·`previous`도 API 기준 경로이므로 서버 주소 뒤에 연결하며 `/dev`가 사라지는 일반 URL 루트 병합을 피한다. 서버 주소는 백엔드팀이 전달한 고정 주소를 사용한다.
- 로그인 외 모든 API는 `Authorization: Bearer <accessToken>`을 요구한다. 발급받은 차트 URL 직접 GET만 예외다. 토큰·복구 증표·서명 URL을 로그, query, 분석 수집기에 기록하지 않는다.
- 제어 요청은 UTF-8 `application/json`이다. 필수 키 누락, 알 수 없는 키, 중복 키, `NaN`/`Infinity`, 잘못된 자료형은 거절한다. GET·DELETE에는 body를 보내지 않는다. 계산 업로드 형식은 5절을 따른다.
- 허용된 query 외에는 보내지 않는다. 중복·빈 값·다중값을 허용하지 않는다. 숫자 query는 선행 `0`, `+`, 공백 없는 양의 십진 문자열이다.
- 공개 ID는 `1..9007199254740991`의 JSON integer다. UUID는 소문자, 정의 hash는 소문자 hex 64자리, 시간은 UTC RFC3339다. 문자열·숫자·boolean·`null`을 임의 변환하지 않는다. `null`은 키 생략과 다르다.
- 세션은 로그인부터 24시간이며 조회로 연장되지 않는다. 앱 재실행 시 재로그인은 앱이 처리한다. 같은 계정도 다른 세션에 연결된 시도는 자동 조회할 수 없다.

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

성공은 다음 envelope다. `204`만 본문이 없다. 응답 헤더의 `X-Request-Id`는 문의에 사용할 서버 추적 ID이며 `Cache-Control: no-store`다.

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
| `POST /api/v2/sessions/` | `loginId`, `password`: string | `201 LoginSession` |
| `GET /api/v2/session/` | 없음 | `200 Session` |
| `POST /api/v2/session/refresh/` | 정확히 `{}` | `200 Session` |
| `DELETE /api/v2/session/` | 없음 | `204` |
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

로그인의 `data`:

```json
{
  "sessionId": "60000000-0000-4000-8000-000000000001",
  "expiresAt": "2026-09-19T00:00:00Z",
  "learningAvailability": {"state":"waiting","reason":"contract_pending"},
  "accessToken": "<발급된 불투명 토큰>",
  "tokenType": "Bearer"
}
```


`LoginRequest`의 `loginId`·`password`는 빈 문자열을 허용하지 않는다. `Session`의 `sessionId`는 `Uuid`, `expiresAt`은 `Utc`, `learningAvailability`는 아래 상태 객체다. `LoginSession`만 빈 문자열이 아닌 `accessToken`과 고정 문자열 `tokenType="Bearer"`를 추가한다. 아래 두 필드를 세션 GET/refresh에 기대하지 않는다.

`GET session`과 `POST session/refresh`는 `sessionId`, `expiresAt`, `learningAvailability`만 반환한다. `accessToken`은 로그인 응답에만 있다. 로그인과 refresh만 공급자 자료를 갱신하며 GET은 저장된 자료를 읽는다. refresh는 토큰 갱신 API가 아니다.

| `learningAvailability.state` | `reason` | 앱 처리 |
|---|---|---|
| `ready` | `null` | 해당 등록의 조건을 확인하고 새 학습 가능 |
| `waiting` | `arc_progress_unavailable` 또는 `contract_pending` | 새 학습 대기. 기존 접수 작업·결과는 보존 |
| `reconciliation_required` | `progress_reconciliation_required` | 진도 의미 확인 필요. 완료나 합격을 앱에서 보정하지 않음 |

로그인 `201`·refresh `200`이어도 `waiting`일 수 있다. 배정 목록 자체를 확인할 수 없으면 과정 GET은 `503`이며 이를 “배정 없음”으로 표시하지 않는다. 정상적으로 배정 0개인 경우에만 빈 목록과 `ready`다. 세션 상태는 전체 배정을 집계하지만 새 시작은 해당 등록의 상태를 다시 검사한다. 자동 refresh 주기나 polling 간격은 아직 계약하지 않았다.

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
| `status` | `NOT_STARTED`, `IN_PROGRESS`, `FINISHED`, `CERTIFIED` |
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

`Enrollment`:

| 필드 | 자료형 / 의미 |
|---|---|
| `id`, `courseId` | 각각 `Id` |
| `status`, `courseTitle` | 각각 string. 공급자 표시 metadata |
| `loginAt`, `finishedAt` | 각각 string 또는 null. 서버 `Utc`와 달리 현재 표시 계약은 문자열만 검증 |
| `elapsedSeconds` | integer 또는 null |
| `centerName`, `enrollStatusCode` | 각각 string 또는 null |

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
        "isPassed": null
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
        "isPassed": null
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
        "isPassed": null
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
        "isPassed": null
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
        "isPassed": null
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
      "enrollStatusCode": null
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

`TrainingSettings`의 다음 키는 **전부 필수**지만 값은 모두 nullable이다. 상위 중첩 객체가 null이 아니면 그 아래 표의 키를 모두 포함한다.

| `training` 필드 | 자료형 |
|---|---|
| `manikinType` | string 또는 null |
| `duration`, `compressionLimit`, `ventilationLimit`, `cycleLimit` | 각각 integer 또는 null |
| `compressionVentilationRatio` | 아래 비율 object 또는 null |
| `aed` | 아래 AED object 또는 null |
| `cprGuideline` | 아래 가이드라인 object 또는 null |
| `twoRescuers` | 아래 두 구조자 object 또는 null |

| 중첩 객체 | 모든 필수 하위 필드와 자료형 |
|---|---|
| `compressionVentilationRatio` | `title`: string 또는 null, `cvrVentilation`: integer 또는 null, `cvrCompression`: integer 또는 null |
| `aed` | `id`: Id, `cprGuide`·`shockMode`·`language`: 각각 string, `scenarioNo`·`volume`·`arrivalSeconds`: 각각 integer, `padsDetection`: boolean. 이 하위 값들은 null 불가 |
| `twoRescuers` | `cycleChangeCount`: integer. null 불가 |
| `assessment` | `passThreshold`: object. null 불가 |
| `assessment.passThreshold` | `cpr`, `aed`: 각각 integer 또는 null |

`cprGuideline`이 object일 때의 전체 필드:

| 필드 | 자료형 |
|---|---|
| `title`, `manikinType`, `name` | 각각 string 또는 null |
| `compressionDepthMax`, `compressionDepthMin` | 각각 integer 또는 null |
| `compressionRateMax`, `compressionRateMin` | 각각 integer 또는 null |
| `ventilationVolumeMax`, `ventilationVolumeMin` | 각각 integer 또는 null |
| `ventilationRateMax`, `ventilationRateMin` | 각각 integer 또는 null |
| `compressionDepthMaxInch`, `compressionDepthMinInch` | 각각 number 또는 null. 정수/소수 모두 가능 |

표시 설정의 단위, 제한값의 0/null 의미, 여러 종료 조건의 우선순위는 실제 ARC 계약 확인 대상이다. 앱이 이 설정으로 업로드용 `condition`을 재구성하지 않는다. 아래는 모든 필드를 포함하는 합성 훈련 항목의 `200` 예시다. 실제 ARC 설정값을 뜻하지 않는다.

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
| `{"type":"document_displayed"}` | 문서가 실제 표시된 근거. 이것만으로 완료하지 않음 |
| `{"type":"document_confirmed","displayReportId":"<표시 보고 UUID>"}` | 같은 START·버전의 표시 보고를 참조하는 읽음 확인. 확인이 먼저 도착하면 근거를 보관하고 표시 보고 수신 후 재평가 |

영상 완료 버튼 이벤트는 현재 API에 없다. 전체 재생 방식으로 연결한다. 실제 콘텐츠 접근·앱 관측 방식은 앱팀과 공급자의 인수 대상이다. 단순히 영상 끝 위치로 이동한 것을 재생 구간으로 보고하지 않는다.

진도 보고 응답 `ProgressReceipt`의 `startId`·`reportId`는 `Uuid`, `courseItemLinkId`는 `Id`, `isCompleted`는 boolean, `isPassed`는 null, `courseStatus`는 2.2절 상태 enum, `application`은 아래 적용 상태 문자열이다. 초기화 전 근거의 nullable 예외는 아래 설명을 따른다. 응답의 모든 키가 필수다. 예시:

```json
{
  "startId": "40000000-0000-4000-8000-000000000001",
  "reportId": "30000000-0000-4000-8000-000000000001",
  "courseItemLinkId": 1001,
  "isCompleted": true,
  "isPassed": null,
  "courseStatus": "IN_PROGRESS",
  "application": "applied"
}
```

`application`은 `applied`, `unchanged`, `pending_evidence`, `pending_reconciliation`, `historical_only` 중 하나다. 마지막 값은 초기화 전 근거만 보관했다는 뜻이며 이때 `isCompleted`, `isPassed`, `courseStatus`는 **모두 null**이다. 현재 진도를 지운다는 뜻이 아니다. 같은 보고 재전송은 당시 응답을 반환하므로 **최신 진도는 과정 GET으로 확인**한다.

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

완료한 일반 훈련은 새로 시작할 수 없다. 미완료 일반 훈련은 같은 항목·다른 항목의 동시 측정을 허용한다. 마지막 평가는 앞선 모든 항목 완료가 필요하고 같은 등록에서 한 시도만 진행한다. 불합격 후 재응시는 횟수 제한이 없지만 합격 후에는 불가하다. 계산·판정 중이나 복구/정책 확인 중에는 새 응시를 임의 생성하지 않는다.

`POST /api/v2/attempts/{attemptId}/calculation/`은 **누적 실제 바이너리**를 받는다. Dummy는 가짜 점수를 뜻하지 않는다. `X-Attempt-ID`를 추가할 필요가 없고 경로의 ID를 사용한다.

| multipart part | 필수 | 내용 |
|---|---|---|
| `rawHexBPfile` | 예 | CPR 원본 binary 파일 bytes |
| `condition` | 예 | 위 객체를 JSON 문자열로 직렬화 |
| `aedHexBPfile` | 아니요 | 수집한 AED binary 파일 bytes |
| `vp_event_list` | 아니요 | 기존 VP 이벤트 JSON array 문자열. 없으면 생략 또는 `[]` |

boundary는 HTTP 라이브러리가 만들게 한다. 파일을 hex 문자열이나 일반 JSON body로 바꾸지 않는다. VP 이벤트는 압박 시작/종료 `0/1`, 호흡 `10/11`, AED `20/21`이며 `timestamp`·`last_timestamp` 단위는 ms다. 기존 우선순위는 truthy인 `last_timestamp`다. `last_timestamp`가 `0`, `null`이거나 없으면 `timestamp`를 사용한다.

호환용 base64 form도 유지한다. 파일 bytes를 URL-safe base64 `cpr_b64_data`/선택 `aed_b64_data`로 만들고, `condition`·선택 `vp_event_list`의 JSON 문자열과 함께 **form URL 인코딩 → 전체 percent 인코딩 → 전체 URL-safe base64 인코딩**한다. 이 최종 문자열을 `application/x-www-form-urlencoded`로 보낸다. 일반 form body·일반 JSON·Swagger의 자동 form 생성과 다르다. 새 앱은 multipart를 사용한다. 기존 파서의 추가 호환 입력이 필요한 경우 [상세 계산 계약](ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md)을 확인한다.

측정 시작에 첫 압박 전 기준선 패킷을 포함한다. 서버는 그 뒤 카운터 변화부터 압박을 센다. 호흡 종료는 최고값 대비 성인·소아 10mL, 영아 5mL 이상 감소가 두 연속 패킷에서 확인돼야 한다. 영아 5mL는 잠정이며 실물 확인이 남아 있다. 종료 확인 패킷까지 기록하고 미확정 마지막 호흡을 앱이 임의 추가하지 않는다. 같은 패킷의 압박·호흡은 각각 처리한다.

시험 호출 예시다. `VCC_API_BASE`는 팀에서 받은 서버 주소이며, 인증 헤더 파일과 실제 바이너리는 저장소 밖의 접근 제한된 위치에 둔다. 토큰을 명령 인자에 직접 적지 않는다.

```sh
curl --request POST "$VCC_API_BASE/api/v2/sessions/" \
  --header 'Content-Type: application/json' \
  --data '{"loginId":"test@test.com","password":"2222"}'

curl --request GET "$VCC_API_BASE/api/v2/courses/101/progress/?enrollmentId=501" \
  --header @/path/to/private/auth.headers

curl --request POST "$VCC_API_BASE/api/v2/attempts/$ATTEMPT_ID/calculation/" \
  --header @/path/to/private/auth.headers \
  --form 'rawHexBPfile=@/path/to/measurement.bin;type=application/octet-stream' \
  --form 'condition=</path/to/condition.json;type=application/json'
```

## 6. 결과·완료·차트

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

계산 snapshot 안에 `submit_arc`를 추가하지 않는다. 제출 상태는 `data.submit_arc`에 있다. 저장된 과거 결과의 키를 새로 만들거나 자료형을 강제 변환하지 않는다.

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

`aed_score.part_scores[]`의 세부 키도 기존 계산기의 조건별 객체를 유지하며 비어 있을 수 있다. 앱이 CPR part와 같은 구조라고 가정하지 않는다.

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

`Evaluation`은 다음 키를 모두 포함한다. `goal.status`만 과거 어댑터 결과에서 생략될 수 있다.

| 필드 | 자료형 |
|---|---|
| `goal.kind` | `compressions`, `ventilations`, `cycles` |
| `goal.required` | 1 이상 integer |
| `goal.observed` | 0 이상 integer 또는 null |
| `goal.met` | boolean 또는 null |
| `goal.status` | 현재 결과는 `evaluated` 또는 `pending_policy`. 과거 결과는 키 생략 가능 |
| `score.decision` | `pass` 또는 `fail` |
| `program_completed` | boolean |
| `reason_codes` | `GOAL_POLICY_UNRESOLVED`, `GOAL_NOT_MET`, `SCORE_NOT_PASS` 중 해당 값의 string[] |

`ProgressApplication`은 `applied:boolean`, `applied_epoch:빈 문자열이 아닌 string 또는 null`, `reason:아래 고정 enum`의 세 키가 필수다. epoch는 앱이 증가시키거나 새로 만드는 값이 아니다.

완료 평가 예시:

```json
{
  "goal": {"kind":"compressions","required":60,"observed":60,"met":true,"status":"evaluated"},
  "score": {"decision":"pass"},
  "program_completed": true,
  "reason_codes": []
}
```

Only는 실제 목표 횟수와 기존 tester Pass를 모두 충족해야 완료한다. CPR은 완전한 cycle 완료 규칙이 미정이므로 `goal.status=pending_policy`, `observed=null`, `met=null`, `program_completed=false`다. `reason_codes`는 `GOAL_POLICY_UNRESOLVED`, `GOAL_NOT_MET`, `SCORE_NOT_PASS`의 해당 항목을 포함한다. 과거 어댑터로 저장한 결과는 `goal.status`가 없을 수 있다. 완료 미정·불합격도 계산이 정상이라면 `200`이다.

계산 점수는 number 또는 null이며 소수 정밀도를 유지한다. total·part·cycle에서 키 존재 여부가 다를 수 있다. null, 누락, 0, 빈 객체를 같은 값으로 바꾸지 않는다. ARC CPR 최소량(성인·소아 압박90/호흡6, 영아 압박45/호흡6)은 점수 null 처리와 관계되며 이것만으로 완료·Pass를 판단하지 않는다. `certification.Target`의 `baby`를 업로드 `condition.target=infant`와 혼동하지 않는다. 이 필드는 ARC 공식 수료증 발급을 의미하지 않는다. 아래 계산 자료형 표와 전체 성공 응답 예시에 값의 모양을 정의했다.

`progressApplication.reason`은 `APPLIED`, `PROGRESS_RESET`, `GOAL_POLICY_UNRESOLVED`, `ALREADY_COMPLETED`, `REQUIREMENTS_NOT_MET`, `PROGRESS_RECONCILIATION_REQUIRED`다. `APPLIED`일 때만 `applied=true`와 해당 epoch가 있고, 나머지는 false/null이다. 평가가 교체되어 현재 과정에 반영할 수 없더라도 원래 결과·합격 근거는 보존된다. 앱이 새 평가 합격을 복제하지 않는다. 초기화 전 결과는 `PROGRESS_RESET`을 우선한다.

`submit_arc`는 현재 다음 둘뿐이다. 전송 성공을 표시하지 않는다.

```json
{"status":"disabled","ok":false,"error":"arc_contract_pending","exclusionReasons":[]}
```

```json
{"status":"excluded","ok":false,"error":null,"exclusionReasons":["dummy"]}
```

`submit_arc`의 `status`, `ok`, `error`, `exclusionReasons`는 네 키 모두 필수다. `ok`는 항상 false다. disabled의 error는 문자열 `arc_contract_pending`이고 사유는 빈 배열이다. excluded의 error는 null이며 사유 배열은 중복 없이 하나 이상 포함한다. 제외 사유는 `dummy`, `non_arc_guideline`, `progress_reset_before_result` 순서다. 그 외 완료·미완료 결과는 전송 대상 정책이지만 실제 제출은 계약 대기로 비활성이다.

차트는 `calculation.chart_dataset_url`을 받은 그대로 GET한다. URL 자체가 접근 권한이므로 Bearer가 필요 없고 300초 뒤 만료된다. 갱신은 `GET .../chart-link/`로 한다. `ChartLink`의 `url`은 빈 문자열이 아닌 string, `expiresAt`은 `Utc`이며 둘 다 필수다. 응답은 `{"url":"<발급 URL>","expiresAt":"<UTC>"}` 또는 검증된 차트 없음일 때 `{"url":null,"expiresAt":null}`이다. 한쪽만 null인 응답은 없다. 새 발급이 저장된 계산 응답을 바꾸지는 않는다. 로그아웃도 이미 발급된 URL의 남은 유효시간을 없애지는 않는다.

### 6.2 계산 성공 200 — 전체 JSON 예시

아래는 기존 계산 회귀자료를 사용하는 **합성 호흡 Only 예시**다. 실제 ARC 결과가 아니며 점수·동작·지표·null을 생략하지 않은 응답 모양이다. 목표 8회는 충족했지만 점수 판정은 fail이라 프로그램은 미완료다. 다른 측정의 점수·코칭·동작 수와 달라질 수 있다.

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
- Dummy 로그아웃은 해당 세션을 폐기하고 계정의 공유 진도를 초기화한다. 다른 활성 세션과 저장 결과는 유지한다. 재인가해도 옛 시도의 epoch를 새 진도로 바꾸지 않는다. 실제 ARC 학생은 로그아웃해도 진도를 보존하는 정책이며 실제 인증 연동은 별도다.

| HTTP / 대표 코드 | 앱 처리 |
|---|---|
| `400 INVALID_REQUEST` | 요청 필드·자료형·query·헤더 확인 |
| `401 LOGIN_FAILED`, `SESSION_REQUIRED`, `SESSION_EXPIRED` | 로그인 확인·재로그인, 필요 시 원래 시도 재인가 |
| `403 SESSION_REVOKED` | 폐기된 세션. 재로그인 및 필요한 재인가 |
| `404 NOT_FOUND` | 경로·배정·연결 세션 확인. 다른 소유자의 존재를 노출하지 않음 |
| `409 DEFINITION_CHANGED` | 과정 상세 재조회 후 새 시작 요청 준비 |
| `409 IDEMPOTENCY_CONFLICT`, `ATTEMPT_INPUT_CONFLICT`, `PROFILE_MISMATCH` | 같은 ID의 내용을 바꾸지 말고 원래 요청·condition 확인 |
| `409 ITEM_ALREADY_COMPLETED`, `ASSESSMENT_ALREADY_PASSED` | 새 시작 차단. 최신 진도 표시 |
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

아래는 새 과정 오류와 재사용하는 Journey 오류의 전체 목록이다. 실제 가능한 오류는 호출 경로·상태에 따른다. 같은 HTTP의 모든 코드가 모든 경로에서 발생한다는 뜻은 아니다. `PROGRAM_ALREADY_COMPLETED`는 기존 시도 호환 코드이며 새 과정 일반 항목은 `ITEM_ALREADY_COMPLETED`로 구별한다.

| HTTP | `error.code` | 고정 `error.message` |
|---|---|---|
| `400` | `INVALID_REQUEST` | Invalid request. |
| `401` | `LOGIN_FAILED` | Login failed. |
| `401` | `SESSION_EXPIRED` | The session has expired. |
| `401` | `SESSION_REQUIRED` | A valid session is required. |
| `403` | `SESSION_REVOKED` | The session has been revoked. |
| `404` | `NOT_FOUND` | Not found. |
| `405` | `METHOD_NOT_ALLOWED` | Method not allowed. |
| `409` | `ASSESSMENT_ALREADY_PASSED` | The final assessment has already passed. |
| `409` | `ATTEMPT_INPUT_CONFLICT` | The attempt already has different input. |
| `409` | `COMPLETION_POLICY_PENDING` | The completion policy is not available. |
| `409` | `CONTENT_VERSION_MISMATCH` | The content version does not match the learning start. |
| `409` | `DEFINITION_CHANGED` | The course definition has changed. |
| `409` | `EXECUTION_DEFINITION_MISSING` | The execution definition is missing. |
| `409` | `FINAL_ASSESSMENT_ACTIVE` | A final assessment is already active. |
| `409` | `FINAL_ASSESSMENT_RECOVERY_REQUIRED` | The final assessment requires recovery. |
| `409` | `IDEMPOTENCY_CONFLICT` | The request identifier has different input. |
| `409` | `INVALID_STATE` | The operation is not allowed in this state. |
| `409` | `ITEM_ALREADY_COMPLETED` | The learning item is already completed. |
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

`course_v2`에서는 `/mock/v1/*`와 `/cpr-analysis`가 `404`다. 두 API 버전을 섞거나 자동 redirect를 기대하지 않는다. 기본 서버용 기존 계약은 [상세 API 계약](ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md)에 보존한다.

| 기존 `/mock/v1` | 새 `/api/v2` |
|---|---|
| `login_id`, `session_token` | `loginId`, 응답 `data.accessToken` |
| 프로그램·연령 선택 `/programs` | 배정 과정 → 등록의 항목 → TrainingProgram. 실제 기존 프로그램을 ARC 과정으로 자동 변환하지 않음 |
| 생성 `client_request_id`, `catalog_version`, `program_id`, `target` | `clientRequestId`, `courseId`, `enrollmentId`, `courseItemLinkId`, `definitionHash` |
| `attempt_id`, `resume_credential` | `attemptId`, `resumeCredential` |
| cancel `user_stopped`, `manikin_disconnected` | **`user_cancelled`, `connection_lost`** |
| 계산 JSON 직접 반환·별도 attempt 평가 | 성공 envelope의 `data.calculation`, `data.evaluation`, `data.progressApplication` |
| 차트 `chart_dataset_url`, `expires_at` | chart-link의 `data.url`, `data.expiresAt` |

이미 저장된 기존 시도도 새 경로로 조회·재인가·계산 복구할 수 있다. 이때 `courseId`, `enrollmentId`, `courseItemLinkId`, `definitionHash`, `role`은 모두 null이다. 새 과정 시도는 이 필드가 전부 채워진다. 저장 계산 결과의 snake_case 키·숫자·null은 그대로 유지하며 새 envelope만 보고 전체를 camelCase로 변환하지 않는다.
