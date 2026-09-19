# AWS·Dev 배포와 인수 안내

2026-09-18 저장소 기준. **API·Worker·Relay의 AWS 실행 연결 코드는 구현되어 있으며, 실제 AWS 자원 연결·권한·배포·전체 경로 인수는 아직 검증하지 않았다.** 로컬 검증 결과는 [VALIDATION](VALIDATION.md)을 따른다. 이 문서의 AWS 명령은 사용자가 승인한 대상에 후속 실행할 절차다.

기본 AWS 조립은 `/mock/v1`과 인증된 `/cpr-analysis`를 제공한다. 새 과정 `/api/v2`는 `build_course_application`의 로컬 내부 계약이며, `aws_runtime.py`는 아직 이 조립을 선택하지 않는다. 공식 ARC 공급자·콘텐츠·목표·revision·제출·운영 전환 조건을 해결하고 별도 실행 연결·인수를 마쳐야 한다. 새 경로가 이미 배포된 것으로 앱팀에 안내하지 않는다.

실제 주소·계정·리전·자원·한도·비용·보관기간을 이 문서에서 정하지 않는다. 기존 계산·null·코칭, 인증·소유권·멱등성·epoch·lease/fence, 비공개 파일을 유지한다. CPR 완료는 `pending_policy`, ARC 제출은 `disabled`, 실제 HSTM 전송은 금지다. commit·push·AWS 변경·배포는 사용자 결정이다.

<a id="appendix-g"></a>
## 1. 현재 코드와 배포 범위

| 역할 | 실행 진입점·연결 | 남은 확인 |
|---|---|---|
| API | `lambda_handler.run` → AWS 조립 → DynamoDB/S3·세션·접수/조회 | 전체 REST 경로·개인 접근·바이너리 처리·실제 권한 |
| Worker | `mock_journey.worker.run` → 내부 계산기·DynamoDB/S3·lease 갱신 | SQS event source mapping·시간/메모리·중복/복구 인수 |
| Relay | `mock_journey.dispatch.run` → DynamoDB/SQS·진행 위치 저장 | Stream/예약 실행·진행 행 최초 생성·실제 권한 |
| ARC 제출 | 비활성 응답 | 공식 인증·제출 문서·접수 확인·멱등/재시도 계약 |

기준 코드: [aws_runtime.py](../mock_journey/aws_runtime.py), [aws_settings.py](../mock_journey/aws_settings.py), [assembly.py](../mock_journey/assembly.py). 설정 누락 시 메모리 DB·가짜 계산·로컬 Java DB로 대체하지 않는다. 로컬 서버 감독기·DynamoDB Local·가상환경은 Lambda에 넣지 않는다.

기존 REST API + Python 3.12 Lambda + DynamoDB + 비공개 S3 구성을 재사용한다. 전체 Journey에는 SQS와 Relay/Worker 연결도 필요하다. 함수 개수와 기존 함수 재사용 여부는 실제 배치 확인 후 정한다. 단일 Calculator/Gateway 스크립트 실행으로 전체 인프라가 완성되지 않는다.

<a id="aws-personal-access"></a>
## 2. 기존 IAM 사용자로 로그인

사용자가 확인한 로그인 방식은 계정 ID/별칭·IAM 사용자 이름·비밀번호다. 기존 접근을 사용하며 SSO 설정이나 별도 관리 Role 생성을 선행 조건으로 삼지 않는다. 아래의 `<...>`는 승인된 실제 값으로 바꾼다. 로그인 리전과 자원 리전은 다를 수 있다.

```sh
cd /Users/mac/arc_calculator_api
aws --version
aws configure list-profiles
aws login --profile arc-console --region '<로그인 리전>'
```

기존 안내의 전제는 AWS CLI 2.32.0 이상이다. 브라우저에서 기존 IAM 사용자와 이중 인증으로 로그인하고 터미널의 완료를 확인한다. `arc-console`은 예시 프로필 이름이므로 기존 다른 용도와 겹치지 않게 선택한다. 새 Access Key를 만들어 문서·대화에 전달하지 않는다. [AWS CLI 로그인 안내](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)

