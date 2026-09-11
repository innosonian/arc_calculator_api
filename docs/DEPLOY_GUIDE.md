# 로컬 검증 이후 AWS·Dev로 옮기는 순서

2026-09-11 소스·공식 문서 재확인 기준. BE 개발·설정·운용을 모두 직접 맡는 사용자를 위한 후속 안내다. 이 문서는 앞으로 할 일이며 **이번 작업에서 AWS 접속·자원 변경·배포·ARC 전송은 하지 않았다.** AWS 공식 문서는 아래 해당 설명에 연결했다. 실제 계정·자원은 아직 확인하지 않았다.

## 먼저 알아둘 현재 상태

로컬에서는 로그인 → 프로그램 선택 → 실측 업로드 → 자동 계산 → 결과·차트 조회까지 검증했다. [로컬 실행 안내](LOCAL_RUN.md)로 앱과 현장 시험부터 진행하면 된다.

AWS에서는 계정 정보를 넣는 것 외에 **실행 연결 코드가 더 필요하다.** 현재 `mock_journey/runtime.py`의 기본 구성은 제어 API이며, `worker_runtime.py`의 worker(계산 실행기)·relay(대기 작업 전달기)는 미구성 상태에서 거절한다. 로컬 CLI의 자식 프로세스 감독 구성·파일 서버는 배포 ZIP에 포함되지 않는다. 내부 계산기와 공용 Worker 코드는 포함된다. 기존 배포 스크립트만 실행하면 같은 Journey가 자동으로 완성되는 상태는 아니다.

| 구분 | 현재 완료 | 이후 필요 |
|---|---|---|
| 내부 계산 | 기존 수식·자료형·null·코칭 유지, 로컬 HTTP 검증 | 동일 코어를 AWS 실행에 연결·회귀 확인 |
| 로그인·진도·결과 | 공용 기능과 로컬 DB·파일 연결 | 실제 DynamoDB·S3·작업 실행 자원 연결 |
| 프로그램 완료 | Only 목표+Pass, CPR은 `pending_policy` | CPR 완료 규칙은 별도 확정 전 계속 대기 |
| 배포 도구 | ZIP 구성·오프라인 설정 검사·기존 Calculator/Gateway 갱신 도구 | 실자원 조사, 역할별 배선, 변경 계획·검증 |
| ARC | `submit_arc` 비활성 상태 표시 | 공식 계약 확보 후 별도 구현·실제 시험 |

현재 코드 근거는 [runtime](../mock_journey/runtime.py), [worker runtime](../mock_journey/worker_runtime.py), [공용 조립 함수](../mock_journey/assembly.py), [배포 ZIP 허용 목록](../scripts/build_mock_artifact.py)이다.

## 배포 경로 선택과 준비 완료의 뜻

**기존 API Gateway REST API + Python 3.12 Lambda + DynamoDB + 비공개 S3 경로를 유지한다.** 전체 훈련에는 기존 공용 코드에 맞는 SQS·Relay·Worker 연결이 추가로 필요하다. 새 상시 서버, Kubernetes, 컨테이너나 다른 계산 서비스는 도입하지 않는다. 현재 API event 형식·SDK·배포 ZIP·거래/파일 저장 코드를 재사용하여 이전 비용과 운영 부담을 줄이는 선택이다. 트래픽과 실제 자원 정보가 없으므로 월 비용이나 필요한 Lambda 개수는 확정하지 않았다.