직접 작업 권한을 받은 IAM 사용자라면 다음을 사용한다.

```sh
export AWS_PROFILE=arc-console
```

별도 관리 Role을 받은 경우에만 다음 경로를 사용한다. 관리 Role은 개인 작업용이며 Lambda 실행 Role과 다르다. 기존 프로필을 덮어쓰지 않도록 이름을 확인한다.

```sh
aws configure set role_arn '<관리 Role ARN>' --profile arc-dev-admin
aws configure set source_profile arc-console --profile arc-dev-admin
export AWS_PROFILE=arc-dev-admin
```

별도 MFA가 요구되면 안내받은 장치 ARN/일련번호로 해당 프로필의 `mfa_serial`을 설정한다. MFA 코드는 터미널 인증에만 입력한다. [Role 프로필 설정](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html)

```sh
export AWS_DEFAULT_REGION='<Dev 자원 리전>'
export AWS_PAGER=''
aws sts get-caller-identity --query '{Account:Account,Identity:Arn}' --output table
```

허용된 계정인지 확인한다. Role을 선택했다면 `Identity`도 해당 역할과 대조한다. 신원 조회 성공은 자원 변경 권한 확인이 아니다. 필요한 조회가 거절된 항목과 운영 공유 여부가 불명확한 항목만 담당자에게 확인한다.

<a id="aws-self-inventory"></a>
## 3. 기존 자원과 연결 조사

다음 조회는 사용자가 자신의 권한으로 수행할 수 있다. 이름에 `dev`가 있어도 운영과 공유하는지 확인해야 한다. 빈 목록이면 계정·리전을 먼저 대조한다.

```sh
aws apigateway get-rest-apis --query 'items[].{Name:name,ID:id}' --output table
aws apigatewayv2 get-apis --query 'Items[].{Name:Name,ID:ApiId,Type:ProtocolType}' --output table
aws lambda list-functions --query 'Functions[].{Name:FunctionName,Role:Role}' --output table
aws dynamodb list-tables --query TableNames --output table
aws s3api list-buckets --page-size 1000 --query 'Buckets[].Name' --output table
aws sqs list-queues --page-size 1000 --query QueueUrls --output table
```

S3 목록은 계정 전체이며 버킷 리전은 별도로 확인한다. REST API를 찾았다면 실제 Stage·경로·대상 함수를 조회한다. REST API가 없거나 HTTP/WebSocket API만 있다면 이 명령을 억지로 적용하지 않고 연결 설계를 확인한다.

```sh
aws apigateway get-stages --rest-api-id '<API ID>' --query 'item[].{Stage:stageName,Deployment:deploymentId}' --output table
aws apigateway get-resources --rest-api-id '<API ID>' --query 'items[].{Path:path,ID:id,Methods:resourceMethods}' --output json
aws apigateway get-integration --rest-api-id '<API ID>' --resource-id '<경로 ID>' --http-method POST --query uri --output text
aws lambda get-function-configuration --function-name '<대상 함수>' --query '{Name:FunctionName,Role:Role,Handler:Handler,Runtime:Runtime}' --output table
```

POST는 해당 경로의 실제 method로 바꾼다. 조회한 편집 설정이 Stage에 아직 배포되지 않았을 수 있으므로 실제 Deployment ID도 대조한다. 비밀 없는 연결표에는 계정·리전·API/Stage·함수/Handler/Role·DB·S3 경로·Queue·Stream/예약 실행·공유 영향·현재 제한을 기록한다. 전체 환경변수·키·토큰·서명 URL은 공유하지 않는다.

## 4. 역할별 설정 작성과 오프라인 검사

[.env.example](../.env.example)은 실행 가능한 운영 기본값이 아니다. `var/deployment/api-runtime.json`, `worker-runtime.json`, `relay-runtime.json`에 각각 하나의 역할 JSON을 준비한다. JSON 공통 필드와 해당 역할 필드만 넣으며 중복 key·알 수 없는 필드·숫자 자리에 boolean·NaN/Infinity는 거절된다. 아래 한도는 코드의 입력 제약이며 운영값 승인이 아니다.

| JSON 필드 | 필수 형식·제약 |
|---|---|
| `schema`, `role` | 정수 `1`; `api` / `worker` / `relay` 중 해당 역할 |
| `account_id`, `partition`, `region` | 12자리 계정 문자열; `aws` / `aws-cn` / `aws-us-gov`; partition과 일치하는 실제 리전 |
| `environment` | 영문·숫자·밑줄·점·하이픈 1~128자. 기존 서명 namespace 유지 |
| `state` | `table_name`, `max_conflict_retries`(정수 1~8). SDK 재시도와 별개 |
| `sdk` | `connect_timeout`, `read_timeout`은 양수 초; `total_max_attempts`는 최초 시도 포함 양의 정수; `retry_mode`는 `standard` 또는 `legacy` |
| `logs` | `capacity`(1~4096), `max_bytes`, `flush_budget_ms`, `response_reserve_ms`는 양의 정수; `sdk`는 로그용 별도 SDK 객체 |
| `storage` — API/Worker | `stage`, `bucket`, `directory`, `input_bytes`, `artifact_bytes`. byte 한도는 양의 정수, artifact≥input; AWS stage `test`/`local` 거절 |
| `execution` — API/Worker | `current_adapter_version`, `projection_version`, `retained_adapter_versions` |
| `api` — API | `payload_limit`: body 문자열 UTF-8 크기 제한, 양의 정수 bytes |
| `worker` — Worker | `lease_seconds`, `retry_seconds`: 양의 정수 초; `renewal_interval_seconds`, `renewal_timeout_seconds`: 양수 초; `processing_reserve_ms`: 양의 정수 |
| `relay` — Relay | `queue_url`, `lease_seconds`, `retry_seconds`, `page_size`, `max_pages`, `processing_reserve_ms`. 수치는 양의 정수; Queue는 같은 계정·리전의 Standard SQS |

Worker는 갱신 interval≤lease/3, interval+갱신 timeout<lease를 검사한다. Relay는 SDK timeout/시도·DB 충돌·로그/반환·시계 여유를 포함한 예산을 검사한다. `page_size × max_pages`는 각 종류의 논리적 조회 예산이며 실제 Query는 `Limit=1`이다. 설정 검사가 실제 AWS의 전체 실행시간을 보장하지는 않는다.

현재 adapter는 `arc-internal-detection-pending-v3`, projection은 `arc-local-projection-v1`이다. `retained_adapter_versions`는 `[]` 또는 `["arc-local-calculator-pending-v2"]`만 지원한다. 기존 작업을 조사한 후 선택하며, v2는 저장된 유효 후보 검증·차트 복구만 지원하고 계산을 다시 실행하지 않는다. 미지원 옛 작업을 새 버전으로 바꾸거나 가짜 Fail로 확정하지 않는다. 완료 결과는 저장 bytes로 조회한다.

새 runtime은 하나의 `region`을 DynamoDB·S3·SQS·로그 client에 적용한다. `AWS_REGION`, `AWS_DEFAULT_REGION`, `ARC_MOCK_REGION`, `ARC_STORAGE_REGION`이 함께 있으면 일치해야 한다. AWS 예약 변수는 사용자 환경파일에 추가하지 않는다. `STAGE`, `ARC_MOCK_ENVIRONMENT`, `ARC_MOCK_TABLE_NAME`도 JSON과 일치해야 한다. 교차 리전이 필요하면 연결 확장을 검토하며 실제값을 바꾸어 검사를 우회하지 않는다. `AWS_ENDPOINT_URL*` 재지정은 거절된다.

```sh
var/local-python/bin/python -m mock_journey.aws_settings --role api --config var/deployment/api-runtime.json
var/local-python/bin/python -m mock_journey.aws_settings --role worker --config var/deployment/worker-runtime.json
var/local-python/bin/python -m mock_journey.aws_settings --role relay --config var/deployment/relay-runtime.json
```

종료 0·`configuration_valid`는 형식 검사 성공이며 `aws_access_checked=false`, `secrets_checked=false`다. SDK나 AWS를 호출하지 않는다. 실제 자원·권한 검증을 대신하지 않는다.