| 준비 단계 | 2026-09-11 상태 |
|---|---|
| 로컬 기능 유지·회귀·배포 ZIP·설정 예시 | 검증 및 보완. 실행 결과는 [검증·리팩터링](VALIDATION.md#8-코드-품질과-배포-준비--2026-09-11) |
| 사용자 정보만 넣으면 AWS 전체 훈련이 동작하는 상태 | **아직 아님.** `runtime.py`의 계산 서비스 및 `worker_runtime.py` 연결, 역할별 로그 배출·접근 보호가 남음 |
| 실제 AWS 인증·연결·배포·실물 앱 인수 | 이번 작업에서 실행하지 않음 |

이 문서의 AWS 명령은 모두 **사용자가 후속 단계에서 실행할 명령**이다. 이번에 실행한 것은 별도 임시 DB 회귀, 로컬 ZIP 빌드·검사, 문법 검사뿐이다. 실제 CLI 대신 대역 명령을 쓰는 배포 회귀 테스트는 AWS 실행 증거가 아니다.

**사용자 확인: 운영값·배치는 아직 미정이며 이번에는 기존 구성 기반 제안과 안전한 준비까지만 진행한다.** 운영값·접근 보호·배치를 임의로 정하면 기존 보존 경계를 바꿀 수 있어 해당 연결 변경은 분리했다. 필요한 맥락은 기존 Lambda/DB/S3/SQS 구성표, 팀원 개인 접근 방식, 최대 요청·결과 크기, 실행·lease·재시도 제한, retention이다. 자격 증명·실제 키는 공유하지 않고 비밀을 제외한 설정 위치만 확인한다. CPR 완료 규칙과 ARC 계약은 AWS 준비와 별개로 계속 미정 상태를 보존한다.

### 준비물과 비용 확인

| 어디에서 → 할 일 | 성공 기준·비용 |
|---|---|
| Mac 터미널 → `python3.12 --version`, `java -version`, `javac -version` 확인 | 로컬 Python 3.12·검증된 DynamoDB Local을 실행할 Java 도구가 있음. 로컬 검사는 AWS 사용료 없음 |
| 사용자 터미널 → `aws --version` 확인 | 회사에서 허용한 AWS CLI v2 설치. 자격 증명은 `.env`에 넣지 않음 |
| 회사 관리자 → 개인 SSO/Role, 읽기 및 계획된 변경 권한 확인 | 공유 비밀번호나 관리자 전체 권한 없이 승인 자원에 접근 가능 |
| AWS Pricing Calculator → 선택 리전의 Lambda/API Gateway/DynamoDB/S3/SQS/CloudWatch 입력 | 요청 수·계산 시간·파일 및 로그 보관량에 근거한 예상 비용 확인 |
| Billing and Cost Management → Budgets → Create budget | 본인이 승인한 예산·수신 주소 입력 후 알림 설정 확인. 자동 비용 차단으로 해석하지 않음 |

**비용 발생:** AWS 자원 생성·사용·로그/원본 보관·데이터 전송부터 과금 가능하다. 본 저장소 스크립트는 새 인프라 전체를 만들지 않으며, 기존 설정 변경도 가동 중 자원의 비용·보관량에 영향을 줄 수 있다. 정리하지 않은 보관 데이터는 계속 비용이 생길 수 있다. [AWS 비용 계산기](https://calculator.aws/), [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html)

## 1. 기존 BE팀에서 내 개인 접근 권한과 자원 목록을 받기

공용 IAM username(사용자 이름)·비밀번호를 넘겨받는 방식보다, 기존 회사의 SSO(통합 로그인)나 임시 Role(역할 권한)을 **내 개인 계정에** 부여받는다. 기존 체계를 먼저 확인하고 새 계정이나 관리자를 임의로 만들지 않는다. 임시 자격 증명·MFA(다중 인증)·최소 권한 사용은 AWS의 보안 권고다. [AWS IAM 보안 권고](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html)

기존 BE팀에 다음처럼 요청할 수 있다. 직접 발송하는 문구는 아니며 필요에 따라 전달한다.

> ARC Calculator API의 로컬 검증을 마쳤고, 다음 단계로 기존 AWS의 Dev 자원에 연결하려고 합니다. 제가 사용할 개인 SSO 또는 임시 Role 접근 방법과, 변경 가능한 계정 ID·리전·기존 API Gateway·Calculator Lambda·관련 저장소/DB/큐의 이름을 부탁드립니다. 실제 운영 자원과 공유하는 항목, 변경하면 안 되는 자원도 함께 알려주세요. 공용 비밀번호나 장기 Access Key는 보내지 않으셔도 됩니다.

| 받을 정보 | 쉬운 뜻 |
|---|---|
| Account ID(계정 번호) | 어느 AWS 계정에서 작업하는지 |
| SSO 시작 주소·SSO Region(로그인 리전)·내 Role | 내 계정으로 로그인하는 방법과 허용 권한 |
| Resource Region(자원 리전) | 실제 서버·DB·파일이 위치한 지역. SSO 리전과 같다고 가정하지 않음 |
| API Gateway ID·유형·Stage(배포 구분) | 앱 요청을 받는 기존 입구와 Dev 연결 위치 |
| Lambda 이름·Handler(시작 함수)·실행 Role | 기존 Calculator 및 제출 함수 유무·실행 권한 |
| DB·Bucket(파일 저장소)·Queue(작업 대기열) | 재사용 가능한 실제 이름, 없으면 없는 상태를 확인 |
| 기존 보호·로그·보관·비용 설정 | 개인 팀원 접근·데이터 삭제·장애 알림·비용을 기존 기준과 맞추기 위한 자료 |

## 2. 로그인해서 Dev 계정이 맞는지만 확인하기

회사가 IAM Identity Center를 사용하는 경우, AWS CLI(명령 도구)를 준비한 뒤 아래 순서로 실행한다. `arc-dev`는 이 Mac에서 정하는 설정 이름의 예시다. 계정 번호·리전·역할은 받은 실제 값을 선택한다.

```sh
aws configure sso --profile arc-dev
aws sso login --profile arc-dev
aws sts get-caller-identity --profile arc-dev
```

마지막 명령의 Account와 Arn이 승인받은 Dev 계정·역할인지 대조한다. 위 명령은 배포 명령이 아니다. 회사가 다른 로그인 체계를 쓴다면 해당 방식으로 임시 권한을 받아 같은 계정 확인을 한다. [AWS CLI의 SSO 설정·로그인](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)

## 3. 바꾸기 전에 현재 자원을 읽어서 기록하기

Console(관리 화면)에서 기존 API Gateway와 Calculator Lambda를 먼저 찾는다. 새 서버·새 IAM Role을 만드는 작업부터 시작하지 않는다. 다음을 비밀값 없이 기록한다.

- 현재 함수 버전·Handler·실행 Role·메모리·시간 제한, API Stage가 어느 함수를 부르는지.
- `/cpr-analysis` 외에 로그인·목록·시도·결과 경로가 실제로 있는지.
- API 인증 방식, 팀원별 접근 허용·회수 방법, 바이너리 처리, 로그·캐시 설정.
- DB의 테이블·인덱스, 파일 저장 경로, 작업 큐·트리거, 현재 보관/삭제 정책.
- Dev와 운영이 공유하는 API·Role·Bucket·배포 경로 및 원복할 현재 설정.

현재 workflow(자동 배포 설정)는 `develop`/`main` push와 수동 실행으로 Dev/Prod를 선택한다. **설정을 이해하기 전에 해당 브랜치로 push하지 않는다.** Beta 자동 배포 경로는 준비되어 있지 않다. 코드에 이름이 보인다는 것이 실제 AWS에 그 자원이 있다는 증거는 아니다. [현재 workflow](../.github/workflows/deploy_arc_lambdas.yml)

현재 workflow 자체에는 회귀 시험·GitHub Environment(배포 승인 구분)의 승인 단계가 선언되어 있지 않고, 수동 Dev 실행은 여러 브랜치에서 가능하다. 실제 저장소 보호 설정을 확인하고, 승인된 코드만 배포하도록 보완한 뒤 Secret을 연결한다. OIDC(단기 권한 교환)를 우선 검토한다. 기존 장기 키 fallback(대체 인증)이나 `docs/iam`의 과거 계정·넓은 권한 예시를 그대로 적용하지 않는다. 배포 권한과 함수의 실행 권한은 같은 것이 아니다.

## 4. AWS에서 실행할 연결 코드 완성하기

이 단계는 다음 개발 작업이다. 사용자는 확인한 자원 정보와 운용 기준을 제공하고, 아래 연결 변경을 검토·승인한다. 수식이나 앱 요청 형식을 다시 구현할 필요는 없다.

| 재사용할 역할 | AWS에서 연결할 내용 |
|---|---|
| HTTP API(요청 수신) | 기존 API Gateway·Calculator Lambda의 인증 입구에 모든 앱 경로와 계산 접수 서비스를 연결 |
| DynamoDB(상태 저장) | 세션·attempt·공유 진도·작업·Outbox(전달할 작업 기록)를 공용 저장 코드에 연결 |
| S3(파일 저장) | 기존 원본·결과·차트 규격과 접근 보호를 실제 승인 Bucket/경로에 연결 |
| Relay(전달기) | Outbox와 재처리할 작업을 읽어 Queue에 작업 ID를 전달 |
| Worker(계산 실행기) | 같은 내부 계산기를 호출하고 결과·별도 평가·진도를 영속 저장 |
| Submit Lambda(ARC 제출 함수) | 공식 계약 확보 후 Calculator 역할에서 동기 호출. 지금은 비활성 유지 |

현재 공용 코드의 Queue 메시지는 `job_id`만 전달한다. 원본 파일·세션 토큰을 Queue 메시지에 넣지 않는다. 로컬의 Python 자식 프로세스나 Java DB 실행기를 Lambda에 복사하지 않는다. `mock_journey/execution_definitions.py`로 분리한 15개 실행 정의와 완료 대기 정책을 공용 구성에서 재사용한다. 버전 문자열과 기존 점수 계산은 바꾸지 않는다.

개발 연결 지점은 `assembly.py`의 `build_application`·`build_worker`·`build_relay`, `mock_journey.worker.run`과 `mock_journey.dispatch.run`이다. 실행 정의·계산 adapter·입력 projection(계산 요청의 허용 필드 규격)의 버전을 명시하고, 이미 접수된 작업의 고정 버전을 새 설정으로 덮지 않는다. 로컬 전용 lease 갱신기는 ZIP에 없으므로 AWS 실행 시간에 맞는 갱신·소유권 검사도 연결해야 한다. 실제 Lambda를 몇 개로 배치할지는 확인한 기존 자원과 역할 구성을 바탕으로 정한다.

DB에는 기존 PK/SK 구조와 due 작업 조회용 GSI1(보조 조회 인덱스)이 필요하다. 실제 기존 테이블이 맞는지 확인하고 계획한 변경만 적용한다. 데이터 삭제·로그인 키 재생성을 인덱스 추가의 해결책으로 삼지 않는다.

SQS(작업 대기열)는 같은 메시지가 여러 번 전달될 수 있다. 따라서 기존 attempt 입력 중복 검사·작업 소유권·최종 저장·로그아웃 epoch 검사를 유지한다. Stream(변경 알림)만 연결하고 끝내지 않고, 누락·지연된 작업을 다시 찾는 주기 실행도 연결한다. [AWS Lambda와 SQS의 중복 처리 설명](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html)

Visibility timeout(같은 메시지를 다른 실행기에서 잠시 숨기는 시간), Lambda 실행 제한, DB lease(작업 소유권 시간)를 함께 맞춘다. AWS는 SQS visibility를 함수 timeout의 최소 6배로 설정하도록 안내하며 batch window도 고려한다. 현재 로컬의 60초 lease를 AWS 기본값으로 그대로 승인하지 않는다. 실패 메시지별 재처리와 DLQ(반복 실패 메시지 보관 큐), 재전달 시험도 필요하다. [SQS 트리거 설정 지침](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)

현재 Worker와 Relay가 반환하는 `batchItemFailures`를 실제 트리거의 부분 실패 처리 설정과 맞춘다. DLQ에 메시지가 들어가도 공용 복구 코드는 아직 끝나지 않은 DB 작업을 다시 찾아 전달할 수 있다. 따라서 DLQ 도착을 앱 작업의 최종 취소나 ARC 제출의 정확히 한 번 실행 보장으로 설명하지 않는다.

## 5. 외부 접속 보호와 환경 분리 적용하기

Dummy ID/비밀번호는 모두가 아는 시험용 값이다. **팀원 개인의 외부 접근 권한과 별도로 유지**한다. 승인된 회사 접근 체계로 로그인 경로부터 보호하고, 개인 권한을 회수했을 때 새 API 호출·로그인이 차단되는지 확인한다. 이미 발급된 차트 서명 URL은 팀 권한·세션 회수와 독립하여 자체 유효기간 동안 읽힐 수 있다. 이때 앱의 `Authorization: Bearer <session_token>`이 서버까지 그대로 전달되어야 한다.

| 적용 항목 | 확인 기준 |
|---|---|
| HTTPS(암호화 통신) | 앱 요청·차트 다운로드에 적용. 로컬의 평문 허용 옵션을 Dev에 이식하지 않음 |
| 최소 권한 | 실행 역할별로 필요한 테이블·인덱스·파일 경로·큐·함수만 허용. `AdministratorAccess`로 오류를 우회하지 않음 |
| 환경 격리 | Dev·Beta·Prod의 세션 키·DB 상태·Queue·저장 경로가 섞이지 않음. 실제 자원 분리 방식은 기존 구성과 함께 검토 |
| 비밀값 | Git·ZIP·명령 인자·로그에 넣지 않음. 환경별 Secret 저장·조회·교체 경로 구성 |
| 로그 | 요청 ID·오류 코드·처리 시간 중심. 비밀번호·Bearer·복구 증표·원본·차트 서명 URL·전체 body 기록 금지. Gateway 요청·응답 본문 추적도 비활성 확인 |
| 요청 제한 | 로그인과 계산·조회 각각의 남용 제한 및 동시 계산·비용 한도를 실제 부하로 검증 |

현재 일부 runtime은 키 값을 환경변수에서 읽는다. Secret 이름만 적으면 자동 조회되는 기능은 없으므로, Secrets Manager(비밀 저장 서비스)를 선택한다면 조회 권한과 연결 코드도 추가해야 한다. AWS는 민감한 정보에 Secrets Manager 사용을 권고한다. 로컬 `var/local-server`의 키·DB를 AWS로 복사하지 않는다. [Lambda 환경변수와 비밀값 안내](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html)

차트는 비공개 S3 객체에 대한 300초 Presigned URL(기한이 있는 서명 링크)로 기존 규격을 연결할 수 있다. URL을 가진 사람이 읽을 수 있으므로 원본·메타데이터에는 발급하지 않는다. 임시 AWS 자격 증명이 먼저 만료되면 링크도 300초보다 일찍 만료될 수 있어, 앱의 링크 갱신 API와 함께 시험한다. 앱 로그아웃과 AWS 자격 증명 만료는 서로 다른 사건이다. [S3 서명 링크의 권한·만료](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)

## 6. 파일 크기와 대기를 실제 요청으로 검증하기

앱이 훈련 종료 후 보낸 전체 누적 파일, multipart 포장, base64 증가량, Lambda event 포장, 계산 JSON까지 측정한다. 로컬 기본 HTTP 본문 1,000,000 bytes·객체 8,000,000 bytes는 AWS 전송 가능 크기를 보증하지 않는다.

기존 REST API의 일반 buffered(전체 응답을 모아서 반환) 경로를 기준으로 API Gateway payload 한도는 10 MB, Lambda 동기 요청·응답은 각각 6 MB다. 둘 중 하나만 보고 허용량을 정하지 않는다. 특히 파일이 작아도 결과 JSON이나 포장된 event가 한도를 넘을 수 있다. 스트리밍이나 업로드 계약 변경을 이번 안내에서 임의로 도입하지 않는다. [REST API 한도](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-execution-service-limits-table.html), [Lambda 한도](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)

API Gateway의 `binaryMediaTypes`와 실제 `isBase64Encoded` 전달을 확인하고, 전송 전후 원본 bytes의 hash(내용 지문)를 비교한다. JSON 로그인·목록 응답도 함께 재검증한다. `*/*`를 무조건 추가하면 공유 API의 다른 응답에 영향을 줄 수 있다. [API Gateway 바이너리 처리](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-payload-encodings.html)

앱의 최대 30초는 계산+ARC 제출을 포함한 사용자 대기다. Gateway 요청 timeout이나 작업 취소 시간과 같은 값이라고 가정하지 않는다. 현재 REST API timeout 범위와 조정 가능 여부는 유형마다 다르므로 실제 API 유형·할당값을 확인한다. 접수 `202` 뒤 같은 attempt를 조회하는 계약을 유지한다. Q23의 정확한 대기 기산점은 아직 미정이다. [REST API timeout 한도](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-execution-service-limits-table.html)

## 7. 설정 파일을 만들고 배포 전 검사하기

실제 확인한 값만 Binding(환경과 자원의 연결표)에 작성한다. `local`, `dev/development`, `beta`, `prod/production` 선택은 지원하지만, 환경 이름만으로 계정·리전·자원 이름을 만들어 주지 않는다. `local`의 AWS 배포는 거절한다.

기존 Binding은 Calculator/Gateway용이며 전체 Journey 자원 연결표를 대신하지 않는다. Worker·Relay·Queue·DB·Secret 연결에 필요한 배포 설정을 다음 개발에서 보완한다. 저장 Bucket/경로가 보존된 업로더 상수와 다르면 현재 사전 검사는 중단한다. 오류를 없애려고 HSTM 또는 운영 경로를 무작정 지정하지 않는다. 실제 승인된 저장 대상과 코드의 바인딩을 먼저 맞춘다.

먼저 **저장소 루트**에서 아래 명령으로 예시를 비공개 작업 사본으로 복사한다. 이미 사본이 있으면 `cp -n`은 덮어쓰지 않는다. 이 단계는 AWS에 연결하지 않는다.

```sh
cd /Users/mac/arc_calculator_api
umask 077
mkdir -p var/deployment
cp -n .env.example .env.dev
cp -n deploy/bindings.example.json var/deployment/bindings.json
chmod 600 .env.dev var/deployment/bindings.json
```

편집기에서 두 파일의 `<...>`를 아래 표와 부록 A에 따라 바꾼다. `memory_mb`, `timeout_seconds`의 문자열 자리표시자는 **따옴표 없는 정수**로 바꾼다. 예를 들어 문법상 `512`, `120`은 가능하지만 이 숫자가 승인된 운영값은 아니다. `null`은 기존 설정 유지이며 자동 삭제를 끄는 명령이 아니다. 미확정 예시 그대로는 preflight가 실패하는 것이 정상이다.

| 환경변수 | 의미 → 얻는 위치 → 입력 위치 |
|---|---|
| `STAGE` | 기존 파일 경로의 환경 문자열 → 기존 Lambda 설정 확인 → `.env.dev`, binding의 `runtime_stage`에 동일하게 입력 |
| `ARC_STORAGE_REGION` | 저장소 리전 → S3 속성 → `.env.dev`, binding `storage_region` |
| `ARC_MOCK_ENABLED` | 현재 AWS 제어 runtime 활성화 → 기본 예시는 `false`; 전체 연결 인수 후에만 `true`로 결정 |
| `ARC_MOCK_ENVIRONMENT` | 세션 서명 namespace → 기존 환경 분리 정의 → `.env.dev`; 기존 값을 임의 변경하지 않음 |
| `ARC_MOCK_TABLE_NAME`, `ARC_MOCK_REGION` | 상태 테이블·리전 → DynamoDB 테이블 정보 → `.env.dev` |
| `ARC_MOCK_RESUME_KEYS` | 복구 서명 keyring JSON → 승인된 비밀 관리 절차 → 비공개 환경 파일/주입 경로. 버전별 base64 키이며 decode 후 최소 32 bytes |
| `ARC_MOCK_RESUME_KEY_VERSION` | 현재 발급할 키 버전 → 위 keyring의 기존 key → `.env.dev` |
| `SENTRY_DSN` | 선택적 오류 수집 주소 → 승인된 Sentry 설정. 빈 값은 비활성. 전체 요청 수집을 켜지 않음 |

관리용 `AWS_PROFILE`은 터미널에만 지정하며 Lambda `.env`에 AWS access key·session token을 넣지 않는다. `ARC_DEPLOYMENT_BINDINGS`는 배포 shell의 선택적 파일 경로이고 런타임 환경변수가 아니다. `.env`는 자동으로 로컬 서버에 로드되지 않는다. `serve_local.py`는 기존 격리 설정과 CLI 옵션을 사용한다.

현재 runtime은 Secret ARN을 해석하지 않는다. Secrets Manager 참조를 자동 조회한다고 가정하지 말고, 향후 연결 단계에서 조회 권한과 코드까지 검증한다. 기존 환경변수 전체를 보존해 주입할 때만 파일 입력을 사용하며 `source`, `eval`, `cat .env.dev`, 명령줄에 키 값을 쓰는 방법은 사용하지 않는다. [Lambda 비밀 설정·전체 환경 교체 동작](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html)

실제 파일이 준비된 뒤 **저장소 루트에서** 외부 접속 없이 수행한다. 아래 출력의 `syntax_and_composition_valid`가 형식 검사 성공이다.

```text
python3.12 scripts/deployment_preflight.py --environment dev --component calculator --bindings var/deployment/bindings.json --env-file .env.dev
python3.12 scripts/deployment_preflight.py --environment dev --component gateway --bindings var/deployment/bindings.json --env-file .env.dev
```

성공 결과 `syntax_and_composition_valid`는 형식 검사를 통과했다는 뜻이다. `aws_resources_verified=false`, `runtime_verified=false`이므로 실제 배포 가능 판정은 아니다. 환경파일을 `source`/`eval`하지 않는다. 상세 형식은 아래 부록의 환경 연결표·환경파일 계약을 따른다.

### 로컬 빌드와 정상 동작 확인

**저장소 루트, 사용자 터미널.** 현재 Mac의 로컬 Journey 시작·상태 확인은 [LOCAL_RUN.md](LOCAL_RUN.md)를 따른다. `GET /healthz`의 200은 로컬 DB·Worker 감독 상태이고 AWS에는 이 health 경로가 구현돼 있지 않다. 전체 재검증은 사용자의 서버를 공유하지 않는 다음 명령이다.

```sh
var/local-python/bin/python scripts/validate_local_integration.py --dynamodb-home var/dynamodb-local-3.3.1 --suite all
```

pytest가 없는 환경이면 먼저 별도 개발 가상환경에 `requirements-local.txt`와 `requirements-dev.txt`를 설치한다. 사용자 DB 폴더를 시험 입력으로 지정하지 않는다. 성공은 pytest 종료 코드 0이며 예상 실패는 별도로 보고한다.

**다음은 사용자가 깨끗한 빌드 환경에서 실행할 명령이다.** pip 단계는 Python 패키지 저장소 다운로드가 필요하지만 AWS 인증·연결은 하지 않는다. ZIP 경로는 엄격한 검사 때문에 `/tmp` 대신 실제 경로인 `/private/tmp`를 사용한다. 매 실행 새 작업 폴더를 만들며 기존 산출물을 덮지 않는다.

```sh
cd /Users/mac/arc_calculator_api
umask 077
ARC_BUILD_DIR="$(mktemp -d /private/tmp/arc-lambda-build.XXXXXX)"
python3.12 -m venv "$ARC_BUILD_DIR/python"
"$ARC_BUILD_DIR/python/bin/python" -m pip install \
  -r requirements.txt -c constraints-lambda.txt \
  --target "$ARC_BUILD_DIR/packages" --no-compile --only-binary=:all: \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12
"$ARC_BUILD_DIR/python/bin/python" scripts/build_mock_artifact.py \
  --source-root "$PWD" --packages-dir "$ARC_BUILD_DIR/packages" --outdir "$ARC_BUILD_DIR/artifact"
"$ARC_BUILD_DIR/python/bin/python" -m zipfile -t "$ARC_BUILD_DIR/artifact/mock-lambda.zip"
```

성공하면 마지막 검사에 `Done testing`이 나오고 `artifact/`에 `mock-lambda.zip`, `artifact-manifest.json`이 생긴다. manifest는 포함 파일 hash·의존성 버전과 **검증하지 않은 범위**를 기록한다. 동일 Python/zlib와 동일 입력으로 반복 생성한 ZIP은 동일하다. 서로 다른 압축 라이브러리 버전까지 ZIP hash 일치를 보장하지 않으며 파일별 hash로 내용도 대조한다.

압축 ZIP 50MiB·압축 해제 내용 250MiB를 초과하면 게시 전에 실패한다. 추가 Lambda layer가 있으면 원격 합산 크기도 별도로 확인한다. 설치에 사용하는 간접 버전은 `constraints-lambda.txt`로 고정했지만 wheel 출처·hash 기반 공급망 검증이나 Linux Lambda 실행 인수를 대체하지 않는다. [Lambda 패키지 한도](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html), [Python ZIP 패키징](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html)

## 8. 검토한 변경만 Dev에 배포하기

앞 단계의 연결 코드·권한·대상 자원과 원복 계획을 완성한 후 진행한다. **현재는 이 조건을 충족하지 않았다. 아래 배포 명령은 나중에 사용자가 실행할 형식이며 지금 실행하지 않는다.**

1. Linux/Python 3.12 대상 Artifact(배포 ZIP)를 만들고 의존성·Handler import·회귀를 검사한다. Mac 가상환경·DynamoDB Local·테스트 데이터·비밀파일을 포함하지 않는다.
2. 실제 대상 계정·기존 함수·Role·API Stage를 다시 대조한다. 배포 전에 현재 함수·설정·API Deployment ID를 기록한다.
3. 승인한 DB·파일·Queue·Secret 및 Worker/Relay 연결을 준비하고, 팀 접근을 제한한 Dev에서 실행을 검증한다.
4. 기존 Calculator를 공용 인증 Handler로 갱신하고 `/cpr-analysis`와 로그인·목록·시도·조회 경로를 연결한다. `_run_trusted_calculation`을 공개 Handler로 쓰지 않는다.
5. API 배포에 공유 API의 다른 대기 변경이 포함되는지 확인한 뒤 Dev Stage를 갱신한다.
6. 아래 인수를 통과한 실제 주소·서버 버전을 앱팀에 별도로 전달한다.

현재 [Calculator 스크립트](../scripts/deploy_arc_lambda.sh)와 [Gateway 스크립트](../scripts/deploy_arc_api_gateway.sh)는 기존 자원을 갱신하는 일부 도구다. 전체 Journey의 DB/Queue/트리거/개인 접근 보호를 한 번에 구성하는 도구가 아니다. 함수 코드·설정 갱신의 중간 실패도 있을 수 있으므로 오류 후 이전 상태를 자동 복구했다고 가정하지 않는다.

특히 현재 Gateway 도구는 `/cpr-analysis`의 POST만 연결하며 나머지 앱 경로·인증의 충분성·Lambda 호출 권한을 완성하지 않는다. Lambda 도구는 **환경변수 전체 목록을 교체**하므로 필요한 기존 변수를 누락하면 사라진다. IAM Role에 `service-role/` 같은 경로가 있으면 binding의 `role_name`에는 마지막 이름, `role_arn`에는 확인한 전체 ARN(자원 식별자)을 지정한다. 생략 시에는 이전처럼 경로 없는 역할만 허용한다. 계정·이름·실제 ARN 일치 검사는 유지하며 새 역할을 만들어 우회하지 않는다. 현재 Gateway 도구가 가리키는 함수는 특정 버전·별칭으로 고정되지 않아 코드 갱신이 버전 발행 전부터 요청에 영향을 줄 수 있다. 현재 도구에는 별칭 전환 기반의 자동 원복이 없으므로 함수·환경변수·API 연결의 원복 절차를 직접 준비한다.

### 조건을 충족한 뒤의 사용자 실행 명령

**AWS 변경·비용 영향이 있는 단계. 저장소 루트에서만 실행한다.** 본 작업에서 실행하지 않았다. 기존 함수·역할·로그 그룹·REST method·stage와 호출 권한이 먼저 있어야 하며 스크립트가 생성하지 않는다. `python3`가 Python 3.12인 전용 가상환경을 PATH 앞에 두고 `aws` CLI v2가 사용 가능한지 확인한다.

```sh
cd /Users/mac/arc_calculator_api
export AWS_PROFILE=<APPROVED_CLI_PROFILE>
export AWS_PAGER=""
aws sts get-caller-identity --query Account --output text
bash scripts/deploy_arc_lambda.sh development .env.dev var/deployment/bindings.json
bash scripts/deploy_arc_api_gateway.sh development .env.dev var/deployment/bindings.json
```

`<APPROVED_CLI_PROFILE>`은 앞서 설정한 개인 프로필 이름(예: `arc-dev`)으로 바꾼다. 표시 계정이 승인 계정과 다르면 다음 명령을 실행하지 않는다. 두 스크립트의 `…DEPLOYMENT_FINISHED`는 각각의 일부 갱신 완료이며 전체 훈련 인수 성공이 아니다. Lambda 스크립트는 새 버전을 발행하지만 Gateway는 현재 별칭 없는 함수에 연결하므로, 갱신 도중부터 요청에 영향을 줄 수 있다.

**로그 데이터 삭제 영향:** `log_retention_days`에 숫자를 명시하면 그 기간을 넘긴 CloudWatch 로그가 삭제 대상이 될 수 있다. 미정이면 `null`로 기존 정책을 유지하고, 기존 정책도 사용자 요구와 맞는지 따로 확인한다. `null`을 무기한 보관으로 오해하지 않는다. [CloudWatch 보관 정책](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutRetentionPolicy.html)

## 9. Dev에서 직접 인수하고, 그다음 Beta·Prod로 이동하기

| 시험 | 통과 기준 |
|---|---|
| 로그인·권한 | 승인 팀원만 접속, 회수된 팀원 차단, 세션 없음/다른 시도 접근 차단 |
| 15개 프로그램·연령 | 앱 실측으로 생성→업로드→계산 결과→차트 확인; 값·자료형·null·코칭 보존 |
| 별도 완료 | Only 목표+Pass, CPR `pending_policy`. `progress_application`·epoch를 구별해 현재 공유 진도 반영 확인 |
| 재시도 | 같은 ID·입력은 같은 결과, 다른 입력409, 중복 Queue 처리로 진도 이중 반영 없음 |
| 지연·장애 | 30초 이후 결과 조회, Worker 중단·재실행, Queue 재전달·반복 실패 추적 |
| 로그아웃 | 공유 진도 초기화, 이전 시도의 늦은 결과는 보관하되 새 진도에 미반영 |
| 차트·파일 | 실다운로드·만료·새 링크, 원본·키 비공개, 저장 실패에도 확정 결과 보존 |
| 비용·운용 | 실제 메모리·소요 시간·동시성·실패/대기 알림 확인, 보관·삭제 기준 기록 |

Beta와 Prod는 각각 다른 실제 연결표·권한·데이터 격리와 같은 시험 증거가 필요하다. Dev 설정을 이름만 바꿔 복사하지 않는다. 현재 workflow에는 Beta가 없으므로 먼저 별도 배포 경로와 검증 절차를 추가한다. 같은 검증된 코드 산출물을 사용하되 환경값은 명시적으로 구별한다.

Beta·Prod라는 환경명도 Dummy 로그인을 실제 사용자 인증으로 바꾸지는 않는다. ARC 연동 전에는 승인된 시험팀의 접근 범위를 유지하며 고객 공개 운영이 승인됐다고 간주하지 않는다.

문제가 생기면 새 접수·실행을 제한하고 진단 자료를 보존한다. 코드·설정·API 연결을 기록한 이전 버전으로 되돌릴 때, DB/작업/파일 형식을 이전 코드도 읽을 수 있는지 먼저 확인한다. 원복을 위해 DB를 삭제하거나 모든 작업을 새 ID로 재제출하지 않는다.

### 배포 후 접속·상태·로그 확인

**사용자가 AWS 연결 후 실행한다.** 모든 `<...>`는 자원 연결표에서 확인한 값으로 바꾼다. 실행 폴더는 저장소 루트이며 선택 계정은 `AWS_PROFILE`로 유지한다.

```sh
aws lambda get-function-configuration --function-name <EXISTING_LAMBDA_NAME> --region <AWS_REGION> \
  --query '{State:State,LastUpdateStatus:LastUpdateStatus,Handler:Handler,Runtime:Runtime,CodeSha256:CodeSha256}' --output json
aws apigateway get-stage --rest-api-id <EXISTING_REST_API_ID> --stage-name <EXISTING_API_STAGE> \
  --region <AWS_REGION> --query '{Stage:stageName,Deployment:deploymentId}' --output json
```

Lambda `State=Active`, `LastUpdateStatus=Successful`, `Handler=lambda_handler.run`, `Runtime=python3.12`와 기록한 배포 ID를 대조한다. 이 상태는 실제 훈련 성공을 보장하지 않는다. API Gateway Console → 해당 REST API → Stages → 승인 Stage의 **Invoke URL**을 확인한다. 사용자 도메인은 미정이므로 임의 도메인이나 인증서를 생성하지 않는다.

해당 URL과 [앱 API](APP_API.md)를 이용해 위 인수 표의 정상·거절 경로를 확인한다. 로그인 응답·차트 링크·토큰은 캡처나 공유 로그에 넣지 않는다. AWS `/healthz` 호출이나 단순 함수 상태만으로 15개 계산 경로 검증을 대신하지 않는다.

CloudWatch Console → Log groups → 해당 함수의 실제 로그 그룹 → 최신 stream에서 request ID·정제된 오류 코드·처리 시간을 확인한다. 원문 요청 추적은 끈다. 함수가 사용자 지정 로그 그룹을 사용한다면 `/aws/lambda/<이름>`을 자동으로 맞는 것으로 취급하지 않는다. DynamoDB의 OPS 로그는 AWS runtime에 아직 연결되지 않았으므로 CloudWatch 조회만으로 DB 로그 완성을 주장하지 않는다. [Lambda 상태 조회](https://docs.aws.amazon.com/cli/latest/reference/lambda/get-function-configuration.html)

### 실패 확인 순서와 롤백

롤백은 이전 코드·설정으로 돌아가는 작업이다. **AWS를 변경하므로 후속 단계에서 사용자가 실행한다.** 갱신 전에 이전 ZIP과 manifest, 전체 환경변수의 보호된 사본, 함수 메모리·timeout·runtime·handler, 로그/동시성 설정, API deployment ID·integration·binaryMediaTypes·method throttle을 기록한다. 기존 ZIP이 없으면 Lambda Console의 해당 버전 Code 탭에서 다운로드해 비공개 폴더에 보관한다. 서명된 다운로드 URL을 문서나 로그에 복사하지 않는다.

| 증상 | 어디에서 → 확인할 일 → 복구 판단 |
|---|---|
| preflight 실패 | 터미널 오류 코드 → `<...>`, 정수 타입, stage/region/저장 상수, 전체 환경 크기 대조 | AWS 호출 전 실패. 키를 출력하지 말고 설정만 교정 |
| `AWS_ACCOUNT_MISMATCH`·역할 불일치 | 개인 프로필과 승인 binding 대조 | 다른 자원을 새로 만들어 우회하지 않음 |
| `503`·훈련 생성 실패 | runtime 연결·키 버전·테이블·실행 정의 확인 | 현재 기본 AWS runtime의 미연결과 실제 서비스 장애 구분 |
| 계속 `202` | SQS/Relay/Worker 트리거·due Job·lease 상태 확인 | 입력·job ID를 바꾸어 재제출하지 않음 |
| `403`·차트 실패 | 개인 접근·S3 namespace·서명 만료·권한 확인 | 소유권 확인 없이 공개 bucket으로 바꾸지 않음 |
| 코드 갱신 후 설정 실패 | 함수 현재 코드 hash·LastUpdateStatus와 이전 설정 비교 | 자동 원복된 것으로 가정하지 말고 아래 부분 복구 수행 |

새 접수를 제한하고 진행 중 작업을 확인한 뒤, 이전 ZIP과 저장된 작업/파일 버전이 호환되는 경우에만 되돌린다. **데이터 삭제는 하지 않는다.** 아래는 코드와 API Stage의 복구 형식이며 전체 자동 롤백 스크립트가 아니다.

```sh
aws lambda update-function-code --function-name <EXISTING_LAMBDA_NAME> --region <AWS_REGION> \
  --zip-file "fileb://<PREVIOUS_ZIP_ABSOLUTE_PATH>" --query CodeSha256 --output text
aws lambda wait function-updated --function-name <EXISTING_LAMBDA_NAME> --region <AWS_REGION>
aws apigateway update-stage --rest-api-id <EXISTING_REST_API_ID> --stage-name <EXISTING_API_STAGE> \
  --region <AWS_REGION> --patch-operations 'op=replace,path=/deploymentId,value=<PREVIOUS_DEPLOYMENT_ID>' \
  --query deploymentId --output text
```

코드 hash·deployment ID가 기록과 일치하면 해당 단계 성공이다. 함수 환경변수·메모리·시간·로그 보관·동시성은 별도 복구한다. Lambda Console → Configuration에서 이전 보호된 설정 사본과 대조하고 복원한다. 환경변수는 부분 목록으로 갱신하지 않는다. API Stage를 되돌려도 현재 REST API 편집 상태의 integration/binaryMediaTypes나 throttle이 모두 복원되는 것은 아니므로 저장한 값과 대조한다. DB/비공개 파일은 유지하고 결과 재조회·인증·공유 진도까지 다시 인수한다. [코드 복구 명령](https://docs.aws.amazon.com/cli/latest/reference/lambda/update-function-code.html), [갱신 대기](https://docs.aws.amazon.com/cli/latest/reference/lambda/wait/function-updated.html), [Stage 변경](https://docs.aws.amazon.com/cli/latest/reference/apigateway/update-stage.html)

### 사용자의 후속 체크리스트

- [ ] 개인 SSO/Role과 승인 계정·리전·기존 자원 목록을 확인한다.
- [ ] Dev/Beta/Prod 격리·개인 접근 보호·입력/결과 한도·실행/lease/재시도·보관 정책을 결정한다.
- [ ] 그 결정에 맞춰 API 계산 접수, Worker, Relay, S3 binding, 비밀 조회와 로그 배출 연결 코드를 완성하고 격리 환경에서 검증한다.
- [ ] `.env.dev`와 `var/deployment/bindings.json`을 채워 오프라인 검사·빌드를 통과시킨다.
- [ ] 기존 GitHub push 배포의 보호 조건을 확인한다. 이번에는 자동화·Secret 연결을 활성화하지 않았다.
- [ ] 비용 예상·알림, 이전 ZIP·전체 설정·API 연결 및 복구 절차를 확보한다.
- [ ] AWS 로그인·계정 대조 후 승인한 기존 자원에만 갱신하고 실제 앱 Journey를 인수한다.
- [ ] 시험 종료 시 접근 회수·새 접수 중단·남은 Job 확인·보관 대상을 확정한다. **삭제는 비용·복구 영향과 대상 자원을 확인한 뒤 별도 승인된 범위에서만** 한다.

이번 변경은 AWS 자원을 만들지 않았으므로 정리할 신규 원격 자원이 없다. 전체 계정 정리·bucket 비우기·table 삭제 명령은 제공하지 않는다. 나중에 생성한 전용 시험 자원이 있다면 AWS Console에서 정확한 이름·환경·연결 관계와 백업을 확인하고 삭제 확인 화면에서 사용자 본인이 처리한다. 기존 공유 자원과 보관기간 미정 데이터는 유지한다. 로컬 새 빌드 폴더는 ZIP/manifest 보관 후 그 폴더만 Finder에서 정리할 수 있고 `var/local-server`는 대상이 아니다.

## 10. ARC 제출은 공식 계약을 받은 뒤 별도로 연결하기

ARC의 테스트 URL·HTTP method·인증/갱신·사용자/과정/훈련 ID·결과 문서·성공/실패 응답·중복/timeout/재시도 규칙을 확보한다. HSTM 문서를 ARC 문서로 간주하지 않는다.

기존 내부 계산 후 Calculator 역할에서 Submit Lambda를 동기 호출하도록 연결하고, 계산 성공과 제출 성공을 별도 저장·반환한다. 응답 유실 시 ARC에 이미 접수됐는지 확인하는 방법이 없으면 자동 중복 제출을 임의로 허용하지 않는다. ARC 실패에도 계산 결과와 조회 경로는 보존한다.

그 전까지 `submit_arc={"status":"disabled","ok":false,"error":"arc_contract_pending"}`를 유지한다. 로컬 Mock Journey, AWS Dev Journey, 실제 ARC 제출은 각각 다른 인수 항목이다.

**지금 다음으로 할 일은 로컬 앱 현장 시험과 1번의 개인 접근·실자원 정보 확보다.** 정보를 확보한 다음 4번의 AWS 실행 연결 개발을 진행하면 된다.

## 부록 A. 환경 연결표·환경파일의 정확한 계약

아래는 `scripts/deployment_preflight.py`의 현재 오프라인 계약이다. 실제 값이나 전체 Journey 배포 구성은 아직 없으며, 형식 검사 통과를 자원 승인으로 해석하지 않는다.

### A1. 현재 가능한 일

`scripts/deployment_preflight.py`는 Python 표준 라이브러리만 사용한다.
설정 파일을 읽고 형식과 값의 연결 관계를 검사하며, SDK·AWS·ARC 호출이나 의존성 설치를 하지 않는다.
설정이 없는 환경은 기존 개발·운영 이름으로 대신 채우지 않고 중단한다.

| 배포 선택자 | 처리 | 실제 runtime 값 |
|---|---|---|
| `local` | AWS 배포 금지 | 로컬 서버 실행 설정을 바꾸지 않음 |
| `dev`, `development` | `development` binding 선택 | 저장된 문자열을 바꾸지 않음 |
| `beta` | 명시된 `beta` binding만 선택 | dev/prod로 fallback(대체 선택)하지 않음 |
| `prod`, `production` | `production` binding 선택 | 저장된 문자열을 바꾸지 않음 |
| 그 밖의 값 | 거절 | 자동 추정하지 않음 |

이 정규화는 **배포 선택자에만** 적용한다.
`STAGE`, `ARC_MOCK_ENVIRONMENT`, table, bucket, prefix, API stage를 선택자와 같은 이름으로 바꾸지 않는다.
특히 S3와 Lambda의 region(리전)은 실제 구성이 다를 수 있으므로 서로 같다고 강제하지 않는다.

### A2. 명시 설정 파일

JSON 파일의 최상위 필드는 `schema_version: 1`과 `environments`다.
`environments` 아래에는 알고 있는 실제 환경의 canonical key(정규 이름)만 둔다.
허용 key는 `local`, `development`, `beta`, `production`이다. 미확정 환경의 내용을 만들어 넣지 않는다.
JSON 중복 필드, 알 수 없는 필드, 비정상 자료형·비유한 수치는 거절한다.

선택한 환경의 필드는 다음과 같다.

| 필드 | 의미와 검사 |
|---|---|
| `account_id` | 명시적 12자리 계정 문자열. 코드에 있던 과거 계정 번호를 기본값으로 사용하지 않음 |
| `region` | Lambda·Gateway 명령의 명시적 리전. 실제 존재 검사는 하지 않음 |
| `runtime_stage` | env 파일의 `STAGE`와 정확히 같아야 하는 문자열 |
| `calculator` | 아래 Calculator 설정 객체. Calculator/Gateway 검사에 필요 |
| `gateway` | 아래 Gateway 설정 객체. Gateway 검사에는 필수 |

Calculator 객체의 필드:

- `function_name`, `role_name`: 기존 함수와 역할의 명시적 이름. 선택 필드 `role_arn`은 경로를 포함한 기존 ARN이며 같은 계정·마지막 역할 이름과 일치해야 한다.
- `memory_mb`: 일반 Lambda 구성의 128~10240 정수. `timeout_seconds`: 1~900 정수. 해당 계정의 더 낮은 한도는 별도 확인한다.
- `log_retention_days`: CloudWatch가 허용하는 일수 또는 `null`(기존 보관 설정 유지). 부록 A의 형식 검사는 실제 삭제 정책의 승인이 아니다.
- `reserved_concurrency`: 명시적 0 이상의 정수 또는 `null`. `null`이면 기존 동시성 설정을 변경하지 않는다.
- `storage_bucket`, `storage_prefix`: 보존된 계산기 소스의 저장 상수와 정확히 같아야 한다.
- `storage_region`: env 파일의 `ARC_STORAGE_REGION`과 정확히 같아야 한다.

저장 상수 검사는 `util/uploader.py`의 `BUCKET`, `RTDATA_DIRECTORY`를 AST(코드를 실행하지 않고 읽는 구조)로 확인한다.
SDK를 불러오는 `util.uploader` import는 하지 않는다.
이 상수를 설정 파일에 자동으로 채워 주지 않는다. **명시된 목적지가 현재 코드와 같은지를 검사하는 것**과 실제 사용 승인은 별개다.
이 필드를 적는 것만으로 계산기 저장 위치가 변경되지는 않는다. 다른 목적지가 필요하면 저장 코드·설정의 명시적 변경과 별도 검증이 필요하다.

Gateway 객체의 필드:

- `api_id`, `stage`: 기존 API와 stage의 명시적 식별자.
- `route`: 현재 이 배포 스크립트가 연결하는 `/cpr-analysis`만 허용한다.
- `rate_limit`, `burst_limit`: 둘 다 숫자 또는 둘 다 `null`. `null`이면 기존 제한을 변경하지 않는다.
- 숫자를 제공하면 해당 `/cpr-analysis`의 POST 설정만 변경하며, 로그인·목록을 포함한 stage 전체에 적용하지 않는다.

env 파일에 `ARC_API_GATEWAY_ID`, `ARC_API_STAGE`, `ARC_API_ROUTE`도 있다면 JSON binding과 정확히 같아야 한다.
셸에서 같은 이름의 변수를 설정해 JSON binding을 몰래 덮어쓸 수 없다.

### A3. env 파일과 비밀값

형식은 기존과 같이 한 줄의 `KEY=VALUE`다. LF·CRLF 줄 끝만 지원한다.
`export`, 중복 key, Lambda 예약 key, 값을 감싸는 따옴표는 거절한다. key는 영문자로 시작하는 2자 이상의 영문·숫자·밑줄이며 key와 value의 UTF-8 합계가 4096 bytes 이하여야 한다. [환경변수 제한](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html)
VT·FF·NEL·Unicode line separator·단독 CR을 새 설정 줄로 해석하지 않는다.
허용한 값의 공백·쉼표·`#`·`=`·문자 그대로의 `$()`·backtick을 셸 코드로 실행하지 않는다.
진짜 비밀값을 command argument(명령 인자)나 Git에 넣지 않는다.

env 파일을 `source`하거나 `eval`하지 않는다.
검증된 env snapshot(읽은 시점의 값)을 private directory(권한 0700)의 파일(권한 0600)에만 내보낼 수 있다.
이 파일은 Lambda의 `--environment file://...` 입력으로 사용하며 ZIP에는 포함하지 않는다.
성공·오류 메시지는 env 값이나 전체 AWS 응답을 출력하지 않는다.

### A4. 검사만 실행하기

실제 파일 경로가 준비된 뒤 다음 형식으로 검사할 수 있다. 아래 꺾쇠 표시는 실제 값으로 바꿀 자리이며 실행 가능한 기본 설정이 아니다.

```text
python3 scripts/deployment_preflight.py --environment <선택자> --component calculator --bindings <binding.json 경로> --env-file <env 파일 경로>
```

Gateway 설정 검사는 `--component gateway`를 사용한다.
development의 env 파일 기본 경로는 기존 `.env.dev`, production은 `.env`다.
beta는 env 파일 경로도 명시해야 하며 `.env`를 대신 읽지 않는다.
성공 결과는 `syntax_and_composition_valid`이며 `aws_resources_verified`·`runtime_verified`는 `false`다.
이 검사는 실제 배포 명령이 아니다.

### A5. 실제 배포 경로에 연결한 보호

두 배포 스크립트는 같은 검증기를 AWS·SDK 명령·pip·산출물 작업보다 먼저 실행한다.
명시 binding 경로는 세 번째 인자 또는 `ARC_DEPLOYMENT_BINDINGS`로 받는다. 기본 자원 설정은 없다.
실제 실행이 승인된 이후에는 STS의 계정이 명시 계정과 같은지도 변경 작업 전에 확인한다.

- Lambda 스크립트는 기존 role과 function을 재사용하며 기존 함수의 role·공개 handler를 확인한다.
- 기존 Handler가 `lambda_handler.run`과 다르면 새 코드를 올리기 전에 중단한다. 비공개 helper를 공개한 상태에서 새 ZIP을 설치하지 않는다.
- 설정 갱신에도 `--handler lambda_handler.run`, `--runtime python3.12`를 명시한다.
- 역할·함수·로그 그룹·IAM 저장 권한을 임의로 생성하지 않는다. 기존 리소스나 권한이 없으면 별도 준비가 필요하다.
- Gateway는 기존 API·resource·method·stage와 공개 handler를 확인한다. 새 `NONE` 인증 method나 wildcard Invoke 권한을 만들지 않는다.
- throttle 설정 범위는 해당 POST지만 `binaryMediaTypes`는 REST API 전체 설정이고 `create-deployment`는 Stage 배포다. 실제 배포 전에 API 공동 사용 여부, 다른 경로의 미배포 변경과 복구 범위를 확인해야 한다. 이 영향 범위는 오프라인 설정 검사만으로 검증되지 않는다.
- 기존 실제 IAM scope(권한 범위), 원격 팀원 접근 보호, Invoke 권한의 적절성은 아직 별도 인수가 필요하다.

ZIP은 `build_mock_artifact.py`의 허용 목록으로만 만든다.
전체 저장소 rsync를 제거해 로컬 DB·키·Python 실행 환경·로컬/통합 시험 파일이 자동으로 섞이는 경로를 없앴다.
선택한 소스·필수 프롬프트·검증한 Python 의존성만 포함한다. pip의 최상위 `bin` 실행 스크립트는 포함하지 않는다.


### A6. CI 연결값

Repository variable `ARC_DEPLOYMENT_BINDINGS`에는 검토한 JSON 연결표의 파일 경로를 지정한다. 해당 환경 설정이 없으면 자격 증명 설정 전에 실패한다. 같은 배포 환경의 작업은 브랜치가 달라도 직렬화하고 임시 환경파일은 종료 시 제거한다. 이는 실제 GitHub Environment 승인·회귀 시험·Beta 배포 구성이 완료됐다는 뜻이 아니다.

## 부록 B. ConfigManager·IAM 자료

`scripts/config_manager_v2/lambda_function.py`는 `clientid=arc`와 query의 `type`을 검사하고 기본 `config-manager/arc/{type}` Secret의 JSON을 반환한다. 허용 type은 `dev`, `testflight-prod`, `prod`이며 API 배포 선택자 `development`/`beta`/`production`과 별개의 이름이다. `SECRET_PREFIX`와 리전의 코드 기본값을 실제 운영 승인값으로 간주하지 않는다.

`scripts/config_manager_v2/seeds/`의 JSON은 구조 템플릿으로 유지한다. dev에는 Cognito 5필드가 없고, prod/testflight-prod는 Watermark만 다르며 CalcURL 배열의 항목 수는 dev 1개, 나머지 2개다. 자리표시자를 채운 사본은 Git에 넣지 않는다. 이 함수는 읽은 Secret 문자열을 프로세스 메모리에 캐시하므로 Secret 수정만으로 다음 호출에 즉시 반영된다고 보장하지 않는다. 실제 함수·API·HMAC 보호·로그 정제·교체 반영은 별도 인수 대상이다.

`docs/iam/`의 정책 JSON 3개는 과거 참고 템플릿으로만 남긴다. 진행 기록과 달리 설정 검토 자료지만, 내부 계정 번호·넓은 권한을 승인된 운영값으로 사용하거나 그대로 적용하지 않는다. 실제 환경·역할별 최소 권한으로 다시 검토한다. 원래 각 폴더의 README는 이 부록으로 통합했다.

## 부록 C. 문서·개발 기록과 배포물

핵심 문서 9개와 별개로 `.documentation-backup/`의 원본·진행 JSON·patch는 이 PC에만 보관한다. Git과 배포 ZIP에서 제외한다. DB 저장 대상은 앱의 훈련 진도·계산 결과·운용 로그다. 개발 과정의 JSON·patch를 DB에 옮기라는 요구가 아니다. [N03~N06](DECISIONS.md)에 따라 기존 상태/결과 참조 DB·비공개 파일 코드를 재사용하고, 주요 이력·정제된 상세 진단의 DB 기록을 로컬에 연결했다. 실제 AWS 연결·배포는 후속 인수다.

### 운용 로그를 AWS에 연결할 때

로컬용 로그 저장 코드는 `services/operational_logs.py`와 `mock_journey/log_storage.py`다. 공용 API/Worker 조립에 기록기를 명시적으로 전달할 수 있다. 현재 AWS runtime에는 자동 연결하지 않았으며 환경변수 한 개로 활성화되는 기능도 아니다.

- 기존 훈련 상태·결과 참조 DB와 비공개 S3 파일 구조를 유지한다. 결과 본문을 새 DB에 중복 저장하지 않는다.
- 실제 환경·Table·Role을 확인하고 로그용 client와 저장 대상을 명시적으로 연결한다. 로컬 가짜 자격 증명·endpoint를 AWS에 복사하지 않는다.
- 현재 로그 저장기는 `PutItem`, 동일 레코드 중복 확인의 `GetItem`을 사용한다. 관리자 조회는 `Query`다. 실제 테이블과 `OPS#…` 기록 영역에 필요한 권한만 검토하고, 이 목적으로 `Scan`·삭제·테이블 생성 권한을 추가하지 않는다. 로컬 초기화 권한을 그대로 복제하지 않는다.
- Lambda의 실행 종료/정지 때도 로컬 daemon이 계속 기록한다고 가정하지 않는다. 실제 API·Worker·Relay 실행 수명에 맞는 전달/배출 연결과 장애·재시작 검증이 남아 있다. 로그 응답을 기다리게 만들어 훈련을 막는 방식은 승인된 정책에 맞지 않는다.
- 로그 폭증이나 저장 지연이 계산용 DB 처리량·동시성·비용을 잠식하지 않는지 검증한다. 로컬의 유한 대기열은 원격 저장 용량·처리량 격리의 증거가 아니다.
- 주요/정제된 상세 진단만 수집한다. Gateway의 전체 body 추적이나 stdout 전체 복사로 수집 범위를 넓히지 않는다. 로그 누락·저장 미확정과 정상 훈련 실패를 다른 지표로 알린다.
- 보관기간 확정 전 자동 삭제를 추가하지 않는다. 원격 DB·CloudWatch 등 수집 목적지의 실제 TTL/lifecycle과 백업도 확인한다. 자동 삭제 없는 보관과 무손실 로그 전달은 다른 조건이다.

Dev·Beta·Prod 각각에서 로그 저장 실패 중 정상 훈련, 저장된 결과 재조회, 환경 간 기록 격리, 비밀값 미기록과 재시작 뒤 조회를 검증한다. 로컬 CLI의 조회 성공을 AWS 로그 운영 완료로 보고하지 않는다.

## 부록 D. 운영 중 점검·장애·복구

### 알림과 비용

Monitoring(상태 감시)은 로그가 쌓이는 것만으로 끝나지 않는다. 실패·작업 적체·가장 오래 기다리는 작업·결과 불명·저장 오류·접근 실패 증가를 확인할 지표를 정하고 내 연락 수단으로 알림이 실제 도착하는지 시험한다. 정상 처리량을 측정한 뒤 알림 기준을 정한다. 비용 예상과 Budget alert(예산 알림)도 기록하되 알림이 모든 서비스의 비용을 자동 차단한다고 생각하지 않는다.

### 장애가 났을 때

`outcome_unknown`은 점수 Fail이 아니다. 같은 Job의 저장 입력·후보·채택 결과를 확인하고 현재 내부 계산 복구 절차로 처리한다. 정상 결과 GET은 계산을 다시 실행하지 않는다. 실제 ARC 제출의 반영 여부가 불명인 경우는 별도 제출 상태이며, 공식 계약 없는 자동 재제출 정책을 만들지 않는다.

DLQ로 이동했다고 모든 반복이 멈췄다고 보지 않는다. 현재 주기적 재확인은 대기열과 별도로 아직 끝나지 않은 DB 작업을 다시 보낼 수 있다. 재전달 전 `job_id`, 계산 실행·제출 의도 기록, 계약 버전, 원본·결과 파일 보존을 확인한다. 이미 완료된 작업, 확정 실패, 결과 불명, 아직 전송 전인 작업을 구분한다. **전송 여부를 지우거나 상태를 처음으로 되돌리지 않는다.** 필요한 대기열 수신과 주기적 재확인을 함께 제어하고, 복구 가능한 소수의 시험 작업에서 먼저 결과를 확인한 다음 범위를 넓힌다.

Backup 복구는 먼저 격리된 위치에서 수행하고 복구본의 작업 트리거를 바로 켜지 않는다. 오래된 DB에는 최신 전송·로그아웃·권한 폐기 기록이 빠질 수 있다. 현재 파일·키·확정 결과·제출 기록과 대조하여 중복 계산, 폐기된 세션, 초기화 전 진도가 되살아나지 않는지 확인한 뒤 적용한다. 이를 검증할 근거가 없으면 복구본을 서비스에 연결하지 않는다.

### 키·계정 유출 또는 분실

노출된 것이 내 관리 접근, 팀원 접근, 앱 세션, 재연결 서명 키, 차트 링크 중 무엇인지 구분한다. 영향 있는 권한을 제한하고 비밀값은 교체하되 기존 작업과 재연결에 미치는 영향을 기록한다. 서명 키 한 개를 바꾸면 모든 종류의 토큰과 이미 발급한 차트가 동시에 폐기된다고 생각하지 않는다. 개인정보·비밀값을 더 복사하지 않으면서 필요한 사건 기록을 보존한다.

### 테스트 종료·정리

계정 회수 → 새 입력 중단 → 남은 작업 확인 → 보관 대상 확정 → 승인한 삭제 순서로 진행한다. 시험용 계정 로그아웃만으로 원본·결과·로그가 삭제되지는 않는다. 저장소의 이전 버전·복구용 복사본까지 정리 범위를 확인하고, 실제 삭제 여부를 조회한다. 복구 절차가 정말 동작하는지도 별도 격리된 시험 자료로 연습한다.


## 부록 E. Kubernetes를 별도로 채택하는 경우

이 절은 **Kubernetes 채택 시에만 추가되는 작업**이다. 현재 Lambda 코드·ZIP을 그대로 올리면 실행되는 배포 절차가 아니다. 컨테이너의 HTTP 입구, 작업 수신·종료, 상태·권한 연결을 별도로 설계하고 구현해야 한다.

| 추가 확인 | 비개발자도 이해할 완료 기준 |
|---|---|
| Cluster(공동 실행 환경) 관리 접근·RBAC(역할별 권한) | 내 일상 조회와 변경 권한을 구별하고 앱 작업에 관리자 권한을 주지 않음 |
| Namespace(자원 묶음)·네트워크·실행 격리 | 이름표만 다르게 붙인 것을 보안 격리로 보지 않음. 다른 환경 접근이 실제 거절됨 |
| NetworkPolicy(통신 허용 규칙) | 선택한 네트워크 플러그인이 정책을 집행하고, 차단·허용 시험을 통과함 |
| Secret(비밀 설정)·저장 암호화 | base64(문자 형태로 포장)와 암호화를 구분하고, 비밀을 읽을 사람·작업과 저장 보호를 확인 |
| Container(격리된 실행 묶음)·이미지 | 이미지의 출처·고정된 지문·취약점을 확인하고 필요한 최소 실행 권한 사용 |
| Readiness(새 요청을 받을 준비)·Liveness(재시작이 필요한 실행 이상) | 두 검사를 구분. 검사 자체가 계산을 요청하지 않고, 저장·계산 장애를 무조건 재시작으로 처리하지 않음 |
| 작업 수신·종료 | Readiness가 false여도 대기열 수신은 별도로 계속될 수 있음. 직접 수신을 멈추고 진행 중 작업을 안전하게 마무리하는 코드와 시험 필요 |
| 용량·복구·업데이트 | 자동 재시작이 결과 복구를 대신하지 않으며 DB·파일·키·작업 버전까지 복구 시험 |

Kubernetes 보안 지침도 비밀 보호, 역할별 권한, 네트워크 정책의 실제 적용을 별도로 다룬다. 상태 검사와 종료는 각각 요청 배정·재시작·진행 작업의 처리를 확인한다. 이 표는 도입 시 확인할 항목이며 현재 클러스터가 안전하다는 판정이 아니다. [Kubernetes 보안 지침](https://kubernetes.io/docs/concepts/security/security-checklist/), [실행 상태 검사](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/).