Lambda의 `ARC_JOURNEY_CONFIG`에는 해당 JSON **문자열 전체**를 주입한다. 파일 경로나 Secret ARN은 자동 로딩되지 않는다. `.env`를 거치면 한 줄 JSON으로 넣는다. 자원·보호·트리거 준비 후 해당 함수의 `ARC_MOCK_ENABLED`를 정확한 문자열 `true`로 설정한다. 예시의 `false`는 비활성 값이다.

API에만 `ARC_MOCK_RESUME_KEYS`(버전→base64 키 JSON)와 `ARC_MOCK_RESUME_KEY_VERSION`을 승인된 비공개 경로로 주입한다. 키는 decode 후 최소 32bytes이며 현재 버전이 keyring에 있어야 한다. 이전 증표 검증용 키를 임의로 삭제하지 않는다. Worker/Relay에 keyring을 복사하지 않는다. 현재 코드는 Secrets Manager 직접 조회를 구현하지 않는다.

Lambda 환경 갱신은 전체 Variables 목록을 교체하므로 기존 필수값까지 보호된 파일에서 합쳐 대조한다. `.env`를 `source`/`eval`하거나 화면·명령 인자에 비밀을 출력하지 않는다. `.env` 형식은 한 줄 `KEY=VALUE`, LF/CRLF이며 `export`·중복 key·예약 key·감싸는 따옴표를 거절한다. key는 영문자로 시작하는 2자 이상 영문·숫자·밑줄, key/value UTF-8 합계는 4096bytes 이하를 검사한다. 설정 파일·원본·키는 Git/ZIP에 넣지 않는다.

## 5. DB·파일·API·작업 전달 연결

| 대상 | 필수 연결과 보호 |
|---|---|
| DynamoDB | `PK`/`SK`: String; `GSI1`의 `GSI1PK`: String, `GSI1SK`: Number, Projection=`ALL`. 공유 영향과 기존 자료를 확인한 뒤 필요한 변경만 적용 |
| S3 | 승인 bucket/directory/stage, Block Public Access·권한·암호화·기존 lifecycle 확인. 원본·후보·결과·metadata는 비공개 |
| API Role | 상태/작업/Outbox DB와 허용 입력·결과·차트 S3. 앱 Bearer를 서버까지 유지 |
| Worker Role | DB lease/fence·최종 거래, 입력/후보/결과/차트 S3, SQS 수신·삭제·큐 속성. 수신은 event source mapping 담당 |
| Relay Role | Outbox·due DB/인덱스 조회·전달 상태 변경·진행 행 조회/갱신·정확한 Queue 전송. API 키와 S3 권한 불필요 |
| 로그 | 같은 명시 테이블에 별도 bounded writer/client로 저장. 실행 Role의 실제 OPS 쓰기와 CloudWatch 수집은 각각 확인 |

개인 외부 접근 보호는 공개된 Dummy 계정과 별개다. 로그인 경로부터 보호하고 개인 권한 회수·환경 간 접근 거절을 시험한다. HTTPS, 요청별 소유권, 최소 권한과 Dev/Beta/Prod 상태·Queue·키·파일 분리를 유지한다. `environment` 이름만으로 같은 DB의 훈련 자료가 격리되지는 않는다.

차트는 소유권 검사 뒤 비공개 S3의 300초 서명 URL을 발급한다. 임시 자격증명이 먼저 만료되면 더 짧아질 수 있으며, 발급된 링크는 앱 로그아웃과 독립적으로 자체 유효기간 동안 읽힐 수 있다. 원본·metadata에 공개 URL을 발급하지 않는다. 만료·재발급은 실제 앱에서 확인한다.

REST API는 [상세 API 계약](ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md)의 모든 활성 경로와 method를 연결한다. 기본 구성은 다음과 같다. `/api/v2` 연결은 1절의 별도 전환 조건을 따른다.

| Method | 기본 AWS 경로 |
|---|---|
| POST | `/mock/v1/sessions` |
| GET, DELETE | `/mock/v1/session` |
| GET | `/mock/v1/programs` |
| POST | `/mock/v1/attempts` |
| GET | `/mock/v1/attempts/{attempt_id}` |
| POST | `/mock/v1/attempts/{attempt_id}/reauthorize` |
| POST | `/mock/v1/attempts/{attempt_id}/cancel` |
| POST, GET | `/mock/v1/attempts/{attempt_id}/calculation` |
| GET | `/mock/v1/attempts/{attempt_id}/chart-link` |
| POST | `/cpr-analysis` — 단일 `X-Attempt-ID`가 필요한 인증된 별칭 |

`/healthz`는 로컬 감독기 경로이며 AWS health endpoint로 구현되어 있지 않다. API의 바이너리 설정·`isBase64Encoded`, 원본 bytes의 hash, JSON 로그인/조회까지 함께 확인한다. multipart·base64/event 포장·결과 JSON을 포함한 실제 제한을 측정한다. 로컬 기본 한도가 API Gateway/Lambda 전송 가능 크기를 보증하지 않는다. 앱 30초 대기는 업로드 시작부터이며 접수된 작업의 취소 시간과 다르다.

SQS body에는 `job_id`만 전달한다. Stream은 새 Outbox를 전달하고 예약 실행은 `source=aws.events` 사건으로 due 작업을 재확인한다. SQS와 Stream의 `ReportBatchItemFailures`를 활성화하며 실패 식별자는 각각 messageId와 SequenceNumber다. Queue visibility·batch window·함수 timeout·DB lease는 구별해 실제 계산/갱신시간과 맞춘다. [SQS 트리거 설정](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)

DLQ 도착은 DB Job 취소나 ARC 제출 완료가 아니다. 중복 전달을 정상 가능성으로 처리하고 기존 멱등성·epoch·fence를 유지한다. 전송 후 위치 저장 전 중단되면 다시 알림이 갈 수 있다. 계속되는 장애·시간 부족에 일정 시간 내 완료를 보장하지 않는다.

## 6. Relay 진행 기록 최초 준비

예약 Relay는 기존 DB의 `RELAY_SCAN#<환경 이름의 SHA-256>` / `PROGRESS#v1` 한 행에 OUTBOX/JOB 다음 위치와 순서를 보관한다. 계정·리전·테이블·환경·Queue 연결 지문을 대조한다. 행이 없거나 손상되거나 연결이 바뀌면 자동 생성·초기화하지 않는다. 업무 본문·비밀·TTL·due 색인 필드를 넣지 않는다.

역할에 필요한 추가 권한은 승인 테이블의 정확한 진행 PK에 대한 강한 일관성 `GetItem`과 조건부 `PutItem`이다. `dynamodb:LeadingKeys`를 쓰는 정책은 `ForAllValues`를 사용하며 PK만 제한한다. SK/schema/조건부 갱신은 코드가 검증한다. 이 권한을 API/Worker에 복사하지 않는다.

검사한 Relay JSON으로 **존재하지 않는 새 출력 파일**을 준비한다.

```sh
var/local-python/bin/python -m mock_journey.relay_init --config var/deployment/relay-runtime.json --output var/deployment/relay-progress-init.json
```

종료 0·`initialization_request_prepared`는 비공개 0600 파일 작성 성공이다. `aws_access_checked=false`, `aws_write_performed=false`를 확인한다. 기존 파일은 덮지 않는다. 실패한 설정이나 기존 행을 삭제하고 재시도하지 않는다.

파일의 TableName·PK/SK·연결 지문이 승인 환경과 일치하는지 확인한다. 최초 위치는 비어 있고 `next_kind=OUTBOX`, `owner=null`, `lease_until=revision=fence=0`이다. `ConditionExpression=attribute_not_exists(PK)`를 제거하지 않는다.

사용자가 실제 쓰기 범위를 승인한 후, 해당 예약 실행을 비활성으로 둔 상태에서만 실행한다.

```sh
aws dynamodb put-item --profile '<APPROVED_CLI_PROFILE>' --region '<APPROVED_REGION>' --cli-input-json file://var/deployment/relay-progress-init.json
```

정확한 PK/SK로 행을 확인한 뒤 승인된 Relay 버전·설정에 예약 대상을 연결한다. 기존 행이 있으면 조건부 실패가 정상 보호다. 기존 행의 소유권·schema·위치·연결 지문을 점검하며 자동 초기화하지 않는다. 항목별 조회·위치 저장으로 DB 호출과 비용이 늘어나므로 실제 backlog/지연을 측정한다.

## 7. 기존 Calculator/Gateway 배포 도구

[bindings.example.json](../deploy/bindings.example.json)은 runtime JSON과 다른 **배포용 연결표**다. 최상위는 `schema_version: 1`, `environments`이며 알려진 실제 환경만 넣는다. `dev/development`→`development`, `prod/production`→`production`, `beta`는 명시 항목만 사용한다. `local` AWS 배포는 거절한다. Beta env 경로도 명시한다.

| 연결표 필드 | 계약 |
|---|---|
| 환경 | `account_id`, `region`, `runtime_stage`, `calculator`, `gateway`. stage는 env `STAGE`와 일치 |
| Calculator 이름 | 기존 `function_name`, `role_name`; 경로 있는 Role은 전체 `role_arn` 추가. 계정·마지막 이름과 일치 |
| Calculator 한도 | `memory_mb` 정수 128~10240, `timeout_seconds` 정수 1~900. 실제 계정 한도 별도 확인 |
| Calculator 유지값 | `log_retention_days`: 지원 일수 또는 `null`; `reserved_concurrency`: 0 이상 정수 또는 `null`. null은 기존 설정 유지 |
| Calculator 저장 | `storage_bucket`, `storage_prefix`는 `util/uploader.py` 보존 상수와 일치; `storage_region`은 env `ARC_STORAGE_REGION`과 일치 |
| Gateway | `api_id`, `stage`, `route=/cpr-analysis`, `rate_limit`, `burst_limit`. 제한은 둘 다 숫자 또는 둘 다 null |

JSON 중복·알 수 없는 필드·잘못된 타입을 거절한다. 자리표시자를 채워 통과시키려 실제 경로·운영값을 추정하지 않는다. 이 연결표를 작성하는 것만으로 저장 대상이 바뀌거나 Worker/Relay 자원이 연결되지 않는다. shell 변수로 연결표를 덮어쓸 수 없으며 env의 Gateway 식별자도 함께 있으면 일치해야 한다.

```sh
umask 077
mkdir -p var/deployment
cp -n .env.example .env.dev
cp -n deploy/bindings.example.json var/deployment/bindings.json
chmod 600 .env.dev var/deployment/bindings.json
```

기존 사본을 보존하며 편집기에서 실제값을 입력한다. 형식 검사는 AWS를 호출하지 않는다.

```sh
var/local-python/bin/python scripts/deployment_preflight.py --environment dev --component calculator --bindings var/deployment/bindings.json --env-file .env.dev
var/local-python/bin/python scripts/deployment_preflight.py --environment dev --component gateway --bindings var/deployment/bindings.json --env-file .env.dev
```

`syntax_and_composition_valid`는 형식 통과다. `aws_resources_verified=false`, `runtime_verified=false`이며 역할 runtime 검사·실자원 인수와 별개다. `.env`는 로컬 서버에 자동 로드되지 않는다.

## 8. 검증·빌드·배포 순서

1. 변경 전 코드/설정·키 버전·API Deployment ID·Queue/Stream/예약 연결·DB/S3 백업과 진행 작업을 보호된 위치에 기록한다.
2. 선택 소스를 사용자 서버와 분리한 Python 3.12 검증 환경에서 검사한다. [LOCAL_RUN](LOCAL_RUN.md)의 준비를 따르고 사용자 DB·키를 시험 입력으로 쓰지 않는다.
3. Linux/Python 3.12 대상 ZIP을 만들고 의존성·파일 목록·Handler·회귀를 검사한다. Mac import 성공과 실제 Lambda 인수를 구별한다.
4. 승인된 DB/S3/Queue·개인 보호·진행 행·실행 Role·트리거를 준비한다. API/Worker/Relay 설정과 timeout/lease를 대조한다.
5. 승인된 코드와 전체 설정을 배포하고 모든 REST method를 연결한다. 공유 API의 다른 미배포 변경까지 Stage에 포함되는지 확인한다.
6. 실제 Dev 인수를 통과한 주소·서버 버전·활성 계약만 앱팀에 전달한다. Beta/Prod 전환은 별도 결정한다.

전체 로컬 검증은 준비된 전용 검증 Python과 검증된 DynamoDB Local 프로그램으로 실행한다.

```sh
STAGE=test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 var/validation-python/bin/python scripts/validate_local_integration.py --dynamodb-home var/dynamodb-local-3.3.1 --suite all
```

필수 의존성은 `requirements-local.txt`와 `requirements-ci.txt`이며 `requirements-dev.txt`만으로 전체 검증 준비가 끝나지 않는다. `docs/local_server/DYNAMODB_DISTRIBUTION_MANIFEST.json`의 배포물 지문을 유지한다. 시험 종료 코드·실패/skip/예상 실패·소스 버전을 구분해 기록한다.

다음은 새 빌드 폴더에서 x86_64용 의존성과 ZIP을 준비하는 기존 경로다. 실제 함수 아키텍처와 일치하는지 먼저 확인한다. 패키지 다운로드가 필요하며 AWS를 호출하지 않는다.

```sh
umask 077
ARC_BUILD_DIR="$(mktemp -d /private/tmp/arc-lambda-build.XXXXXX)"
python3.12 -m venv "$ARC_BUILD_DIR/python"
"$ARC_BUILD_DIR/python/bin/python" -m pip install -r requirements.txt -c constraints-lambda.txt \
  --target "$ARC_BUILD_DIR/packages" --no-compile --only-binary=:all: \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12
"$ARC_BUILD_DIR/python/bin/python" scripts/build_mock_artifact.py \
  --source-root "$PWD" --packages-dir "$ARC_BUILD_DIR/packages" --outdir "$ARC_BUILD_DIR/artifact"
"$ARC_BUILD_DIR/python/bin/python" -m zipfile -t "$ARC_BUILD_DIR/artifact/mock-lambda.zip"
```

허용 소스·필수 prompt·검사한 의존성만 ZIP에 들어간다. DB·키·가상환경·개발 기록·테스트 자료는 제외한다. builder는 ZIP 50MiB/압축해제 250MiB를 검사하고 manifest에 파일 hash·버전·미검증 범위를 기록한다. layer 합산과 실제 Linux/Lambda 실행은 별도로 확인한다.

아래 두 명령은 **기존 Calculator와 `/cpr-analysis` 갱신만** 수행한다. 사용자가 배포를 결정하고 기존 자원·권한·백업을 준비한 뒤 실행한다. Worker/Relay에 Calculator용 도구를 적용하지 않는다.

```sh
bash scripts/deploy_arc_lambda.sh development .env.dev var/deployment/bindings.json
bash scripts/deploy_arc_api_gateway.sh development .env.dev var/deployment/bindings.json
```

도구는 실제 STS 계정·기존 Role/함수·`lambda_handler.run`을 확인한다. 다른 Handler·새 자원·광범위 Invoke 권한을 자동 생성해 우회하지 않는다. Lambda 도구는 전체 환경을 교체하며 코드→설정→버전 발행은 단일 거래가 아니다. Gateway는 현재 별칭 없는 함수를 호출하므로 갱신 중에도 새 코드가 요청을 받을 수 있다. 자동 원복을 가정하지 않는다.

Gateway 도구의 throttle은 해당 POST만 변경하지만 binaryMediaTypes는 REST API 전체에, create-deployment는 Stage에 영향을 준다. 완료 메시지는 전체 Journey 인수 성공이 아니다. 로그 retention 숫자는 기존 로그를 삭제 대상으로 만들 수 있으며 null은 기존 정책 유지다. 실제 custom LogGroup을 조회해 적용하고 공유 함수 영향을 확인한다.

[배포 workflow](../.github/workflows/deploy_arc_lambdas.yml)는 `develop`/`main` push와 수동 실행으로 동작한다. 현재 선언에 별도 회귀·GitHub Environment 승인 gate가 없고 수동 Dev는 여러 브랜치에서 가능하다. 검증 workflow 통과가 자동으로 배포 승인에 연결되지 않는다. 실제 Rules/Branches·Environments·Secret·OIDC trust를 확인한 뒤 push한다. 과거 IAM JSON 예시·장기 키 fallback을 그대로 활성화하지 않는다.

## 9. 로그·인수·복구

로그 버퍼는 정제한 주요 처리 이력·상세 진단만 받는다. `accepted`는 메모리 접수, `stored`는 DB 저장 확인, `unconfirmed`는 응답 미확인, `dropped`는 누락이다. 로그 장애만으로 정상 훈련을 재실행하거나 성공 결과를 실패로 바꾸지 않는다. 업무와 같은 DB의 처리량을 공유한다.

배출 예산은 응답 지연을 추가할 수 있고 freeze/timeout/강제 종료 시 마지막 로그와 누계가 사라질 수 있다. `operational_log_batch_unconfirmed` 경고의 `unreported_dropped`/`unreported_unconfirmed`는 가능한 후속 writer가 보고를 시도하는 값이며 영구 집계 보장이 아니다. 경고 없음이 무누락을 증명하지 않는다. 실제 CloudWatch 수집과 DB OPS 조회를 각각 확인한다.

비밀번호·Bearer·복구 증표·raw body·바이너리·서명 URL을 로그에 넣지 않는다. Gateway 본문 추적도 확인한다. 보관기간 확정 전 TTL/lifecycle 자동 삭제를 새로 추가하지 않는다. 기존 보관 정책·백업·일별 파일/로그량·요청/계산시간·DB 부하와 알림 수신·비용은 실제 환경에서 확인한다.

| 실제 Dev 인수 | 확인 범위 |
|---|---|
| 접근/소유권 | 개인 접근 허용·회수, 로그인, 세션 없음·다른 시도 거절, 환경 간 접근 거절 |
| 전체 처리 | 로그인→시도→실제 누적 binary→Worker→저장 결과/차트. 원본 hash·자료형·null·코칭 유지 |
| 별도 상태 | 계산 성공·프로그램 완료·현재 진도 반영·ARC 상태 구분. Only 목표+Pass, CPR pending, ARC disabled |
| 작업 수명 | 중복/지연 Queue·Stream 실패·예약 backlog·lease/fence·로그아웃 epoch·구버전 후보·기존 결과 조회 |
| 파일/로그 | 익명 원본 거절·차트 만료/재발급·민감값 부재·로그 장애 중 훈련·저장 실패/누락 관측 |
| 복구 | 허용한 부분 장애 뒤 코드/전체 설정/키/DB 참조/S3/트리거 복원과 기존 결과 조회 |

`401/403`은 개인 접근·세션·소유권부터, `404`는 활성 경로·정확한 식별자부터 확인한다. `503`은 설정/저장/버전과 실제 장애를 구분한다. 계속 `202`면 DB Job/Outbox→Stream/예약→Queue→Worker를 확인한다. 새 Job ID 재제출·공개 bucket·관리자 권한·정답 덮어쓰기로 해결하지 않는다.

복구 시 승인된 방법으로 새 접수와 필요한 트리거만 제한하고 기존 Job·원본·후보·결과를 보존한다. 이전 코드가 현재 저장 schema/버전을 읽는지 확인한 뒤 코드·전체 환경·키·API/트리거를 복구한다. API Stage의 Deployment ID만 돌려도 Lambda·환경·편집 설정은 별도로 남을 수 있다.

Relay 예약 대상을 중지/복원해도 진행 행은 유지한다. 동일 연결·지원 schema면 만료된 소유권 확인 후 저장 위치를 이어간다. 연결 지문 불일치·행 손상은 자동 초기화하지 않는다. 과거 위치 복원에 따른 중복 알림도 멱등 처리로 보호하며 Queue·Job·결과를 함께 삭제하지 않는다.

DB를 복원하면 원본을 덮어쓰지 않는 승인된 별도 대상으로 진행하고 참조 S3 객체·키·실행 버전·IAM·Stream·TTL·경보를 함께 확인한다. 코드 복구와 자료 복구의 성공을 구분한다. 실물 iOS/Android·마네킨 인수와 실제 ARC 계약/송신은 AWS 인수와도 별도다.
