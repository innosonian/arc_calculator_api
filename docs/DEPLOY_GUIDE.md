# AWS·Dev 배포와 인수 안내

2026-09-23 기준. **새 `/api/v2` Dummy Dev의 자원 연결과 stage 생성 후 사용자 출력으로 HTTPS 로그인·조회 및 기존 압박 파일 한 건의 실제 계산·일반 훈련 완료·진도 반영, 저장 원본·최종 결과 일치와 운용 로그를 확인했다. 일반 훈련 차트 다운로드·서명 없는 접근 차단과 최종평가 합격·과정 FINISHED까지 확인했다. 해당 기본 흐름은 통과했고 나머지 AWS 인수는 진행 중이다.** 로컬 검증과 실제 AWS 확인 범위는 [VALIDATION](VALIDATION.md)을 따른다. 이 문서의 AWS 명령은 사용자가 승인한 대상에 실행할 절차이며 완료한 생성 명령을 처음부터 반복하지 않는다.

기본 AWS 조립은 `/mock/v1`과 인증된 `/cpr-analysis`를 제공한다. **이번 목표는 API·Worker에 `course_v2_dummy`를 명시한 새 `/api/v2` Dev 시험**이다. 내장 Dummy 임시 과정 15개(기존 5프로그램×3연령, 각 훈련→최종평가)를 사용하고 실제 업로드 바이너리로 계산한다. 영상·문서 자료나 공식 ARC 배정을 만들지 않는다. Dummy 결과는 ARC 제출 `excluded`, 일반 비활성 상태는 `disabled`, CPR 완료는 `pending_policy`다. 생성된 Dev 주소와 완료한 검증 범위를 함께 앱팀에 안내한다.

실제 주소·계정·리전·자원·한도·비용·보관기간을 이 문서에서 정하지 않는다. 기존 계산·null·코칭, 인증·소유권·멱등성·epoch·lease/fence, 비공개 파일을 유지한다. CPR 완료는 `pending_policy`, ARC 제출은 `disabled`, 실제 HSTM 전송은 금지다. commit·push·AWS 변경·배포는 사용자 결정이다.

<a id="beginner-dev"></a>
## 0. 처음 배포하는 사람의 순서

이 서비스는 컴퓨터 한 대에 파일 하나를 복사하면 끝나는 형태가 아니다. **접수 담당(API), 계산 담당(Worker), 작업 전달 담당(Relay)** 세 프로그램과, 기록장(DynamoDB), 파일 보관함(S3), 작업 대기줄(SQS)을 연결한다. 아래에서 `확인`은 읽기, `업로드·변경·활성화`는 실제 AWS에 영향을 주는 작업이다.

1. **AWS 웹사이트에 로그인한다.** 기존에 받은 계정 ID/별칭·IAM 사용자 이름으로 로그인한다. 별도 작업 Role을 받았다면 [2절의 역할 전환](#aws-personal-access)까지 수행한다. 새 계정이나 새 접근 키를 만들 필요는 없다. 화면 위 계정과 리전을 확인한다. 이번 작업은 Dev이며 운영 서버를 선택하지 않는다. 앱용 공용 Dummy 계정과 AWS 관리 계정은 다른 것이다.
2. **이미 있는 것의 이름을 적는다.** AWS 검색창에서 `Lambda`, `API Gateway`, `DynamoDB`, `S3`, `SQS`를 차례로 열어 아래 표를 채운다. 이름에 dev가 있어도 운영과 같이 쓰는 자원인지 확인한다. 모르는 칸은 추측하지 말고 비워 둔다. 조회 명령은 3절에 있다.
3. **없는 자원과 바꿔도 되는 자원을 구분한다.** 기존 자원을 재사용하는 것이 기본이다. 필요한 것이 없으면 새 Dev 전용 자원을 만들지, 기존 것을 연결할지 결정해야 한다. 이 저장소는 실제 이름·권한을 모른다. 이 단계까지는 삭제·생성·저장을 누르지 않아도 된다.
4. **서버 설정 세 장을 작성한다.** `api-runtime.json`, `worker-runtime.json`, `relay-runtime.json`이 각각 세 담당자의 주소록이다. 4절의 형식에 실제 이름과 선택한 한도를 넣는다. API·Worker에는 같은 `course` 설정을 넣는다. 개별 검사와 세 장 묶음 검사 모두 통과시킨다. 예제 시험값은 자동으로 운영값이 되지 않는다.
5. **배포할 ZIP 한 개를 만든다.** 8절의 빌드 명령을 사용한다. `mock-lambda.zip`은 세 Lambda가 함께 쓰지만 시작 함수(Handler)는 서로 다르다. `.env`, DB, 키 파일, 원본 훈련 파일, 컴퓨터의 가상환경을 압축해서 올리지 않는다.
6. **되돌릴 준비를 한다.** 기존 Lambda 코드·설정·키 버전, API의 배포 번호, 큐/예약 연결과 진행 중 작업을 보호된 위치에 보관한다. 변경 전후 함수·DB·파일 이름을 비교한다. 이미 접수된 작업을 처리할 Worker를 끄거나 기존 데이터·키를 지우는 방식으로 시작하지 않는다.
7. **세 Lambda에 코드를 올린다.** 승인한 Dev 함수에서 `Code → Upload from → .zip file`로 같은 ZIP을 올리고 아래 Handler를 각각 지정한다. Python 3.12를 사용한다. `Configuration → Environment variables`에 각자에게 맞는 설정을 넣는다. 처음부터 앱에 공개하지 말고 Worker·Relay 준비를 먼저 확인한다. 기존 Calculator용 자동 스크립트 두 개만으로 이 세 역할이 모두 설치되지는 않는다.
8. **기록장·보관함·대기줄을 연결한다.** 5절의 DB 키/인덱스, 비공개 S3와 역할별 권한을 맞춘다. SQS 트리거는 Worker에 연결하고 `ReportBatchItemFailures`를 켠다. Relay는 새 작업 알림용 DynamoDB Stream과 재확인용 EventBridge 예약에 연결한다. 예약은 6절의 진행 행을 처음 준비한 후 켠다. Queue visibility는 Worker timeout의 6배+batch window 이상이어야 한다. 실제 연결 권한과 실행 결과도 확인한다.
9. **앱이 들어오는 문을 연결한다.** API Gateway의 **REST API**에서 `/api/v2/{proxy+}`의 `ANY`를 API Lambda의 **Lambda proxy integration**으로 연결할 수 있다. 이미 같은 경로가 있으면 먼저 충돌을 확인한다. 앱의 `Authorization: Bearer ...`와 원래 경로·query를 그대로 넘긴다. `multipart/form-data`를 binary media type으로 설정하고 원본 파일 hash 보존을 시험한다. 변경한 뒤 승인한 Dev stage로 `Deploy API`한다. 공유 API의 다른 미배포 변경까지 포함되는지 먼저 확인한다. [AWS proxy 안내](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-set-up-simple-proxy.html), [binary 안내](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-payload-encodings.html)
10. **작게 한 번 시험한다.** 앱팀에 Dev 주소와 [APP_API](APP_API.md)를 전달한다. 로그인→`[Dummy Dev]` 과정 15개 조회→성인 압박 Only 훈련→실제 누적 파일 업로드→결과/차트 조회→최종평가를 확인한다. 점수가 나온 것과 프로그램 통과를 구별한다. 처음에는 `202`(접수/처리 중), 완료 후 `200`(결과)이며 Dummy 제출은 `excluded`다. `/healthz`는 AWS 확인 주소가 아니다.
11. **실패 시험과 비용을 확인한다.** 다른 세션의 결과 접근 거절, 같은 업로드 재전송, 로그아웃 후 공유 진도 초기화와 결과 보존, 차트 만료, 작업 재전달을 9절대로 시험한다. CloudWatch 오류·SQS 대기/DLQ·DB 운용 로그·AWS 비용을 확인한다. 경보 수신자와 시험 예산도 정한다. 통과한 주소만 앱팀의 시험 주소로 사용한다.

| 적을 내용 | 찾는 위치·뜻 |
|---|---|
| 계정 ID, Dev 리전 | AWS 화면 상단. 비밀번호·접근 키는 적지 않음 |
| API 이름/ID, Dev stage | API Gateway. 앱 주소 앞부분 결정 |
| API/Worker/Relay 함수 이름·실행 Role | Lambda. 각각 Handler는 `lambda_handler.run`, `mock_journey.worker.run`, `mock_journey.dispatch.run` |
| DB table·Stream ARN | DynamoDB. 세 역할이 같은 Dev table 사용 |
| 비공개 bucket·directory·stage | S3. API/Worker가 동일한 저장 경로 사용 |
| 작업 Queue URL·DLQ·EventBridge 예약 | SQS와 EventBridge. 전달·실패·재확인 연결 |
| 함수 timeout·메모리, Queue visibility·batch window | Lambda/SQS 설정. 4절 묶음 검사에 실제값 입력 |
| 공유 여부·변경 허용·비용 예산 | 기존 자원 담당 기록. 모르면 확인 후 변경 |

이번 Dev는 사용자 선택(D94)에 따라 팀 공용 Dummy `test@test.com` / 문자열 `2222`를 사용한다. 별도 개인 인증 구축을 이번 배포 조건으로 요구하지 않는다. 이 값은 소스에도 공개되어 있어 인터넷 전체 공개 시 외부인도 로그인할 수 있으며, 구두 공유 자체가 접근 제한은 아니다. 기존 Gateway 보호가 있다면 제거하지 않는다. HTTPS·세션·소유권·비공개 파일·호출량 제한은 유지하고 노출 범위와 실제 제한값을 배포 전에 확인한다.

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
## 2. 기존 IAM 사용자로 로그인하고 작업 역할로 전환

사용자가 확인한 로그인 방식은 계정 ID/별칭·IAM 사용자 이름·비밀번호다. 2026-09-22 사용자가 공유한 관리자 회신에 따르면 개인 작업용 `arc-dev-operator-role`과 CI/CD용 `gha-arc-calc-dev-deploy`가 준비되어 있다. 기존 접근과 준비된 역할을 사용하며 SSO 설정이나 새 관리 Role 생성을 요구하지 않는다. 회신의 권한 목록은 실제 정책·제한·자원 접근을 검증한 결과와 구별한다.

웹 화면에서 작업하는 사람은 먼저 다음만 수행한다. 아래 CLI 설정 파일을 편집할 필요는 없다.

1. 기존 IAM 사용자로 로그인하고 등록한 MFA 장치로 본인 인증한다.
2. 화면 오른쪽 위 사용자/계정 메뉴 → `Switch role(역할 전환)`을 누른다. 다중 세션 기능을 사용하는 화면이면 `Add session(세션 추가) → Switch role`을 선택한다.
3. `Account ID`에는 관리자가 안내한 12자리 계정 번호, `Role`에는 **`arc-dev-operator-role`**을 넣는다. Role 입력란에는 전체 ARN을 넣지 않는다. 선택 사항인 표시 이름은 `ARC Dev`처럼 알아보기 쉽게 정한다.
4. `Switch Role`을 누른 뒤 오른쪽 위 메뉴에서 역할과 계정 번호가 맞는지 확인한다. 자원 리전은 이번에 선택한 **미국 동부(오하이오), `us-east-2`**로 맞춘다. [AWS 역할 전환 안내](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_switch-role-console.html)

`...:user/<사용자>`의 `dynamodb:CreateTable` 거절은 그 요청이 개인 IAM 사용자 권한으로 실행됐다는 뜻이다. 준비된 역할의 권한으로 재시도하는 것이 다음 단계다. 역할 전환 자체가 거절되면 관리자에게 해당 오류와 MFA 로그인 여부를 전달해 전환 권한·신뢰 정책을 확인한다. 전환 후에도 생성이 거절되면 **새 오류의 principal·action·resource**로 역할 정책/조건을 확인한다. 비밀번호·MFA 코드·키는 전달하지 않는다. 사용자에게 직접 같은 권한을 붙이거나 전체 관리자 권한을 새로 요청할 필요가 있는지는 이 확인 뒤 판단한다.

`arc-dev-operator-role`은 사람이 콘솔에서 일할 때, `gha-arc-calc-dev-deploy`는 자동 배포가 실행될 때 쓰는 역할이다. API/Worker/Relay Lambda의 실행 역할은 해당 함수에 필요한 범위로 별도 준비한다. 사람의 작업 역할을 Lambda 실행 역할로 지정하지 않는다.

관리자 회신에 따르면 비용 조회와 예산 알림은 FitCloud에서 처리하며 계정 전달을 기다리는 상태다. AWS 비용 화면 접근을 배포 준비의 선행 단계로 반복 요구하지 않는다. 실제 비용 확인 경로·시험 예산·알림 설정은 아직 확인할 항목이다.

CLI를 사용하는 이후 단계에서는 아래의 `<...>`를 승인된 실제 값으로 바꾼다. 로그인 리전과 자원 리전은 다를 수 있다. 관리자가 안내한 기존 `moon`/`arc-dev` 프로필이 준비되어 있다면 이를 우선 사용하고 중복 프로필이나 접근 키를 만들지 않는다. 콘솔의 역할 전환만으로 별도 터미널의 CLI 프로필까지 바뀌지는 않는다. [AWS CLI 역할 프로필 안내](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html)

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

<a id="beginner-runtime-settings"></a>
### 처음 하는 사람: ‘서버 설정 작성’에서 막혔을 때

이 단계는 프로그램에게 **어느 AWS 계정의 어떤 기록장·파일 보관함·작업 대기줄을 사용할지 알려 주는 메모 세 장**을 만드는 일이다. 그 메모의 파일 형식이 JSON이다. 사용자가 JSON 문법이나 수십 개의 개발 설정을 직접 정할 필요는 없다. 먼저 실제 자원의 이름을 확인하고, 그 자료로 설정을 구성·검사한다. AWS에 파일을 올리거나 설정을 저장하는 것은 이후 단계다.

| 파일 | 받는 프로그램 | 쉬운 뜻 |
|---|---|---|
| `api-runtime.json` | API | 앱의 로그인·훈련 요청을 받는 담당자의 주소록 |
| `worker-runtime.json` | Worker | 계산을 하고 결과를 저장하는 담당자의 주소록 |
| `relay-runtime.json` | Relay | 대기 중인 일을 계산 담당자에게 전달하는 담당자의 주소록 |

첫 확인은 AWS 웹 화면에서 한다. 이미 적어 둔 값은 다시 찾을 필요가 없다. 이름에 `dev`가 있다는 이유만으로 이 서비스의 자원이라고 선택하지 않는다. 여러 개여서 모르겠으면 후보 이름을 적는다. 목록이 비어 있어도 곧바로 자원이 없다고 단정하지 않고 계정·리전·조회 권한부터 확인한다.

1. **계정 ID:** AWS 화면 오른쪽 위 계정 메뉴에서 12자리 계정 ID를 확인한다. IAM 사용자 이름이나 앱 로그인 이메일과 다르다. [AWS 계정 ID 안내](https://docs.aws.amazon.com/accounts/latest/reference/manage-acct-identifiers.html)
2. **리전:** 화면 위 리전 메뉴에 보이는 지역 이름과 코드다. 예를 들어 서울 표시는 `ap-northeast-2`지만 예제대로 바꾸지 말고 기존 Dev 자원의 리전을 적는다. S3는 버킷 자체의 리전도 확인한다.
3. **기록장 이름:** 위 검색창에 `DynamoDB` 입력 → 해당 서비스 → 왼쪽 `Tables(테이블)` → 사용하는 테이블 이름을 복사한다. 데이터 항목의 내용은 필요 없다. [AWS 테이블 화면 안내](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Tagging.Operations.html)
4. **파일 보관함 이름:** 검색창에 `S3` 입력 → `General purpose buckets(범용 버킷)` 목록에서 버킷 이름을 복사한다. 파일 다운로드 주소나 서명 링크가 아니라 버킷 이름이다. [AWS S3 안내](https://docs.aws.amazon.com/AmazonS3/latest/userguide/create-bucket-overview.html)
5. **작업 대기줄 주소:** 검색창에 `SQS` 입력 → `Queues(대기열)` → 해당 이름 클릭 → 상세 정보의 URL을 복사한다. 브라우저 주소창의 관리 화면 주소나 `arn:`으로 시작하는 ARN과 다르다. 큐 종류가 `Standard`인지도 적는다. [AWS SQS 화면 안내](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-overview.html), [Queue URL 안내](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-queue-message-identifiers.html)
6. **프로그램 이름:** 검색창에 `Lambda` 입력 → `Functions(함수)` 목록에서 관련 함수 이름을 적는다. API/Worker/Relay 중 어느 것인지 모르겠으면 이름만 적어도 된다. 원래 세 개가 이미 준비되어 있다고 가정하지 않는다.

다음 양식에 확인한 것만 채운다. `모름`, `목록에 없음`, `접근 거부`도 유효한 답이다. 비밀번호·Access Key·Bearer·복구 키·전체 환경변수는 붙이지 않는다.

```text
AWS 계정 ID:
Dev 리전:
DynamoDB 테이블 이름:
S3 버킷 이름 / 버킷 리전:
SQS 큐 URL / 종류:
Lambda 함수 이름들:
이 자원들을 다른 서비스도 사용하는지: 알고 있음 / 모름
```

이것은 설정 작성을 시작할 정보이며 배포 준비 전체가 끝난 것은 아니다. 다음으로 기존 환경 이름·S3 저장 경로·키 버전·진행 중 작업, 함수의 시간/메모리와 큐 설정을 확인한다. 이미 쓰는 값은 보존하고 새 용량·비용 결정은 별도로 설명한다. `dev`라는 이름을 모든 칸에 넣지 않는다. API Gateway stage, 파일 저장용 stage, 인증 namespace는 서로 다른 설정이다. 복구 키는 **있음/없음/모름**부터 확인하고 키 값 자체는 대화에 받지 않는다.

코드가 정한 버전·새 API 모드 등은 설정 작성자가 채운다. 파일은 아래 경로에 준비하고, 먼저 오프라인 검사한다. 파일이 아직 없거나 값이 미완성이면 아래 검사 명령을 실행해도 통과하지 않는다. 내용이 준비되면 저장소 폴더에서 세 개의 개별 검사와 한 개의 묶음 검사를 실행한다. `configuration_valid`/`dummy_dev_bundle_valid`는 문법·구성 통과이며 실제 AWS 배포 성공이라는 뜻이 아니다.

검사를 마친 뒤 실제 배포 단계에서 Lambda의 `Configuration(구성) → Environment variables(환경 변수) → Edit(편집)`에 넣는다. `ARC_JOURNEY_CONFIG`의 값은 해당 JSON **내용 전체**이며 `api-runtime.json`이라는 파일 이름을 넣는 것이 아니다. API에는 API 설정, Worker에는 Worker 설정, Relay에는 Relay 설정을 넣는다. 기존 값을 보존하고 아직 확정하지 않은 설정을 AWS에 먼저 저장하지 않는다. [AWS 환경변수 화면 안내](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html)

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
| `course` — 이번 Dev의 API/Worker | `mode="course_v2_dummy"`, `catalog_version="arc-dummy-dev-v1"`, `settings`에 아래 9개 한도 전부. Relay에는 넣지 않음 |
| `api` — API | `payload_limit`: body 문자열 UTF-8 크기 제한, 양의 정수 bytes |
| `worker` — Worker | `lease_seconds`, `retry_seconds`: 양의 정수 초; `renewal_interval_seconds`, `renewal_timeout_seconds`: 양수 초; `processing_reserve_ms`: 양의 정수 |
| `relay` — Relay | `queue_url`, `lease_seconds`, `retry_seconds`, `page_size`, `max_pages`, `processing_reserve_ms`. 수치는 양의 정수; Queue는 같은 계정·리전의 Standard SQS |

Worker는 갱신 interval≤lease/3, interval+갱신 timeout<lease를 검사한다. Relay는 SDK timeout/시도·DB 충돌·로그/반환·시계 여유를 포함한 예산을 검사한다. `page_size × max_pages`는 각 종류의 논리적 조회 예산이며 실제 Query는 `Limit=1`이다. 설정 검사가 실제 AWS의 전체 실행시간을 보장하지는 않는다.

`course.settings`의 필수 양의 정수는 `max_course_items`, `max_assignments`, `max_bundle_bytes`, `max_control_body_bytes`, `max_intervals_per_report`, `max_merged_intervals_per_start`, `max_reports_per_start`, `max_transaction_actions`, `max_conflict_retries`다. Dev 카탈로그를 수용하려면 과정 항목≥2·배정≥15·transaction actions≥7이며 충돌 재시도≤8이다. 실제 bundle 크기와 `max_bundle_bytes`/`storage.artifact_bytes`도 검사한다. 이것은 구현의 수용 조건이며 권장 운영 한도표가 아니다. `storage.stage`는 `dev` 또는 `development`여야 한다. `course`를 생략하면 새 API로 전환되지 않는다. API/Worker에서 같은 값을 사용한다.

현재 adapter는 `arc-internal-detection-pending-v3`, projection은 `arc-local-projection-v1`이다. `retained_adapter_versions`는 `[]` 또는 `["arc-local-calculator-pending-v2"]`만 지원한다. 기존 작업을 조사한 후 선택하며, v2는 저장된 유효 후보 검증·차트 복구만 지원하고 계산을 다시 실행하지 않는다. 미지원 옛 작업을 새 버전으로 바꾸거나 가짜 Fail로 확정하지 않는다. 완료 결과는 저장 bytes로 조회한다.

새 runtime은 하나의 `region`을 DynamoDB·S3·SQS·로그 client에 적용한다. `AWS_REGION`, `AWS_DEFAULT_REGION`, `ARC_MOCK_REGION`, `ARC_STORAGE_REGION`이 함께 있으면 일치해야 한다. AWS 예약 변수는 사용자 환경파일에 추가하지 않는다. `STAGE`, `ARC_MOCK_ENVIRONMENT`, `ARC_MOCK_TABLE_NAME`도 JSON과 일치해야 한다. 교차 리전이 필요하면 연결 확장을 검토하며 실제값을 바꾸어 검사를 우회하지 않는다. `AWS_ENDPOINT_URL*` 재지정은 거절된다.

```sh
var/local-python/bin/python -m mock_journey.aws_settings --role api --config var/deployment/api-runtime.json
var/local-python/bin/python -m mock_journey.aws_settings --role worker --config var/deployment/worker-runtime.json
var/local-python/bin/python -m mock_journey.aws_settings --role relay --config var/deployment/relay-runtime.json
```

종료 0·`configuration_valid`는 형식 검사 성공이며 `aws_access_checked=false`, `secrets_checked=false`다. SDK나 AWS를 호출하지 않는다. 실제 자원·권한 검증을 대신하지 않는다.

개별 통과 후 역할 간 같은 계정·리전·환경·DB·저장·실행/과정 버전과 실제로 선택한 시간값을 함께 검사한다. 다음 `<...>`는 시험 기본값이 아니라 배포할 자원에서 확인한 정수 **초**다.

```sh
var/local-python/bin/python scripts/validate_aws_dev_bundle.py \
  --api-config var/deployment/api-runtime.json --api-timeout '<API timeout>' \
  --worker-config var/deployment/worker-runtime.json --worker-timeout '<Worker timeout>' \
  --relay-config var/deployment/relay-runtime.json --relay-timeout '<Relay timeout>' \
  --queue-visibility '<SQS visibility>' --batch-window '<SQS batch window>'
```

`dummy_dev_bundle_valid`와 종료 0을 확인한다. `aws_resources_verified=false`, `secrets_verified=false`, `deployment_performed=false`가 정상이다. 서로 다른 DB·버전·저장 한도, 너무 짧은 Queue visibility, Relay가 한 항목도 처리할 수 없는 timeout은 거절한다. [AWS SQS 시간 설정 근거](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)

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

<a id="beginner-new-dynamodb"></a>
### 새 Dev용 DynamoDB를 만드는 경우

기존 자원의 용도·공유 연결이 확인되지 않고 선택한 Dev 리전에 전용 DB가 없다면, 기존 HSTM/제출/외부 Scoring 자원과 분리한 새 테이블을 권한다. 2026-09-22 공유된 관리자 요청에 맞춰 새 자원 이름은 `arc-calc-dev-*`, 태그는 `Project=arc-calc`, `Env=dev`로 안내한다. 다음 이름은 아직 생성 여부를 확인하지 않은 **추천안**이다. S3의 `<ACCOUNT_ID>`는 실제 12자리 계정 번호로 바꾸며 이름 사용 가능 여부도 생성 시 확인한다. 이미 만들어진 자원은 이름을 맞추려고 삭제하지 않는다.

| 새 자원 | 추천 이름 |
|---|---|
| DynamoDB | `arc-calc-dev-training` |
| 비공개 S3 | `arc-calc-dev-storage-<ACCOUNT_ID>` |
| Standard SQS 작업 큐 | `arc-calc-dev-calculation-jobs` |
| Standard SQS 실패 큐(DLQ) | `arc-calc-dev-calculation-dlq` |
| API / Worker / Relay Lambda | `arc-calc-dev-api` / `arc-calc-dev-worker` / `arc-calc-dev-relay` |
| REST API | `arc-calc-dev-rest-api` |

이전 추천 `arc_training_dev`는 생성 전이라면 `arc-calc-dev-training`으로 바꾼다. 테이블 이름과 달리 내부 키·인덱스 이름은 아래 계약을 그대로 유지한다. 다음은 오하이오(`us-east-2`)에서 사용자가 생성할 때의 안내이며 자원 생성 완료나 운영 설정 확정을 뜻하지 않는다.

1. AWS 화면 위 리전을 **미국 동부(오하이오)**로 선택한다. 검색창에서 `DynamoDB`를 열고 `Tables(테이블) → Create table(테이블 생성)`을 누른다.
2. 테이블 이름은 `arc-calc-dev-training`, 파티션 키는 `PK`/`String(문자열)`, 정렬 키는 `SK`/`String(문자열)`로 입력한다. `PK`/`SK`는 대문자다. 정렬 키를 비워 두지 않는다. `Tags(태그)`에는 `Project` → `arc-calc`, `Env` → `dev` 두 쌍을 추가한다.
3. `Table settings(테이블 설정) → Customize settings(설정 사용자 지정)`에서 용량 모드를 확인한다. 초기 Dev에는 처리량을 미리 산정하지 않는 **On-demand(온디맨드)**와 **Standard(표준)** 테이블 클래스를 권장한다. 이는 사용자가 선택할 과금 방식의 제안이며 무료 보장이나 확정된 비용 한도가 아니다. 암호화는 유지한다. [생성 절차](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/getting-started-step-1.html), [온디맨드 설명](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode.html)
4. `Create table`을 눌러 생성하고 상태가 `Active(활성)`가 될 때까지 기다린다. 이어서 테이블 이름을 누른다.
5. `Indexes(인덱스) → Create index(인덱스 생성)`에서 다음 **글로벌 보조 인덱스**를 추가한다. 인덱스는 처리할 작업을 찾아보는 검색용 목록이다.

| 인덱스 입력란 | 정확한 값 |
|---|---|
| Index name | `GSI1` — 마지막 문자는 숫자 1 |
| Partition key | `GSI1PK` / `String(문자열)` |
| Sort key | `GSI1SK` / **`Number(숫자)`** |
| Attribute projections | **`All(전체)`** |

자동 제안된 인덱스 이름 대신 정확히 `GSI1`을 사용한다. 생성 후 인덱스 상태도 `Active`인지 확인한다. [AWS 인덱스 설명](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html)

6. 같은 테이블에서 `Exports and streams(내보내기 및 스트림)` 탭을 연다. **DynamoDB stream details** 영역의 `Turn on(켜기)`을 누르고 **Key attributes only(키 속성만)**를 선택해 활성화한다. 이는 `KEYS_ONLY`다. 위 인덱스의 `All`과 서로 다른 설정이다. Kinesis 스트림을 새로 만드는 단계가 아니다. 현재 Relay는 이 이벤트의 PK/SK와 순서 번호만 읽는다. [AWS Streams 화면 안내](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Streams.html)
7. `Time to live(TTL)` 자동삭제는 비활성으로 유지한다. 사용자 기록을 시험 삼아 수동 입력할 필요는 없다. 완료 기준은 **테이블 Active, GSI1 Active, DynamoDB Streams 켜짐/키 속성만**이다.

여기까지는 DB 준비다. API/Worker/Relay 권한과 Stream 트리거, 예약, Relay 진행 행은 이후 단계에서 연결하며 이 작업만으로 서버나 계산이 실행되지는 않는다. 원래 잘못 만든 테이블·인덱스나 기존 자료를 확인 없이 삭제하여 맞추지 않는다.

<a id="beginner-new-s3"></a>
### DynamoDB 다음: 비공개 S3 버킷 만들기

S3 버킷은 실제 훈련 파일·계산 결과·차트 등을 보관하는 파일 보관함이다. 먼저 위 DynamoDB 절차에서 **테이블 Active, GSI1 Active, DynamoDB Streams 켜짐/KEYS_ONLY**까지 완료했는지 확인한다. 테이블 생성까지만 했다면 위 5~7번을 먼저 수행한다. 이미 완료한 설정을 다시 만들지 않는다.

다음은 새 Dev 버킷에 대한 추천 생성 절차다. 버킷이 실제 만들어졌거나 암호화·버전 관리 운영 방침이 승인된 것으로 기록하지 않는다. 조직에서 별도 설정을 요구하면 그 내용을 확인하고 안내와 대조한다.

1. 화면 오른쪽 위에서 `arc-dev-operator-role`로 작업 중인지 확인한다. 검색창에 `S3`를 입력해 서비스를 연다.
2. `General purpose buckets(범용 버킷) → Create bucket(버킷 만들기)`을 누른다. 생성 화면의 AWS 리전을 **미국 동부(오하이오), `us-east-2`**로 확인한다. 버킷 종류 선택란이 있으면 **General purpose(범용)**를 선택한다.
3. 버킷 이름에 `arc-calc-dev-storage-<ACCOUNT_ID>`를 넣는다. `<ACCOUNT_ID>`는 실제 12자리 계정 번호로 바꾼다. 이 추천 이름은 **Shared global namespace(공유 글로벌 네임스페이스)** 방식이다. 네임스페이스 선택란이 보이면 이 방식을 선택한다. 사용 중인 이름이라는 오류가 나면 실제 버킷 존재·소유 여부를 확인하고, 기존 버킷을 삭제하거나 이름이 다른 버킷을 임의 연결하지 않는다. 다른 이름을 선택하면 이후 설정 세 장에도 실제 이름을 반영한다. [AWS 생성 안내](https://docs.aws.amazon.com/AmazonS3/latest/userguide/create-bucket-overview.html), [이름 공간 안내](https://docs.aws.amazon.com/AmazonS3/latest/userguide/gpbucketnamespaces.html)
4. 기존 버킷의 설정 복사는 사용하지 않고 아래 값을 확인한다.

| 생성 화면의 항목 | 이번 새 Dev 버킷의 추천값 |
|---|---|
| Object Ownership(객체 소유권) | **ACLs disabled(ACL 비활성화)** / Bucket owner enforced(버킷 소유자 적용) |
| Block Public Access(퍼블릭 액세스 차단) | **Block all public access(모든 퍼블릭 액세스 차단)** 선택, 하위 4개 모두 켜짐 |
| Bucket Versioning(버킷 버전 관리) | 최초 Dev 구성은 기본값 **Disable(비활성화)** 유지 권고. 자동 삭제 설정이 아니며 과거 덮어쓰기/삭제 복구 기능도 제공하지 않음 |
| Default encryption(기본 암호화) | **Amazon S3 managed keys(SSE-S3)** 사용 권고 |
| Advanced settings → Object Lock(객체 잠금) | 기본값 **Disable(비활성화)** 유지 권고 |

현재 코드는 ACL·KMS 키·VersionId 지정을 요구하지 않는다. ACL 비활성화와 비공개 저장을 유지한다. 버전 관리는 과거 파일 버전을 복구하는 별도 기능이며 활성화하면 각 버전의 보관 비용이 발생한다. 보관기간 결정과 별개로, 이를 원하면 실제 파일 업로드 전에 선택한다. [객체 소유권](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-ownership-new-bucket.html), [버전 관리](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html), [기본 암호화](https://docs.aws.amazon.com/AmazonS3/latest/userguide/default-bucket-encryption.html)

5. `Tags(태그)`에서 키 `Project` / 값 `arc-calc`, 키 `Env` / 값 `dev` 두 쌍을 추가한다. 생성 화면에 태그 입력란이 없으면 생성 후 버킷의 `Properties(속성) → Tags(태그)`에서 추가한다.
6. `Create bucket(버킷 만들기)`을 누른다. 생성된 버킷 이름을 눌러 **실제 리전, 퍼블릭 액세스 차단, ACL 비활성화, 기본 암호화, 태그**가 위 내용과 맞는지 확인한다.
7. 빈 버킷이면 정상이다. 수동 폴더·시험 파일·공개 버킷 정책·정적 웹사이트 호스팅·Lifecycle 자동 삭제 규칙을 추가하지 않는다. 저장 경로는 이후 API/Worker의 같은 `storage.directory`/`storage.stage`로 지정하며 프로그램이 파일을 저장할 때 경로가 생긴다. 브라우저 CORS가 필요한 경우에도 실제 앱 origin을 확인해 별도로 설정한다.

완료 후에는 **실제 버킷 이름, 버킷 리전, 모든 퍼블릭 액세스 차단 켜짐 여부**를 확인한다. 파일 접근 권한은 다음 Lambda 실행 역할 단계에서 연결한다. 버킷 생성만으로 업로드/조회가 검증된 것은 아니다. 그 다음 준비 대상은 SQS 작업 큐와 실패 큐다.

<a id="beginner-new-sqs"></a>
### S3 다음: SQS 작업 큐와 실패 큐 만들기

작업 큐는 계산할 작업 번호를 전달하는 대기줄이고, 실패 큐(DLQ)는 반복해서 처리하지 못한 알림을 따로 모으는 곳이다. 현재 코드는 같은 계정·리전의 **Standard(표준)** 큐만 지원한다. FIFO나 기존 HSTM 운영 큐를 선택하지 않는다. 다음은 **송신·Lambda 트리거가 연결되지 않은 새 빈 큐 두 개**를 준비하는 절차이며 실제 작업 접수는 이후 설정 검증 뒤 시작한다.

1. `arc-dev-operator-role`과 **미국 동부(오하이오), `us-east-2`**를 확인한다. AWS 검색창에서 `SQS` → `Queues(대기열)` → `Create queue(대기열 생성)`를 연다.
2. **실패 큐부터** 만든다. 종류는 `Standard`, 이름은 `arc-calc-dev-calculation-dlq`다. 아래 값은 최초 Dev 구성을 위한 추천이며 확인된 실제 설정이나 확정 운영 정책으로 기록하지 않는다.

| 실패 큐 생성 항목 | 값 |
|---|---|
| Visibility timeout(표시 제한 시간) | AWS 기본값 `30초` 유지 — 이 단계에는 소비자 연결 없음 |
| Message retention period(메시지 보존 기간) | `14일` 권장 |
| Delivery delay(전송 지연) | 기본값 `0초` 유지 |
| Receive message wait time(수신 메시지 대기 시간) | 기본값 `0초` 유지 |
| Maximum message size(최대 메시지 크기) | 화면 기본값 유지. 큐에는 실제 훈련 파일을 넣지 않음 |
| Encryption(암호화) | 활성화, **SQS owned encryption keys(SSE-SQS)** 권장 |
| Access policy(액세스 정책) | `Basic(기본)` → 송신·수신 모두 `Only the queue owner(대기열 소유자만)` 유지 |
| Dead-letter queue(배달 못한 편지 대기열) | 비활성화 — 이 큐 자체가 실패 보관함 |
| Tags(태그) | `Project=arc-calc`, `Env=dev` |

처음에는 `Redrive allow policy`를 기본값으로 두고, 아래 5번에서 이 ARC 작업 큐만 사용하도록 제한한다. 위 `Access policy`와 서로 다른 항목이다. [AWS 표준 큐 생성 안내](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/creating-sqs-standard-queues.html)

3. `Create queue`를 눌러 실패 큐를 생성한다. 다시 대기열 목록으로 돌아가 `Create queue`를 누른다.
4. **작업 큐**를 만든다. 종류 `Standard`, 이름 `arc-calc-dev-calculation-jobs`. 위와 같은 암호화·액세스 정책·태그와 나머지 기본값을 사용하되 다음은 다르게 지정한다.

| 작업 큐 생성 항목 | 값 |
|---|---|
| Message retention period | 기본값 `4일` 유지 권고 |
| Dead-letter queue | **활성화** |
| 실패 대기열 선택 | 방금 만든 `arc-calc-dev-calculation-dlq` |
| Maximum receives(최대 수신 횟수) | 최초 Dev 권장값 **`5`** |

`5`는 코드의 필수 숫자가 아니라 AWS의 최소 5회 권고를 따른 초기 제안이다. SQS 한 메시지의 수신 횟수 기준이며 프로그램의 전체 재시도 횟수나 ARC 제출 횟수를 정하지 않는다. 작업 큐의 `Create queue`를 누른다. [Lambda/SQS 권고](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)

5. 작업 큐 상세의 **ARN**을 복사한다. 실패 큐 `arc-calc-dev-calculation-dlq` → `Edit(편집)` → `Redrive allow policy(리드라이브 허용 정책)` → 활성화 → `By queue(대기열별)`를 선택하고, 방금 복사한 **작업 큐 ARN 하나만** 추가해 저장한다. 실패 큐 자신의 ARN이나 Queue URL을 넣지 않는다. 이 설정은 어떤 작업 큐가 이 실패 큐를 사용할지 정하며 메시지를 재전송하는 버튼과 다르다. [DLQ 정책 안내](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
6. 작업 큐의 실패 대상이 위 DLQ이고 최대 수신 횟수가 선택한 값인지, 두 큐가 같은 계정·오하이오의 Standard인지 확인한다. 각 상세 화면의 **Queue URL**과 **ARN**을 기록한다. 브라우저 주소창 URL과 다르며 설정의 `relay.queue_url`에는 작업 큐의 Queue URL을 사용한다.

이 단계의 `30초`는 AWS의 기본 표시 제한 시간일 뿐, 계산용 Worker에 적합하다고 승인한 값이 아니다. **Lambda 트리거·Relay 전송·예약을 연결하기 전에 실제 Worker timeout의 6배 + batch window 이상으로 작업 큐 visibility를 설정하고 4절의 묶음 검사를 통과**해야 한다. 시험 메시지를 수동으로 보내거나 콘솔에서 실제 메시지를 수신하지 않는다. 메시지 본문은 애플리케이션이 실제 DB 작업과 맞는 `job_id`로 만든다.

SQS 메시지는 영구 보관되지 않는다. 위 `4일`/`14일`은 작업 알림에 대한 초기 제안이며 DynamoDB 진도·결과·운용 로그나 S3 원본 파일의 삭제 정책이 아니다. Standard 큐의 DLQ 만료는 원래 큐에 들어온 시각을 기준으로 하므로 실패 큐 이동 후 새로 14일이 시작되지 않는다. Relay의 미완료 Job 재확인과 장애 알림까지 실제 연결·시험한 뒤 운영한다. [SQS 보존 기간](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)

<a id="beginner-lambda-roles"></a>
### SQS 다음: Lambda 실행 역할 세 개 만들기

사람이 사용하는 `arc-dev-operator-role`과 별도로, API·Worker·Relay가 필요한 AWS 자원에 접근하는 실행 역할을 각각 만든다. 관리자 역할이나 CI/CD 역할을 Lambda 실행 역할로 사용하지 않는다. 현재 단계는 역할·권한 준비이며 Lambda 함수·트리거 생성이나 실제 계산 실행은 아니다.

| 역할 이름 | 연결할 함수 이름 | 인라인 정책 이름 | 준비할 권한 파일 |
|---|---|---|---|
| `arc-calc-dev-api-role` | `arc-calc-dev-api` | `arc-calc-dev-api-permissions` | `api-permissions.json` |
| `arc-calc-dev-worker-role` | `arc-calc-dev-worker` | `arc-calc-dev-worker-permissions` | `worker-permissions.json` |
| `arc-calc-dev-relay-role` | `arc-calc-dev-relay` | `arc-calc-dev-relay-permissions` | `relay-permissions.json` |

권한 파일은 실제 계정·리전·테이블·버킷·작업 큐를 대조해 `var/deployment/`의 별도 디렉터리에 준비한다. 운영 식별자별 JSON은 Git/배포 ZIP에 넣지 않는다. 2026-09-23 안내용 초안은 이전 생성 안내의 DB/버킷 이름과 사용자가 제공한 작업 큐 URL을 사용한다. S3 버킷 이름이 안내와 다르면 먼저 초안을 수정한다. 초안의 내부 `environment`는 **`dev`를 전제**하므로 이후 세 runtime JSON도 이 값을 사용하거나, 다른 값을 선택할 경우 Relay 진행 PK 권한을 다시 계산한다. 오프라인 JSON/범위 점검과 코드·AWS 문서 교차 검토는 실제 IAM 정책 평가·Lambda 접근 성공과 구별한다.

다음은 API 역할 하나를 만드는 순서이며, 끝나면 표에 따라 Worker와 Relay에도 반복한다.

1. 작업 역할로 로그인한 AWS 콘솔의 검색창에서 `IAM`을 연다. IAM 화면의 리전이 글로벌로 표시되어도 정상이다. 왼쪽 `Roles(역할)` → `Create role(역할 생성)`을 누른다.
2. `Trusted entity type(신뢰할 수 있는 엔터티 유형)`은 **AWS service**, `Service or use case(서비스 또는 사용 사례)`는 **Lambda**를 선택하고 다음으로 이동한다.
3. 권한 정책 선택 화면에서는 체크된 정책 없이 다음으로 이동한다. 아래 권한 파일에 해당 함수의 CloudWatch 로그 권한도 포함되어 있으므로 광범위 서비스 관리 정책을 추가하지 않는다.
4. 이름 `arc-calc-dev-api-role`, 태그 `Project=arc-calc` / `Env=dev`를 넣고 역할을 생성한다. 태그 입력란이 없는 화면이면 생성 뒤 `Tags(태그)` 탭에서 추가한다. 같은 이름의 역할이 이미 있다면 자동 삭제·재생성하지 않고 용도와 기존 정책을 확인한다.
5. 생성한 역할 → `Permissions(권한)` → `Add permissions(권한 추가)` → `Create inline policy(인라인 정책 생성)`를 누른다.
6. 편집기를 **JSON**으로 바꾸고, 준비된 `api-permissions.json`의 `{`부터 마지막 `}`까지 전체 내용을 붙여 넣는다. 파일 이름이나 경로를 입력하는 것이 아니다. 정책 이름은 `arc-calc-dev-api-permissions`로 저장한다. 화면에서 표시하는 오류는 저장 전에 해결하며 다른 역할의 파일을 붙이지 않는다.
7. `Trust relationships(신뢰 관계)`에서 신뢰 서비스가 **`lambda.amazonaws.com`**, 허용 작업이 **`sts:AssumeRole`**인지 확인한다. 엔터티를 Lambda로 선택했다면 생성 과정에서 이 신뢰 관계가 설정된다. 권한 JSON은 신뢰 정책 편집란에 붙이지 않는다.
8. 표의 Worker·Relay 역할도 같은 순서로 각각 맞는 파일과 정책 이름을 사용한다. 완료 기준은 **역할 세 개, 각각 해당 인라인 정책, Lambda 신뢰 관계, 태그 두 개**다.

[AWS 서비스 역할 생성](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-service.html), [인라인 권한 추가](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_manage-attach-detach.html), [Lambda 실행 역할](https://docs.aws.amazon.com/lambda/latest/dg/lambda-intro-execution-role.html)

권한 파일 작성·검토 시 다음 경계를 유지한다.

- DB는 해당 Dev 테이블의 필요한 PK 계열에 `GetItem`·`PutItem`·필요한 `ConditionCheckItem`을 허용한다. DynamoDB 트랜잭션 API 이름인 `TransactGetItems`/`TransactWriteItems`를 IAM Action으로 쓰지 않는다. 한 트랜잭션이 여러 PK 계열을 함께 사용하는 점을 반영해 `ForAllValues` 조건의 허용 목록을 구성한다. Worker는 Dummy의 로컬 제출 제외 기록인 `SUBMISSION#*`도 저장해야 하며 이것은 실제 외부 전송이 아니다.
- API/Worker는 전용 S3 버킷의 `GetObject`·`PutObject`와 **버킷 자체의 `ListBucket`**이 필요하다. 현재 코드는 새 파일을 쓰기 전 존재 여부를 GET으로 확인한다. `ListBucket`을 빼면 없는 파일도 404 대신 403이 되어 새 과정 스냅샷 생성이 실패할 수 있다. 객체 prefix는 실제 저장 설정과 맞추고 확인되지 않은 경로를 임의로 가정하지 않는다. [AWS GetObject 권한](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)
- Worker만 작업 큐의 수신·삭제·속성 조회, Relay만 그 큐의 전송을 허용한다. 실패 큐를 읽거나 지우는 권한은 이 실행 역할들에 필요하지 않다. Relay에는 S3 권한이 없다.
- Relay의 `GSI1` 조회는 `DUE#JOB`/`DUE#OUTBOX`, 진행 행은 선택한 환경의 정확한 `RELAY_SCAN#<SHA-256>` PK로 제한한다. 진행 행 권한을 API/Worker에 복사하지 않는다. Relay만 해당 테이블 Stream 읽기 권한을 받는다. `ListStreams`는 리소스 수준 제어가 없어 `Resource="*"`를 사용하되 선택한 리전으로 제한한다.
- 각 함수는 자기 `/aws/lambda/<함수 이름>` 로그 그룹 생성과 그 아래 스트림 생성·로그 쓰기만 허용한다. Lambda 함수 이름이나 로그 그룹을 바꾸면 정책도 대조한다. SSE-S3/SSE-SQS 구성에는 별도 KMS 권한을 추가하지 않는다.

세 역할을 만든 다음에는 Lambda 함수 세 개에 각각 연결한다. 이벤트 소스 트리거 활성화, 함수 시간·SQS visibility, runtime 설정 세 장, Relay 진행 행 초기화는 후속 단계에서 함께 검증한다.

<a id="beginner-lambda-functions"></a>
### 실행 역할 다음: Lambda 함수 생성과 ZIP 업로드

이번 단계에서는 새 함수 세 개를 생성하고 같은 검증 ZIP을 올린 뒤 실행 시작점(Handler)을 지정한다. 함수별 실행 역할과 로그 그룹 이름은 앞서 준비한 정책과 일치해야 한다.

| 함수 이름 | 기존 실행 역할 | Handler |
|---|---|---|
| `arc-calc-dev-api` | `arc-calc-dev-api-role` | `lambda_handler.run` |
| `arc-calc-dev-worker` | `arc-calc-dev-worker-role` | `mock_journey.worker.run` |
| `arc-calc-dev-relay` | `arc-calc-dev-relay-role` | `mock_journey.dispatch.run` |

1. AWS 콘솔에서 작업 역할과 **오하이오 `us-east-2`**를 확인한다. 검색창에서 `Lambda` → `Functions(함수)` → `Create function(함수 생성)`을 누른다.
2. `Author from scratch(새로 작성)`를 선택한다. 먼저 함수 이름 `arc-calc-dev-api`, Runtime **Python 3.12**, Architecture **x86_64**로 지정한다. Python 최신 버전 표시를 그대로 선택하는 것이 아니라 이번 빌드와 검증 버전을 맞춘다.
3. 실행 역할/권한 영역을 펼쳐 `Use an existing role(기존 역할 사용)` 또는 해당 화면의 `Use another role`에서 **`arc-calc-dev-api-role`**을 선택한다. 역할 관리 기능 때문에 생성 시 선택할 수 없는 화면이라면 생성 후 `Configuration → Permissions → Execution role → Edit`에서 기존 역할을 지정한다. 새 기본 역할을 계속 만들거나 사람의 Operator/CI 역할을 선택하지 않는다.
4. 추가 구성의 함수 URL은 활성화하지 않고 VPC는 연결하지 않는다. 태그를 넣을 수 있으면 `Project=arc-calc` / `Env=dev`를 입력한다. `Create function`을 누르고 생성 완료를 기다린다. 생성 뒤에도 `Configuration → Permissions`의 실행 역할 이름을 확인한다. [AWS 함수 생성](https://docs.aws.amazon.com/lambda/latest/dg/getting-started.html), [실행 역할 지정](https://docs.aws.amazon.com/lambda/latest/dg/lambda-intro-execution-role.html)
5. 함수의 `Code(코드)` 탭 → `Upload from(에서 업로드)` → `.zip file` → `Upload(업로드)`에서 검증된 **`mock-lambda.zip`**을 선택하고 저장한다. 저장소 폴더를 새로 압축하거나 IAM JSON/manifest를 대신 올리지 않는다. 같은 ZIP을 세 함수에 사용한다.
6. 2026-09-23 다시 확인한 파일은 `var/deployment/dev-readiness-20260922-dlqf4aqe/mock-lambda.zip`이며 13,837,535 bytes다. Mac 파일 선택창에서 찾기 어렵다면 `⌘⇧G`를 누르고 해당 폴더의 절대 경로를 입력한 뒤 ZIP을 선택한다. ZIP 업로드 성공 표시를 확인한다.
7. `Code` 탭 아래 `Runtime settings(런타임 설정)` → `Edit(편집)`에서 Handler를 **`lambda_handler.run`**으로 바꾸고 저장한다. 확장자 `.py`나 괄호는 붙이지 않는다. [ZIP 업로드와 Handler 설정](https://docs.aws.amazon.com/lambda/latest/dg/configuration-function-zip.html)
8. 생성 때 태그를 넣지 못했다면 `Configuration(구성) → Tags(태그) → Edit`에서 두 태그를 추가한다. 역할의 태그가 함수에 자동 복사된 것으로 가정하지 않는다.
9. Worker·Relay 함수도 위 표의 이름·역할·Handler로 같은 절차를 반복한다. 세 함수 모두 Python 3.12/x86_64와 같은 ZIP을 사용한다.

완료 기준은 **함수 세 개 생성, 각각 맞는 기존 실행 역할, 같은 ZIP 업로드 성공, 세 Handler 일치, 태그 두 개**다. 아직 runtime JSON·복구 키·시간/메모리 설정과 Relay 진행 행을 준비하지 않았으므로 테스트 이벤트 실행·SQS/Stream 트리거·예약은 연결하지 않는다. 화면의 기본 timeout/메모리는 계산 운영값으로 승인한 것이 아니다. 다음 단계에서 역할별 서버 설정과 선택한 실행시간·용량을 함께 검사하고 SQS visibility를 맞춘다. 코드 업로드만으로 `/api/v2` 서버 공개·실제 AWS 인수 성공을 뜻하지 않는다.

<a id="beginner-lambda-configuration"></a>
### ZIP 업로드 다음: 메모리·시간·환경 변수

2026-09-23 사용자가 세 Lambda 생성·ZIP 업로드·Handler 설정 완료를 보고했고 D95의 초기 용량/시간/업로드안을 선택했다. 아래 파일은 `150612770165` / `us-east-2` / `environment=dev`, 테이블 `arc-calc-dev-training`, 버킷 `arc-calc-dev-storage-150612770165`, 사용자가 제공한 작업 큐 URL을 연결한다. 테이블·버킷 이름은 앞선 생성 안내를 기준으로 작성했으므로 콘솔에 만든 실제 이름과 대조한다. 다르면 현재 파일을 그대로 적용하지 않고 설정과 IAM 정책을 함께 수정·재검사한다.

붙여넣기용 파일은 Git에서 제외하는 `var/deployment/dev-runtime-20260923-0octlgf9/`에 준비했다. JSON은 한 줄이다. **파일 이름이 아니라 파일 내용 전체**를 해당 환경 변수의 값 칸에 복사한다. 이번 코드/ZIP 변경은 없으므로 ZIP을 다시 올릴 필요는 없다.

1. 오하이오 Lambda 목록에서 각 함수를 열고 `Configuration(구성) → General configuration(일반 구성) → Edit(편집)`을 누른다. 아래 메모리와 제한 시간을 입력한 뒤 저장하고 성공 표시를 확인한다. [메모리 화면](https://docs.aws.amazon.com/lambda/latest/dg/configuration-memory.html), [제한 시간 화면](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html)

| 함수 | 메모리 | 제한 시간 입력 |
|---|---|---|
| `arc-calc-dev-api` | 512MB | 0분 30초 |
| `arc-calc-dev-worker` | 1024MB | 2분 0초 |
| `arc-calc-dev-relay` | 256MB | 1분 0초 |

2. SQS에서 **`arc-calc-dev-calculation-jobs`**를 열어 `Edit(편집) → Visibility timeout(표시 제한 시간)`을 **720초(12분)**로 저장한다. 계산 중인 메시지를 다른 Worker가 너무 일찍 다시 받지 않게 하는 시간이다. Worker 120초와 이후 batch window 0초에 대한 `6 × 120 + 0` 기준이다. 최대 수신 횟수 5와 기존 DLQ 연결은 유지한다. [AWS SQS 시간 설정 근거](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)
3. Lambda의 `Configuration → Environment variables(환경 변수) → Edit → Add environment variable(환경 변수 추가)`에서 아래 공통 세 줄을 **각 함수마다** 입력한다. 키와 값은 별도 칸이다. `false`는 연결을 준비하는 동안 기능을 꺼 두는 값이며, 후속 활성화 단계에서 변경한다. AWS 예약 변수 `AWS_REGION`은 직접 추가하지 않는다. [환경 변수 화면](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html)

| 공통 키 | 값 |
|---|---|
| `ARC_MOCK_ENABLED` | `false` |
| `STAGE` | `dev` |
| `ARC_STORAGE_REGION` | `us-east-2` |

4. 각 함수에 `ARC_JOURNEY_CONFIG`를 한 줄 더 추가한다. API에는 `api-runtime.json`, Worker에는 `worker-runtime.json`, Relay에는 `relay-runtime.json`의 내용 전체를 넣는다. 중괄호를 포함하되 바깥에 따옴표를 새로 붙이지 않는다. 다른 역할의 JSON을 복사하지 않는다.
5. **API 함수에만** `ARC_MOCK_RESUME_KEY_VERSION`의 값 `v1`, `ARC_MOCK_RESUME_KEYS`의 값으로 같은 폴더 `api-resume-keys.txt` 내용 전체를 추가한다. 새 32bytes 보안난수를 생성한 비공개 파일이며 기존 키를 덮어쓰지 않았다. AWS 비밀번호/Access Key가 아니라 훈련 재개 증표를 확인하는 키다. 내용이나 전체 환경 변수 화면을 대화에 보내지 않는다. Worker·Relay에는 넣지 않는다. 이후 재배포 때 같은 키를 보존한다.
6. API는 6개, Worker와 Relay는 각각 4개의 환경 변수를 저장한다. 이 개수는 새 함수에 이번 목록만 넣은 경우이며 다른 기존 필수 변수를 삭제하라는 뜻이 아니다. 세 함수의 저장 성공을 확인한다. 완료 뒤 Relay 최초 진행 행, Worker/Relay 전달 연결, API Gateway와 활성화/호출 시험으로 이어간다. 이 단계에서는 기능을 `false`로 두므로 로그인 성공 시험은 아직 하지 않는다.

설정의 기술 한도는 API 본문 문자열 4MiB, CPR+AED 원본 합계 2MiB, 저장 객체 8MiB다. 본문은 multipart/base64 때문에 원본보다 커지므로 서로 다른 한도다. 결과/차트 크기와 실제 앱 전체 요청이 AWS 전송 한도 안에 드는지는 후속 실측 대상이다. 초기 과정 방어 한도는 항목64·배정32·bundle256KiB·제어 본문16KiB·보고 구간128·병합 구간512·시작당 보고4096·거래 동작20·충돌 재시도2다. 이는 교육 완료 조건이나 실제 ARC 계약을 새로 정한 것이 아니다.

API/Worker SDK는 연결1초·읽기2초·총2회, Relay는 연결1초·읽기1초·총1회로 구별한다. DB 충돌 재시도2, Worker lease90초·갱신 간격20초·갱신 제한30초, Relay lease90초를 사용한다. 로그는 buffer128개/64KiB, 종료 대기2초·반환 여유1초이며 별도 SDK 연결0.25초·읽기0.75초·총1회를 사용한다. 로그 장애가 훈련을 막지 않는 기존 best-effort 경계를 유지하며 로그 전량 전달을 보장하지 않는다. 수치 전체는 역할별 JSON과 비밀 없는 `settings-review.json`에 남겼다.

실제 생성 파일의 개별/묶음 검사, 업로드한 ZIP 코드의 네트워크 차단·SDK 대역 조립, API 키 형식, 환경 변수 크기(API1338/Worker1344/Relay741 bytes), Relay 진행 PK와 준비한 IAM 정책 일치를 확인했다. 후속 단계용 `relay-progress-init.json`도 조건부 최초 생성 요청으로 준비했으며 AWS에는 적용하지 않았다. 오프라인 설정 통과는 실제 권한·자원·Lambda 실행 성공을 뜻하지 않는다.

개인 외부 접근 보호는 공개된 Dummy 계정과 별개다. 이번 공유 Dummy Dev 예외는 0절·D94를 따른다. 개인 보호가 구성된 환경에서는 로그인 경로 보호와 개인 권한 회수를 시험한다. HTTPS, 요청별 소유권, 최소 권한과 Dev/Beta/Prod 상태·Queue·키·파일 분리를 유지한다. `environment` 이름만으로 같은 DB의 훈련 자료가 격리되지는 않는다.

차트는 소유권 검사 뒤 비공개 S3의 300초 서명 URL을 발급한다. 임시 자격증명이 먼저 만료되면 더 짧아질 수 있으며, 발급된 링크는 앱 로그아웃과 독립적으로 자체 유효기간 동안 읽힐 수 있다. 원본·metadata에 공개 URL을 발급하지 않는다. 만료·재발급은 실제 앱에서 확인한다.

이번 `/api/v2` Dev는 [앱 API 계약](APP_API.md)의 16개 동작을 모두 연결한다. 0절의 `/api/v2/{proxy+}` `ANY` proxy 또는 개별 경로 연결을 사용하고 로그인/조회/업로드/삭제를 함께 검증한다. 아래는 **기존 Mock 모드**용 연결표이며 이 표만 연결하면 새 API는 열리지 않는다.

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

<a id="beginner-relay-init-worker"></a>
### 환경 변수 다음: 최초 기록과 Worker 연결

2026-09-23 사용자가 메모리·시간·환경 변수 설정 완료를 보고했다. 이번 순서는 **Relay 최초 기록 한 개 생성과 SQS→Worker 연결**까지다. `ARC_MOCK_ENABLED=false`일 때 handler는 성공한 빈 실행이 아니라 오류를 반환하므로 트리거도 **Disabled**로 만든다. 환경 변수만 꺼 둔 채 트리거를 켜서 재시도/DLQ를 발생시키지 않는다.

두 파일을 `var/deployment/dev-runtime-20260923-0octlgf9/`에서 사용한다. `relay-progress-init.json`은 앞서 검사한 조건부 최초 생성 요청이고, `worker-trigger-disabled.json`은 작업 큐 ARN과 Worker ARN·배치1·대기0초·`ReportBatchItemFailures`·`Enabled=false`·태그를 지정한 새 연결 요청이다. SQS 필터는 넣지 않는다. 이 파일들은 환경 변수나 Lambda ZIP이 아니며 비밀 키 파일을 업로드할 필요가 없다.

1. AWS에서 작업 역할 `arc-dev-operator-role`과 오하이오를 선택한 뒤 검색창에서 **CloudShell**을 연다. CloudShell은 AWS 웹 화면 안에서 명령을 입력하는 창이며 로컬 Mac 터미널과 다르다. 일반 CloudShell을 사용하고 VPC 환경을 새로 만들지 않는다. 아래 명령을 한 덩어리씩 붙여넣고 Enter로 실행한다. [CloudShell 시작/파일 업로드](https://docs.aws.amazon.com/cloudshell/latest/userguide/getting-started.html)
2. 먼저 현재 실행 주체를 확인한다. `Account=150612770165`, Arn에 `assumed-role/arc-dev-operator-role/`이 있는지 확인한다. 다른 계정/역할이면 쓰기 명령을 진행하지 말고 콘솔의 역할 전환부터 맞춘다. Access Key를 만들거나 붙여넣지 않는다.

```sh
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output json --no-cli-pager
```

3. CloudShell의 `Actions(작업) → Upload file(파일 업로드)`에서 위 두 파일을 하나씩 선택해 업로드한다. Mac 선택창에서 `⌘⇧G`로 로컬 폴더를 찾을 수 있다. 업로드 파일은 CloudShell 홈 디렉터리에 놓인다. 아래 명령의 `$HOME`은 그 CloudShell 홈이며 Mac 경로로 바꾸지 않는다.
4. 아래 명령으로 시작 표식 한 개를 만든다. 성공하면 `arc-calc-dev-training`이 출력된다. `ConditionalCheckFailedException`은 이미 같은 키의 기록이 있다는 뜻이므로 기존 항목을 삭제/덮어쓰지 않고 확인한다. 파일 전체는 **PutItem 요청**이므로 DynamoDB 콘솔 항목 편집기에 그대로 붙여넣지 않는다. [조건부 PutItem](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_PutItem.html)

```sh
aws dynamodb put-item --region us-east-2 \
  --cli-input-json file://"$HOME/relay-progress-init.json" \
  --return-consumed-capacity TOTAL --query 'ConsumedCapacity.TableName' \
  --output text --no-cli-pager
```

5. Worker의 기존 연결을 먼저 조회한다. 새 함수에서 결과가 `[]`이면 다음 생성 명령을 진행한다. 이미 목록이 있으면 중복 생성하지 말고 대상/상태/부분 실패 설정을 확인한다.

```sh
aws lambda list-event-source-mappings --region us-east-2 \
  --function-name arc-calc-dev-worker \
  --event-source-arn arn:aws:sqs:us-east-2:150612770165:arc-calc-dev-calculation-jobs \
  --query 'EventSourceMappings[].{UUID:UUID,State:State,Source:EventSourceArn,BatchSize:BatchSize,PartialFailures:FunctionResponseTypes}' \
  --output json --no-cli-pager
```

6. SQS 작업 큐의 표시 제한 시간이 앞 단계의 **720초**인지 확인하고 아래 명령으로 비활성 연결을 만든다. 실패한 메시지를 성공 처리하지 않도록 `ReportBatchItemFailures`를 명시했다. 기존 실행 역할의 SQS 권한을 사용하며 관리형 광역 정책을 추가하지 않는다. [SQS 연결](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html), [부분 실패 처리](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html), [CLI 요청/태그](https://docs.aws.amazon.com/cli/latest/reference/lambda/create-event-source-mapping.html)

```sh
aws lambda create-event-source-mapping --region us-east-2 \
  --cli-input-json file://"$HOME/worker-trigger-disabled.json" \
  --query '{UUID:UUID,State:State}' --output json --no-cli-pager
```

7. 처음 `Creating`이면 잠시 기다린 뒤 5번 조회를 다시 실행한다. **State=Disabled**, Source 끝이 `arc-calc-dev-calculation-jobs`, BatchSize=1, PartialFailures에 `ReportBatchItemFailures`가 있으면 완료다. Lambda 콘솔의 Worker `Configuration → Triggers`에서도 SQS 연결과 비활성 상태를 볼 수 있다. 오류가 나면 다음 명령을 연속 실행하지 말고 오류를 확인한다. 환경 변수는 계속 `false`로 유지한다.

후속 단계는 DynamoDB Streams→Relay와 예약 재확인, API Gateway, 활성화 및 호출 시험이다. Stream은 실제 LatestStreamArn과 `TRIM_HORIZON`·부분 실패·비활성 시작을 사용하고, 필터는 `dynamodb.Keys.PK.S`의 `OUTBOX#` 접두사와 `SK.S=DISPATCH`로 제한한다. INSERT 검사는 코드가 담당한다. OPS 로그/Relay 진행 행을 제외하고 SQS 계산 DLQ와 Stream 실패 대상을 섞지 않는다. 예약은 `source=aws.events` 사건을 전달해야 하며, 후속 사용자 답변 D96으로 복구 점검 주기를 1분으로 선택했다. [DynamoDB 필터](https://docs.aws.amazon.com/lambda/latest/dg/with-ddb-filtering.html)

<a id="beginner-relay-stream"></a>
### Worker 연결 다음: DynamoDB Streams와 Relay 연결

사용자가 Worker 연결의 `Disabled`, 배치1, `ReportBatchItemFailures` 결과를 전달했다. 이제 새 계산 요청의 Outbox 기록을 Relay에 전달하는 연결을 준비한다. 같은 계정·작업 역할의 오하이오 CloudShell에서 진행하며 환경 변수와 연결은 계속 비활성으로 유지한다. [AWS DynamoDB 연결](https://docs.aws.amazon.com/lambda/latest/dg/services-dynamodb-eventsourcemapping.html)

1. 먼저 아래 명령으로 현재 테이블의 Stream 정보를 읽는다. `TableArn`은 `arn:aws:dynamodb:us-east-2:150612770165:table/arc-calc-dev-training`, `Enabled=true`, `View=KEYS_ONLY`여야 한다. `StreamArn`은 같은 테이블 ARN 뒤에 `/stream/날짜와시간`이 붙은 값이다. null/false/다른 값이면 연결을 만들지 않고 확인한다. 기존 Stream을 껐다 켜면 ARN이 바뀔 수 있으므로 임의 토글하지 않는다.

```sh
aws dynamodb describe-table --region us-east-2 \
  --table-name arc-calc-dev-training \
  --query 'Table.{TableArn:TableArn,Enabled:StreamSpecification.StreamEnabled,View:StreamSpecification.StreamViewType,StreamArn:LatestStreamArn}' \
  --output json --no-cli-pager
```

사용자가 제공한 2026-09-23 조회에서는 테이블 ARN이 위 값과 일치하고 `Enabled`/`View`/`StreamArn`이 모두 null이었다. 이처럼 이번 새 Dev 테이블에 Stream 설정이 없는 경우 다음 명령으로 **KEYS_ONLY 변경 알림만 활성화**한다. 기존 데이터/키/인덱스를 재생성하지 않는다. 이미 Stream이 켜져 있거나 다른 값인 환경에 이 명령을 반복 적용하지 않는다. [AWS Stream 활성화 명령](https://docs.aws.amazon.com/cli/latest/reference/dynamodb/update-table.html)

```sh
aws dynamodb update-table --region us-east-2 \
  --table-name arc-calc-dev-training \
  --stream-specification StreamEnabled=true,StreamViewType=KEYS_ONLY \
  --query 'TableDescription.{Status:TableStatus,Enabled:StreamSpecification.StreamEnabled,View:StreamSpecification.StreamViewType,StreamArn:LatestStreamArn}' \
  --output json --no-cli-pager
```

응답의 `Status=UPDATING`은 설정 변경 중이라는 뜻이다. 잠시 뒤 다음 **조회**만 반복하여 `Status=ACTIVE`, `Enabled=true`, `View=KEYS_ONLY`, null이 아닌 실제 Stream ARN을 확인한 다음 2번으로 진행한다. 활성화 명령 자체를 재실행하거나 껐다 켜지 않는다. 이 작업은 Stream 알림 준비이며 Relay 트리거 활성화와 별개다.

```sh
aws dynamodb describe-table --region us-east-2 \
  --table-name arc-calc-dev-training \
  --query 'Table.{Status:TableStatus,Enabled:StreamSpecification.StreamEnabled,View:StreamSpecification.StreamViewType,StreamArn:LatestStreamArn}' \
  --output json --no-cli-pager
```

2. CloudShell `Actions → Upload file`로 로컬 `var/deployment/dev-runtime-20260923-0octlgf9/relay-trigger-disabled.json` 한 개를 올린다. 이 파일에는 Relay 함수 ARN, `Enabled=false`, 배치1·대기0초·병렬1, `StartingPosition=TRIM_HORIZON`, 부분 실패, 태그와 키 필터가 들어 있다. 실제 Stream ARN은 파일에 추정해서 넣지 않았으며 생성 명령에서 조회해 전달한다.
3. 아래 명령으로 Relay의 기존 연결 전체를 조회한다. `[]`이면 다음 단계로 진행한다. 목록이 있으면 기존 Stream 연결을 먼저 확인하고 추가 생성하지 않는다. 최신 ARN으로만 필터링하지 않아 이전 Stream의 연결도 확인한다.

```sh
aws lambda list-event-source-mappings --region us-east-2 \
  --function-name arc-calc-dev-relay \
  --query 'EventSourceMappings[].{UUID:UUID,State:State,Source:EventSourceArn,BatchSize:BatchSize,PartialFailures:FunctionResponseTypes,Filter:FilterCriteria}' \
  --output json --no-cli-pager
```

4. 아래 블록 전체를 한 번에 붙여넣어 실행한다. 현재 Stream 주소를 임시 변수에 보관하고, 정확한 계정·리전·테이블의 Stream 주소일 때만 비활성 연결을 만든다. `$HOME`은 CloudShell 업로드 폴더다. 코드에서 계산 요청만 처리하도록 만든 필터와 부분 실패 설정을 함께 적용한다. [CLI 생성 설정](https://docs.aws.amazon.com/cli/latest/reference/lambda/create-event-source-mapping.html)

```sh
arc_relay_stream_arn=$(aws dynamodb describe-table --region us-east-2 \
  --table-name arc-calc-dev-training --query 'Table.LatestStreamArn' \
  --output text --no-cli-pager)
if [[ "$arc_relay_stream_arn" == arn:aws:dynamodb:us-east-2:150612770165:table/arc-calc-dev-training/stream/* ]]; then
  aws lambda create-event-source-mapping --region us-east-2 \
    --cli-input-json file://"$HOME/relay-trigger-disabled.json" \
    --event-source-arn "$arc_relay_stream_arn" \
    --query '{UUID:UUID,State:State}' --output json --no-cli-pager
else
  printf '%s\n' 'STREAM_CHECK_REQUIRED: no mapping was created.'
fi
```

5. `Creating`이면 잠시 기다렸다가 3번 조회를 반복한다. `Disabled`, 배치1, 부분 실패 `ReportBatchItemFailures`, Source가 1번의 실제 Stream ARN, Filter에 `OUTBOX#`/`DISPATCH`가 있으면 준비 완료다. `Filter`는 JSON 내부 문자열이므로 따옴표 앞의 역슬래시는 정상 표기다. 필터는 `dynamodb.Keys`만 사용하며 코드가 INSERT만 실제 전달한다. 기존 Relay IAM에 승인 테이블 Stream 권한이 있으므로 새 광역 관리형 정책이나 계산 DLQ 대상을 추가하지 않는다. 실제 필터 매칭·전달/중복/재시도 동작은 활성화 후 AWS 인수 대상이다.

다음은 D96의 1분 예약 점검 연결이며, 모든 전달 연결과 API Gateway 준비 후 환경 변수/트리거를 순서대로 활성화한다. 이 단계의 Disabled 성공은 계산 실행이나 외부 API 공개 성공을 의미하지 않는다.

<a id="beginner-relay-scheduler"></a>
### Relay 연결 다음: 1분 복구 점검용 Scheduler 준비

신규 예약에는 AWS가 권장하는 **EventBridge Scheduler**를 사용한다. 사용자 D96의 1분 주기로 Relay를 깨워 DB에 남은 누락 전달·미완료 계산을 다시 확인한다. 기존 API/Worker/Relay 실행 역할과 구별하여 Scheduler용 호출 역할 하나를 추가한다. 권한은 Relay 함수 한 개의 `lambda:InvokeFunction`뿐이다. [AWS Scheduler 권고](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-run-lambda-schedule.html), [Lambda 예약 호출](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html)

| 자원 | 새 이름 |
|---|---|
| Scheduler 그룹 | `arc-calc-dev-schedules` |
| Scheduler 호출 역할 | `arc-calc-dev-scheduler-role` |
| 역할 인라인 정책 | `arc-calc-dev-scheduler-invoke-relay` |
| 예약 이름 | `arc-calc-dev-relay-reconcile` |

같은 오하이오 CloudShell에서 진행한다. 먼저 `var/deployment/dev-runtime-20260923-0octlgf9/`의 `scheduler-trust.json`, `scheduler-invoke-relay.json`을 각각 `Actions → Upload file`로 업로드한다. 두 파일에 비밀값은 없다. 역할의 신뢰 주체는 `scheduler.amazonaws.com`, SourceAccount는 정확한 계정, SourceArn은 정확한 **schedule-group** ARN으로 제한한다. SourceArn을 개별 schedule ARN으로 바꾸지 않는다. [Scheduler 신뢰 정책](https://docs.aws.amazon.com/scheduler/latest/UserGuide/cross-service-confused-deputy-prevention.html)

1. 예약을 묶는 그룹을 만든다. 성공하면 `ScheduleGroupArn`이 출력된다. 생성 명령에서 이름 충돌이 나면 기존 자원을 삭제/수정하지 않고 확인한다.

```sh
aws scheduler create-schedule-group --region us-east-2 \
  --name arc-calc-dev-schedules \
  --tags Key=Project,Value=arc-calc Key=Env,Value=dev \
  --output json --no-cli-pager
```

2. Scheduler 전용 호출 역할을 만든다. 성공한 ARN은 `arn:aws:iam::150612770165:role/arc-calc-dev-scheduler-role`이다. `EntityAlreadyExists`가 나오면 기존 역할을 확인하고 아래 정책 쓰기를 이어 실행하지 않는다.

```sh
aws iam create-role --region us-east-2 \
  --role-name arc-calc-dev-scheduler-role \
  --assume-role-policy-document file://"$HOME/scheduler-trust.json" \
  --tags Key=Project,Value=arc-calc Key=Env,Value=dev \
  --query 'Role.Arn' --output text --no-cli-pager
```

3. 방금 새로 만든 역할에 Relay 호출 권한을 붙인다. 성공 시 별도 내용 없이 입력 줄로 돌아오는 것이 정상이다. Lambda의 기존 실행 역할을 이 역할로 교체하지 않는다.

```sh
aws iam put-role-policy --region us-east-2 \
  --role-name arc-calc-dev-scheduler-role \
  --policy-name arc-calc-dev-scheduler-invoke-relay \
  --policy-document file://"$HOME/scheduler-invoke-relay.json" \
  --no-cli-pager
```

권한 연결은 아래 조회에서 `lambda:InvokeFunction`과 정확한 Relay 함수 ARN만 있는지 확인한다.

```sh
aws iam get-role-policy --region us-east-2 \
  --role-name arc-calc-dev-scheduler-role \
  --policy-name arc-calc-dev-scheduler-invoke-relay \
  --query 'PolicyDocument.Statement' --output json --no-cli-pager
```

예약 본문은 `Input={"source":"aws.events"}`를 명시해야 현재 handler가 Stream이 아닌 재확인 경로를 실행한다. `rate(1 minute)`, FlexibleTimeWindow OFF, 최초 `DISABLED`를 사용한다. 사용자 D97에 따라 Scheduler 자체의 추가 전달 재시도는 `MaximumRetryAttempts=0`이다. 최대 사건 나이는 생략하고 실제 AWS 응답값을 확인하되 새 보관기간 결정으로 취급하지 않는다. 기존 계산 DLQ를 Scheduler에 재사용하지 않는다. Scheduler는 Lambda를 비동기 호출하므로 전달 접수와 실제 재확인 성공을 구별하며 Lambda 실행 오류 재시도는 별도 설정이다. 1분은 호출 주기이고 lease 경합·장애 상황에서 복구 완료 기한을 보장하지 않는다.

<a id="beginner-relay-schedule-create"></a>
### Scheduler 권한 다음: 예약 생성

사용자가 Scheduler 역할에 정확한 Relay 함수의 `lambda:InvokeFunction`이 연결된 조회 결과를 전달했고 D97의 재시도 방식도 선택했다. 새 파일 `var/deployment/dev-runtime-20260923-0octlgf9/relay-schedule-disabled.json`은 schema·IAM 대상/그룹·handler 입력 경로를 오프라인 검사했다. 이 파일을 같은 오하이오 CloudShell에서 사용한다.

1. `Actions → Upload file`에서 `relay-schedule-disabled.json`을 업로드한다. Mac 선택창의 `⌘⇧G`에서 위 폴더를 찾는다. 이 단계에서 기존 그룹이나 역할을 다시 생성하지 않는다.
2. 아래 명령으로 그룹 안의 같은 이름 접두사 예약을 조회한다. 결과가 `[]`이면 새 예약을 만든다. 기존 목록이 있으면 상태와 대상을 확인하고 덮어쓰거나 중복 생성하지 않는다.

```sh
aws scheduler list-schedules --region us-east-2 \
  --group-name arc-calc-dev-schedules \
  --name-prefix arc-calc-dev-relay-reconcile \
  --query 'Schedules[].{Name:Name,State:State}' --output json --no-cli-pager
```

3. 아래 명령으로 새 예약을 생성한다. 성공하면 `ScheduleArn`이 `arn:aws:scheduler:us-east-2:150612770165:schedule/arc-calc-dev-schedules/arc-calc-dev-relay-reconcile`로 나온다. 이는 아직 실행되지 않는 DISABLED 예약이다. 오류가 나면 기존 자원을 삭제하거나 UpdateSchedule로 우회하지 않고 원인을 확인한다. [예약 생성 명세](https://docs.aws.amazon.com/cli/latest/reference/scheduler/create-schedule.html)

```sh
aws scheduler create-schedule --region us-east-2 \
  --cli-input-json file://"$HOME/relay-schedule-disabled.json" \
  --output json --no-cli-pager
```

4. 아래 조회로 저장된 실제 설정을 확인한다. State=DISABLED, Rate=`rate(1 minute)`, Window=OFF, Retries=0, Function=정확한 Relay ARN, Role=정확한 Scheduler 역할 ARN, Input을 해석한 값이 `{"source":"aws.events"}`이면 준비 완료다. Input 문자열의 역슬래시 표시는 JSON 인코딩 결과이며 정상이다. [예약 조회 명세](https://docs.aws.amazon.com/cli/latest/reference/scheduler/get-schedule.html), [재시도 설정](https://docs.aws.amazon.com/scheduler/latest/APIReference/API_RetryPolicy.html)

```sh
aws scheduler get-schedule --region us-east-2 \
  --group-name arc-calc-dev-schedules --name arc-calc-dev-relay-reconcile \
  --query '{Name:Name,State:State,Rate:ScheduleExpression,Window:FlexibleTimeWindow.Mode,Retries:Target.RetryPolicy.MaximumRetryAttempts,Function:Target.Arn,Role:Target.RoleArn,Input:Target.Input}' \
  --output json --no-cli-pager
```

이 단계에서도 Scheduler·Worker/Stream 연결은 비활성, Lambda 환경 변수는 `false`를 유지한다. 다음은 API Gateway 연결 및 최초 행/GSI/로그·실제 호출을 포함한 활성화 전후 인수다. 스케줄 설정 저장 성공을 계산 성공이나 전체 AWS 배포 인수 통과로 보고하지 않는다.

<a id="beginner-rest-api-create"></a>
### 예약 다음: 새 REST API 생성

사용자가 전달한 예약 조회는 `DISABLED`, `rate(1 minute)`, Window=OFF, Retries=0, 정확한 Relay/Scheduler 역할 ARN, `{"source":"aws.events"}` 입력으로 준비값과 일치한다. 다음은 앱의 HTTPS 접속을 담당할 API Gateway다. 현재 코드는 REST API의 `httpMethod`/`path`와 다중 헤더·query 형식을 사용한다. **오하이오에 새 Regional REST API `arc-calc-dev-rest-api`를 만든다.** 아래는 API 생성까지만이며 Lambda 연결·stage 배포·호출량 설정은 후속 단계다.

1. 지금 사용 중인 오하이오 CloudShell에서 아래 전체 명령을 복사해 실행한다. `~ $`나 `>`는 입력하지 않는다. 결과가 `[]`이면 같은 이름의 REST API가 없는 것이다. 목록이 나오면 표시된 ID로 기존 설정을 먼저 확인하며 생성 명령을 반복하지 않는다. API 이름만으로 중복 생성이 방지되지는 않는다.

```sh
aws apigateway get-rest-apis --region us-east-2 \
  --query "items[?name=='arc-calc-dev-rest-api'].{Id:id,Name:name}" \
  --output json --no-cli-pager
```

2. 결과가 `[]`일 때 아래 명령을 한 번 실행한다. `multipart/form-data`는 앱이 훈련 파일을 업로드할 때 쓰는 형식이다. API 전체의 binary media type에 등록하고 후속 실제 업로드에서 원본 bytes 보존을 확인한다. 태그는 관리자 요청값이다. [REST API 생성 명세](https://docs.aws.amazon.com/cli/latest/reference/apigateway/create-rest-api.html), [바이너리 처리 안내](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-payload-encodings.html)

```sh
aws apigateway create-rest-api --region us-east-2 \
  --name arc-calc-dev-rest-api \
  --description 'ARC calculation dev course API' \
  --endpoint-configuration types=REGIONAL \
  --binary-media-types 'multipart/form-data' \
  --tags Project=arc-calc,Env=dev \
  --query '{Id:id,Name:name,Type:endpointConfiguration.types,Binary:binaryMediaTypes,Tags:tags}' \
  --output json --no-cli-pager
```

3. 결과의 `Id`는 AWS가 새로 붙여 준 API 식별 번호이며 비밀 키가 아니다. `Name=arc-calc-dev-rest-api`, `Type=[REGIONAL]`, `Binary=[multipart/form-data]`, `Project=arc-calc`/`Env=dev` 태그를 확인하고 ID를 다음 단계에 사용한다. 생성 결과를 잃었으면 1번 조회로 찾으며 같은 이름을 다시 생성하지 않는다.

후속 연결은 `ANY /api/v2/{proxy+}` → `arc-calc-dev-api`의 `AWS_PROXY`로 제한하고, 실제 API ID로 Lambda 호출 권한을 지정한다. 그다음 Dev stage의 호출량 한도를 확정하고 배포·로그인·파일 원본/경로 보존을 검증한다. 이 생성 단계에서는 세 Lambda의 기능 플래그와 Worker/Stream/Scheduler의 비활성 상태를 유지한다.

<a id="beginner-rest-api-connect"></a>
### 새 REST API 다음: API Lambda 연결

사용자가 생성 결과로 API ID `2ftxmdtrx1`, 이름 `arc-calc-dev-rest-api`, Regional·multipart·Project/Env 태그 일치를 전달했다. 아래 명령은 **이 새 API만** 대상으로 한다. `var/deployment/dev-runtime-20260923-0octlgf9/api-gateway-v2-integration.json`은 AWS가 읽는 배포 설정 파일이며 앱 요청·응답 계약은 계속 `APP_API.md` 한 곳에 유지한다. 이 단계는 경로와 호출 권한 준비이며 아직 stage 배포를 하지 않는다.

1. 같은 오하이오 CloudShell에서 현재 경로를 확인한다. 새 API에는 `/` 하나와 Methods의 null 또는 빈 객체만 있어야 한다. 다른 경로나 메서드가 있으면 아래 merge가 같은 메서드를 바꿀 수 있으므로 출력부터 확인한다. [기존 API merge 동작](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-import-api-update.html)

```sh
aws apigateway get-resources --region us-east-2 \
  --rest-api-id 2ftxmdtrx1 --embed methods \
  --query 'items[].{Path:path,Methods:resourceMethods}' \
  --output json --no-cli-pager
```

2. CloudShell의 `Actions → Upload file`에서 새 `api-gateway-v2-integration.json`을 업로드한다. Mac 선택창에서 `⌘⇧G`를 누르고 `/Users/mac/arc_calculator_api/var/deployment/dev-runtime-20260923-0octlgf9/` 폴더를 연다. 비밀 키 파일은 이 단계의 대상이 아니다.
3. 업로드한 파일을 이 API에 적용한다. `--mode merge`로 경로를 추가하며 `--fail-on-warnings`로 경고가 생기면 적용을 중단하도록 한다. 오류·경고가 있으면 다음 단계로 넘어가지 말고 실제 상태를 다시 조회한다. [PutRestApi 명세](https://docs.aws.amazon.com/cli/latest/reference/apigateway/put-rest-api.html)

```sh
aws apigateway put-rest-api --region us-east-2 \
  --rest-api-id 2ftxmdtrx1 \
  --mode merge --fail-on-warnings \
  --body fileb://"$HOME/api-gateway-v2-integration.json" \
  --query '{Id:id,Name:name,Binary:binaryMediaTypes,Warnings:warnings}' \
  --output json --no-cli-pager
```

4. Gateway가 API Lambda를 부를 수 있도록 아래 권한을 한 번 추가한다. 계정·API ID·`dev` stage·`/api/v2/` 경로로 제한한다. `Statement`가 출력되면 추가 결과를 확인한다. `ResourceConflictException`이면 같은 statement ID가 이미 있는 것이므로 다른 ID로 중복 생성하거나 기존 권한을 삭제하지 않고 `get-policy`로 비교한다. 이 권한은 콘솔의 `test-invoke-stage`에는 적용되지 않는다. 후속 시험은 실제 `dev` 주소로 수행한다. [API Gateway의 Lambda 호출 권한](https://docs.aws.amazon.com/lambda/latest/dg/services-apigateway.html)

```sh
aws lambda add-permission --region us-east-2 \
  --function-name arc-calc-dev-api \
  --statement-id arc-calc-dev-rest-api-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-account 150612770165 \
  --source-arn 'arn:aws:execute-api:us-east-2:150612770165:2ftxmdtrx1/dev/*/api/v2/*' \
  --output json --no-cli-pager
```

5. 저장된 메서드와 Lambda 연결을 조회한다. 아래 출력과 4번 권한 결과를 함께 대조한다.

```sh
aws apigateway get-resources --region us-east-2 \
  --rest-api-id 2ftxmdtrx1 --embed methods \
  --query "items[?path=='/api/v2/{proxy+}'].{Path:path,Method:resourceMethods.ANY}" \
  --output json --no-cli-pager
```

기대값은 Path=`/api/v2/{proxy+}`, Method.httpMethod=`ANY`, authorizationType=`NONE`, apiKeyRequired=false, methodIntegration.type=`AWS_PROXY`, methodIntegration.httpMethod=`POST`, uri가 정확한 `arc-calc-dev-api/invocations` 대상, timeoutInMillis=29000이다. 여기서 NONE은 별도 AWS IAM 로그인 없이 앱의 로그인 요청을 전달한다는 뜻이며, 로그인 외 Bearer 세션·소유권 검사는 기존 Lambda 코드가 담당한다. 파일의 메서드 응답200 항목은 가져오기용 설명이며 Lambda proxy가 실제 202/4xx/5xx와 본문을 반환한다.

요청/응답 변환 template·contentHandling·별도 Gateway 실행 Role·캐시·전체 CORS 허용은 추가하지 않는다. Gateway 대기는 기본 범위의29초이며 API Lambda30초와 별개다. 실제 앱30초 대기 기준이나 비동기 계산 완료 기한을 변경한 것이 아니다. 경로 끝 `/`, Authorization, 중복 query 거절, multipart 원본 보존은 실제 배포 후 확인한다. 세 Lambda 기능 플래그/Worker·Stream·Scheduler는 아직 비활성으로 유지하며 호출량 설정·연결 최종 확인 뒤 Dev stage를 배포한다.

<a id="beginner-preactivation-readback"></a>
### Gateway 연결 다음: 활성화 전 저장 설정 조회

사용자가 API `2ftxmdtrx1`의 merge 성공(Warnings=null), 정확한 API Lambda에 제한한 dev 호출 권한, `ANY /api/v2/{proxy+}`의 AWS_PROXY/POST/29000ms 조회 결과를 전달했다. 경로·권한 설정은 준비값과 일치한다. 아래는 아직 실제 값을 확인하지 않은 DB 색인·Relay 최초 행·큐 대기시간·Lambda 설정을 읽는 명령이다. 자원을 다시 만들거나 데이터를 변경하지 않는다. 기존에 출력으로 확인한 mapping/Scheduler/Gateway를 같은 이유로 반복 생성하지 않는다.

1. DynamoDB 테이블/색인 구조를 조회한다. 테이블과 GSI1은 ACTIVE, PK/SK/GSI1PK는 S, **GSI1SK는 N**, GSI1 키는 GSI1PK(HASH)/GSI1SK(RANGE), projection ALL이 필요하다. Stream은 KEYS_ONLY이며 앞서 연결한 Stream ARN과 같아야 한다. [DescribeTable 명세](https://docs.aws.amazon.com/cli/latest/reference/dynamodb/describe-table.html)

```sh
aws dynamodb describe-table --region us-east-2 \
  --table-name arc-calc-dev-training \
  --query 'Table.{Name:TableName,Status:TableStatus,Keys:KeySchema,Types:AttributeDefinitions,Indexes:GlobalSecondaryIndexes[].{Name:IndexName,Status:IndexStatus,Keys:KeySchema,Projection:Projection.ProjectionType},Stream:StreamSpecification,StreamArn:LatestStreamArn}' \
  --output json --no-cli-pager
```

2. Relay 복구 점검의 진행 행 한 개만 강한 일관성으로 읽는다. 전체 훈련 기록을 조회하는 Scan이 아니다. 처음 준비한 행의 schema1, 정확한 binding 지문, revision/fence/lease_until0, owner NULL, next_kind OUTBOX, 두 scans의 cutoff/cursor NULL을 대조한다. null이면 행 생성부터 확인하며 기존 행을 지우거나 초기값으로 덮지 않는다. [GetItem 명세](https://docs.aws.amazon.com/cli/latest/reference/dynamodb/get-item.html)

```sh
aws dynamodb get-item --region us-east-2 \
  --table-name arc-calc-dev-training \
  --key '{"PK":{"S":"RELAY_SCAN#ef260e9aa3c673af240d17a2660480361a8e081d1ffeca2a5ed0e3219fc18567"},"SK":{"S":"PROGRESS#v1"}}' \
  --consistent-read --query Item \
  --output json --no-cli-pager
```

binding_sha256 기대값은 `d31b25574dba0ba9d5f549815e3c0980865a6b8708f1d5661136b82a7c962269`다. SHA 값은 비밀 키가 아니라 계정/리전/테이블/환경/큐 연결의 지문이다. 이미 Relay가 실행된 흔적이 있으면 revision/위치가 달라도 곧바로 초기화하지 않고 이유를 확인한다.

3. 작업 큐의 처리 대기시간과 실패 큐 연결을 읽는다. VisibilityTimeout은 문자열 `720`, RedrivePolicy를 JSON으로 해석했을 때 maxReceiveCount5와 정확한 `arc-calc-dev-calculation-dlq` ARN, SqsManagedSseEnabled는 `true`가 기대값이다. 실제 KMS 키가 있으면 기존 SSE-SQS 가정과 다르므로 키 권한을 따로 확인한다. 메시지 수는 근사치이며 예상치 못한 작업이 있으면 출처를 확인한다. 이 명령은 메시지를 수신·삭제·재전달하지 않는다. [GetQueueAttributes 명세](https://docs.aws.amazon.com/cli/latest/reference/sqs/get-queue-attributes.html)

```sh
aws sqs get-queue-attributes --region us-east-2 \
  --queue-url https://sqs.us-east-2.amazonaws.com/150612770165/arc-calc-dev-calculation-jobs \
  --attribute-names QueueArn VisibilityTimeout RedrivePolicy SqsManagedSseEnabled KmsMasterKeyId ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
  --output json --no-cli-pager
```

4. 아래 블록을 통째로 실행하면 세 Lambda의 필요한 설정만 차례로 출력한다. 환경 변수 전체나 키 값은 출력하지 않는다. Name별 역할/handler/메모리/시간은 앞의 설정표, State=Active, LastUpdate=Successful, Runtime=python3.12, Architecture=x86_64, Enabled=false, Stage=dev, StorageRegion=us-east-2를 확인한다. CodeSha256은 업로드 ZIP의 SHA-256을 base64로 인코딩한 `1H+3p8fOagnCxRrSWIrAsMymy9IpsJUiXU54m0N4+/A=`와 대조한다. [GetFunctionConfiguration 명세](https://docs.aws.amazon.com/cli/latest/reference/lambda/get-function-configuration.html)

```sh
for arc_dev_fn in arc-calc-dev-api arc-calc-dev-worker arc-calc-dev-relay
do
  aws lambda get-function-configuration --region us-east-2 \
    --function-name "$arc_dev_fn" \
    --query '{Name:FunctionName,Role:Role,Handler:Handler,Runtime:Runtime,Architecture:Architectures,MemoryMB:MemorySize,TimeoutSeconds:Timeout,State:State,LastUpdate:LastUpdateStatus,CodeSha256:CodeSha256,Enabled:Environment.Variables.ARC_MOCK_ENABLED,Stage:Environment.Variables.STAGE,StorageRegion:Environment.Variables.ARC_STORAGE_REGION}' \
    --output json --no-cli-pager
done
```

사용자가 위 출력을 전달하면 실제 값으로 대조한다. IAM의 실제 허용, S3 저장·서명 조회, 업무 처리·로그 기록, 앱 요청·응답은 별도의 실행 시험으로 확인한다. 환경 변수/전달 연결은 이 조회 단계에서 켜지 않는다. 공개 전 Gateway 요청량 제한은 사용자 선택 후 별도 적용하며 아직 숫자를 확정값으로 기록하지 않는다.

<a id="beginner-relay-first-run"></a>
### 저장 설정 확인 다음: Relay 최초 직접 실행

사용자가 DynamoDB 테이블/GSI1 ACTIVE, PK/SK/GSI1PK의 S와 GSI1SK의 N, GSI1 ALL, KEYS_ONLY 및 기존 mapping과 같은 Stream ARN을 전달했다. 앞선 세 Lambda·Relay 최초 행·SQS 조회와 함께 이 설정 묶음이 일치한다. 다음은 **설정 조회가 아니라 Relay의 실제 한 번 실행**이며 진행 행 갱신과 운용 로그 저장 시도가 발생한다. API/Worker 기능, Stream/Worker 트리거, Scheduler는 계속 비활성으로 두고 Relay부터 확인한다.

1. 오하이오 Lambda 콘솔에서 `arc-calc-dev-relay` → `Configuration(구성)` → `Environment variables(환경 변수)` → `Edit(편집)`을 연다. 기존 `ARC_MOCK_ENABLED` 값만 `false`에서 소문자 `true`로 바꾸고 저장한다. 다른 환경 변수는 보존한다. CLI의 `--environment`는 변수 묶음 전체를 교체하므로 한 변수만 넣어 갱신하지 않는다. [AWS 환경 변수 안내](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html)
2. 저장 후 CloudShell에서 아래를 실행한다. State=Active, Update=Successful, Enabled=true일 때 다음 단계로 간다. InProgress이면 잠시 후 같은 조회를 반복하고 Failed이면 오류 원인을 확인한다.

```sh
aws lambda get-function-configuration --region us-east-2 \
  --function-name arc-calc-dev-relay \
  --query '{Name:FunctionName,State:State,Update:LastUpdateStatus,Enabled:Environment.Variables.ARC_MOCK_ENABLED}' \
  --output json --no-cli-pager
```

3. 아래 블록을 통째로 한 번 실행한다. `mktemp`는 기존 파일을 덮지 않는 새 0600 결과 파일을 준비한다. CLI 추가 재시도는 이 명령에 한해1회 시도로 제한하고, 응답 대기는90초로 두어 Relay60초 실행 제한과 구별한다. 실패·응답 유실이면 반복 호출 전에 실제 결과를 확인한다. 콘솔 테스트 이벤트/예약을 만들거나 SQS에 가짜 계산 메시지를 넣지 않는다. [AWS 동기 호출 안내](https://docs.aws.amazon.com/cli/latest/reference/lambda/invoke.html)

```sh
arc_relay_probe_file=$(mktemp "$HOME/arc-calc-dev-relay-check.XXXXXX") &&
AWS_MAX_ATTEMPTS=1 aws lambda invoke --region us-east-2 \
  --function-name arc-calc-dev-relay \
  --invocation-type RequestResponse --log-type None \
  --cli-binary-format raw-in-base64-out \
  --payload '{"source":"aws.events"}' \
  --cli-read-timeout 90 \
  "$arc_relay_probe_file" \
  --query '{StatusCode:StatusCode,FunctionError:FunctionError,ExecutedVersion:ExecutedVersion}' \
  --output json --no-cli-pager
```

4. 같은 CloudShell 세션에서 응답 본문을 확인한다. 여기에는 로그인/복구 키가 아니라 Relay 처리 건수 또는 정제된 실행 오류가 들어간다. 빈 작업 상태의 정상 결과는 `{"outbox_wakes":0,"job_wakes":0,"failures":0}`이고, 3번은 StatusCode200/FunctionError=null이어야 한다. SDK 호출 접수200과 handler 성공을 구별한다. CLI 자체 오류가 났거나 결과 파일이 비어 있으면 성공으로 처리하지 않는다.

```sh
cat "$arc_relay_probe_file"
```

5. 응답 확인 뒤 진행 기록을 다시 읽는다. **최초 행에서 정확히 한 번 두 종류의 빈 검색을 완료한 경우** revision6/fence1, owner NULL, lease_until0, next_kind OUTBOX, 두 scans의 cutoff/cursor NULL이 기대값이다. 시간 부족·경합 시 counts가0이어도 점검을 완료하지 못했을 수 있으므로 기록과 함께 판정한다. 값이 다르면 초기화하지 않고 원인을 확인한다.

```sh
aws dynamodb get-item --region us-east-2 \
  --table-name arc-calc-dev-training \
  --key '{"PK":{"S":"RELAY_SCAN#ef260e9aa3c673af240d17a2660480361a8e081d1ffeca2a5ed0e3219fc18567"},"SK":{"S":"PROGRESS#v1"}}' \
  --consistent-read \
  --query 'Item.{Revision:revision.N,Fence:fence.N,Owner:owner,LeaseUntil:lease_until.N,NextKind:next_kind.S,Scans:scans}' \
  --output json --no-cli-pager
```

3~5번 결과를 함께 대조한다. 이 빈 검색 시험은 Relay의 조립·진행 행 읽기/조건부 쓰기·GSI 검색을 확인하는 단계이며, SQS 전송·Worker 계산·S3 파일·로그 저장까지 통과한 것으로 확대하지 않는다. 성공 후에도 이 단계에서는 API/Worker 기능 및 자동 트리거/예약을 켜지 않는다. 함수 비동기 오류 재시도 설정은 변경하지 않는다. 실제 파일 접수와 작업 전달·로그 조회는 후속 인수다.

<a id="beginner-worker-first-run"></a>
### Relay 최초 실행 다음: Worker 시작 확인과 SQS 연결 활성화

사용자가 Relay true/Active/Successful, 동기 호출200/FunctionError=null, 세 처리 건수0, 진행 행 revision6/fence1/owner NULL/lease0/next OUTBOX/두 scans NULL을 전달했다. 최초 빈 점검의 두 검색과 진행 갱신은 통과했다. 다음은 Worker 프로그램이 시작되는지 확인한 뒤 **기존** SQS 연결을 켜는 단계다. API 기능·Relay Stream 연결·Scheduler는 아직 비활성으로 유지한다.

1. 오하이오 Lambda `arc-calc-dev-worker` → 구성 → 환경 변수 → 편집에서 `ARC_MOCK_ENABLED`만 `false`에서 소문자 `true`로 바꾸고 저장한다. 다른 환경 변수와 기존 키/설정은 보존한다. Relay는 이미 true이므로 다시 수정하지 않는다.
2. 아래 조회에서 State=Active, Update=Successful, Enabled=true를 확인한 뒤 진행한다. InProgress이면 이 조회만 다시 하고 Failed이면 원인을 확인한다.

```sh
aws lambda get-function-configuration --region us-east-2 \
  --function-name arc-calc-dev-worker \
  --query '{Name:FunctionName,State:State,Update:LastUpdateStatus,Enabled:Environment.Variables.ARC_MOCK_ENABLED}' \
  --output json --no-cli-pager
```

3. 아래 블록을 통째로 한 번 실행한다. 빈 Records 목록은 가짜 훈련 작업을 생성하지 않고 Worker runtime·실행 정의·임시 과정·계산 adapter·SDK client 조립과 handler 진입을 검사한다. 실제 SQS 메시지를 받거나 삭제하지 않는다. 출력 파일은 mktemp로 새0600 파일을 만들고 CLI 추가 재시도는 이 명령에 한해1회 시도로 제한한다. CLI150초는 Worker120초와 구별되는 응답 대기 설정이다. [동기 호출 안내](https://docs.aws.amazon.com/cli/latest/reference/lambda/invoke.html)

```sh
arc_worker_probe_file=$(mktemp "$HOME/arc-calc-dev-worker-check.XXXXXX") &&
AWS_MAX_ATTEMPTS=1 aws lambda invoke --region us-east-2 \
  --function-name arc-calc-dev-worker \
  --invocation-type RequestResponse --log-type None \
  --cli-binary-format raw-in-base64-out \
  --payload '{"Records":[]}' \
  --cli-read-timeout 150 \
  "$arc_worker_probe_file" \
  --query '{StatusCode:StatusCode,FunctionError:FunctionError,ExecutedVersion:ExecutedVersion}' \
  --output json --no-cli-pager
```

4. 같은 CloudShell에서 응답 파일을 읽는다. 3번 StatusCode200/FunctionError=null과 아래 본문 `{"batchItemFailures":[]}`가 모두 맞을 때만5번으로 간다. CLI 오류·FunctionError·빈 파일·다른 응답이면 연결을 켜지 않고 결과부터 확인한다.

```sh
cat "$arc_worker_probe_file"
```

5. 성공했으면 아래 명령으로 기존 Worker 연결 UUID만 활성화한다. 새 mapping을 만들지 않는다. 이때부터 실제 SQS polling/메시지 처리가 시작될 수 있으므로 단순 조회와 구별한다. 앞서 큐의 대기/처리 중 근사 메시지 수는0이었고 API는 계속 비활성이다. 배치/부분 실패/필터/역할은 변경하지 않는다. [연결 활성화 명세](https://docs.aws.amazon.com/cli/latest/reference/lambda/update-event-source-mapping.html)

```sh
aws lambda update-event-source-mapping --region us-east-2 \
  --uuid eef89e6e-b897-40e9-885f-87ebeddbf094 \
  --enabled \
  --query '{UUID:UUID,State:State}' \
  --output json --no-cli-pager
```

6. 아래 조회로 State=Enabled, 정확한 Worker/작업 큐 ARN, BatchSize1, BatchWindow0, PartialFailures=[ReportBatchItemFailures], SQS Filter가 없음(null 또는 빈 설정)을 확인한다. Enabling이면 잠시 뒤 **이 조회만** 다시 실행한다. LastResult가 비어 있거나 아직 처리 기록이 없다고 가짜 SQS 메시지를 넣지 않는다.

```sh
aws lambda get-event-source-mapping --region us-east-2 \
  --uuid eef89e6e-b897-40e9-885f-87ebeddbf094 \
  --query '{UUID:UUID,State:State,Function:FunctionArn,Source:EventSourceArn,BatchSize:BatchSize,BatchWindow:MaximumBatchingWindowInSeconds,PartialFailures:FunctionResponseTypes,Filter:FilterCriteria,LastResult:LastProcessingResult}' \
  --output json --no-cli-pager
```

3·4·6번 결과를 함께 확인한다. 빈 목록 성공은 DDB/S3 권한·실제 계산·lease 갱신·업무 결과·운용 로그 저장 인수가 아니다. Relay Stream/Scheduler 활성화, S3 설정/실제 파일, API 기동·Gateway 제한/배포와 앱 호출은 다음 단계다.

Worker가 시작 시험을 통과했지만 연결 조회가 `Enabling`인 경우에는1~2분 뒤 아래 읽기만 실행한다. `LastResult=null`만으로 실패를 판단하지 않는다. Reason은 최근 상태 변경 관련 정보이며 지연 원인을 반드시 설명하는 필드는 아니다. 활성화 요청 뒤 약5분 이상 계속 Enabling이면 아래 결과와 경과 시간을 확인해 추가 점검한다. 이 시간은 점검 기준이며 AWS의 완료 시간 보장이 아니다. Update를 반복하거나 연결을 삭제·재생성하지 않는다.

```sh
aws lambda get-event-source-mapping --region us-east-2 \
  --uuid eef89e6e-b897-40e9-885f-87ebeddbf094 \
  --query '{State:State,Reason:StateTransitionReason,LastResult:LastProcessingResult,LastModified:LastModified}' \
  --output json --no-cli-pager
```

<a id="beginner-relay-stream-enable"></a>
### Worker 연결 활성화 다음: Relay Stream과 예약 준비

사용자가 Worker mapping의 Enabled/USER_INITIATED 결과를 전달했다. Worker 시작 시험과 연결 활성화는 확인됐고 실제 훈련 메시지 처리 인수는 별도다. 이제 이미 true로 실행 검증한 Relay에 기존 Stream mapping을 연결한다. API는 아직 false이며 신규 앱 요청을 열지 않는다.

1. 아래 명령으로 Relay Stream의 기존 mapping만 켠다. 필터·배치·시작 위치·진행 기록은 변경하지 않고 새 mapping을 만들지 않는다.

```sh
aws lambda update-event-source-mapping --region us-east-2 \
  --uuid 94038f0a-a725-4324-ba22-b14bdc2fc6d7 \
  --enabled \
  --query '{UUID:UUID,State:State}' \
  --output json --no-cli-pager
```

2. 아래 조회에서 State=Enabled, Function이 정확한 Relay ARN, Source가 앞서 확인한 `arc-calc-dev-training/stream/2026-09-23T02:57:25.829`, BatchSize1/BatchWindow0/부분 실패/OUTBOX#·DISPATCH 필터를 확인한다. Enabling이면1~2분 뒤 이 조회만 반복하고 장시간 전환되지 않으면 상태와 경과 시간을 확인한다. [mapping 상태와 조회](https://docs.aws.amazon.com/cli/latest/reference/lambda/get-event-source-mapping.html)

```sh
aws lambda get-event-source-mapping --region us-east-2 \
  --uuid 94038f0a-a725-4324-ba22-b14bdc2fc6d7 \
  --query '{UUID:UUID,State:State,Reason:StateTransitionReason,Function:FunctionArn,Source:EventSourceArn,BatchSize:BatchSize,BatchWindow:MaximumBatchingWindowInSeconds,PartialFailures:FunctionResponseTypes,Filter:FilterCriteria,LastResult:LastProcessingResult}' \
  --output json --no-cli-pager
```

3. 기존1분 예약의 현재 설정을 읽는다. 이 조회는 예약을 켜지 않는다. Scheduler UpdateSchedule은 상태만 보내는 부분 갱신이 아니며, 생략한 선택 필드가 기본값으로 돌아갈 수 있으므로 **현재 저장된 갱신 가능 필드 전체를 먼저 확인**한다. 아래 출력은 현재 검증한 Relay 예약에 한정한다. [AWS 예약 갱신 안내](https://docs.aws.amazon.com/cli/latest/reference/scheduler/update-schedule.html)

```sh
aws scheduler get-schedule --region us-east-2 \
  --group-name arc-calc-dev-schedules \
  --name arc-calc-dev-relay-reconcile \
  --query '{Name:Name,GroupName:GroupName,Description:Description,ScheduleExpression:ScheduleExpression,ScheduleExpressionTimezone:ScheduleExpressionTimezone,StartDate:StartDate,EndDate:EndDate,FlexibleTimeWindow:FlexibleTimeWindow,State:State,ActionAfterCompletion:ActionAfterCompletion,KmsKeyArn:KmsKeyArn,Target:Target}' \
  --output json --no-cli-pager
```

2·3번 사용자 출력이 오면 현재 예약에서 State만 ENABLED로 바꾸는 후속 요청을 준비한다. 실제 null인 선택 필드는 요청에서 생략하고, 날짜/시간대/KMS/대상·입력·추가 재시도0·최대 사건 나이 등 존재하는 값은 보존한다. 과거 생성 파일만으로 현재 선택 설정 보존을 주장하지 않는다. 예약 활성화 저장과 실제 자동 점검 성공은 구별하며, 활성화 뒤 수동 Relay 재호출 없이 진행 기록 증가와 정제된 실행 증거를 확인한다.

<a id="beginner-relay-schedule-enable"></a>
### Relay Stream 활성화 다음: 1분 자동 점검 켜기

사용자 출력에서 Relay Stream mapping은 Enabled, 정확한 함수/Stream, 배치1·대기0·부분 실패·OUTBOX#/DISPATCH 필터로 확인됐다. `No records processed`는 아직 처리한 기록이 없다는 뜻이며 이 값만으로 실패라고 판단하지 않는다. 현재 Scheduler는 DISABLED다. 다음 요청부터는 예약이 실제로 Relay를 호출하고 DB의 진행 기록을 갱신한다. API 기능은 아직 false로 유지한다.

1. 준비된 `var/deployment/dev-runtime-20260923-0octlgf9/relay-schedule-enabled.json`을 오하이오 CloudShell의 Actions → Upload file로 업로드한다. Mac 파일 선택 창에서 `⌘⇧G`를 누르고 `/Users/mac/arc_calculator_api/var/deployment/dev-runtime-20260923-0octlgf9`로 이동해 이 파일 하나를 선택한다. 최신 사용자 GetSchedule의 모든 non-null 갱신 값을 보존하고 State만 ENABLED로 바꾼 파일이다. 실제 반환된 UTC와 MaximumEventAgeInSeconds=86400도 보존하며 null인 StartDate/EndDate/KmsKeyArn은 제외했다. 86400은 DB 보관기간이 아니며 추가 전달 재시도는 계속0회다. UpdateSchedule은 생략한 선택값을 기본값으로 되돌릴 수 있어 전체 설정을 보낸다. [AWS 예약 갱신 명세](https://docs.aws.amazon.com/cli/latest/reference/scheduler/update-schedule.html)

2. 아래 명령으로 기존 예약을 켠다. 성공 시 ScheduleArn은 `arn:aws:scheduler:us-east-2:150612770165:schedule/arc-calc-dev-schedules/arc-calc-dev-relay-reconcile`이다.

```sh
aws scheduler update-schedule --region us-east-2 \
  --cli-input-json file://"$HOME/relay-schedule-enabled.json" \
  --output json --no-cli-pager
```

3. 실제 저장된 설정을 읽는다. State=ENABLED, Rate=`rate(1 minute)`, Timezone=UTC, Window=OFF, Retries=0, MaxAgeSeconds=86400과 정확한 함수/역할/입력을 확인한다.

```sh
aws scheduler get-schedule --region us-east-2 \
  --group-name arc-calc-dev-schedules \
  --name arc-calc-dev-relay-reconcile \
  --query '{Name:Name,State:State,Rate:ScheduleExpression,Timezone:ScheduleExpressionTimezone,Window:FlexibleTimeWindow.Mode,Retries:Target.RetryPolicy.MaximumRetryAttempts,MaxAgeSeconds:Target.RetryPolicy.MaximumEventAgeInSeconds,Function:Target.Arn,Role:Target.RoleArn,Input:Target.Input}' \
  --output json --no-cli-pager
```

4. 수동으로 Relay를 호출하지 않고 2~3분 뒤 아래 조회만 실행한다. Scheduler는 초 단위 정시 실행을 보장하지 않으며 이 대기는 관찰용이다. [AWS 예약 시간 정밀도](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html)

```sh
aws dynamodb get-item --region us-east-2 \
  --table-name arc-calc-dev-training \
  --key '{"PK":{"S":"RELAY_SCAN#ef260e9aa3c673af240d17a2660480361a8e081d1ffeca2a5ed0e3219fc18567"},"SK":{"S":"PROGRESS#v1"}}' \
  --consistent-read \
  --query 'Item.{Revision:revision.N,Fence:fence.N,Owner:owner,LeaseUntil:lease_until.N,NextKind:next_kind.S,Scans:scans}' \
  --output json --no-cli-pager
```

앞선 수동1회 결과는 Revision=6/Fence=1이었다. 추가 수동 호출 없이 이보다 증가하면 예약 활성화 이후 Relay가 진행 기록을 갱신한 근거다. 완료된 빈 점검에서는 Owner={NULL:true}, LeaseUntil=0, NextKind=OUTBOX와 두 scans의 cutoff/cursor=NULL도 함께 확인한다. 조회 순간 실행 중일 수 있으므로 Owner가 있으면 즉시 실패로 판단하지 않고1분 뒤 조회만 다시 한다. 숫자가 늘지 않으면 설정·호출/실행 증거를 점검하며 진행 행 초기화·예약 재생성으로 해결하지 않는다. 3·4번 출력으로 다음 단계를 판단한다. 설정 ENABLED만으로 자동 실행·실제 계산 성공을 주장하지 않으며 실제 파일·SQS 전달·Worker 계산·운용 로그·API/Gateway 인수는 후속 단계다.

<a id="beginner-storage-api-enable"></a>
### 자동 점검 확인 다음: 저장 설정 조회와 API 활성화

사용자 출력에서 Scheduler는 ENABLED이며 기존1분/UTC/OFF/재시도0/최대 사건 나이86400/정확한 대상·역할·입력을 유지한다. 추가 수동 호출 없이 조회한 Relay 진행 기록은 revision6→54/fence1→9, owner NULL/lease0/next OUTBOX/두 scans NULL이다. 이 범위의 자동 빈 점검과 종료는 확인됐으며 실제 파일 계산·업무 로그 저장까지 확인한 것은 아니다.

1. 오하이오 CloudShell에서 다음 S3 조회를 각각 실행한다. 모두 읽기 전용이고 예상 소유 계정을 지정한다. 먼저 실제 버킷 리전, 공개 차단4개, ACL 비활성화, 기본 암호화를 확인한다. 앞선 사용자 생성 보고와 구별하여 이 출력으로 실제 저장값을 대조한다.

```sh
aws s3api get-bucket-location --region us-east-2 \
  --bucket arc-calc-dev-storage-150612770165 \
  --expected-bucket-owner 150612770165 \
  --query '{Region:LocationConstraint}' \
  --output json --no-cli-pager
```

```sh
aws s3api get-public-access-block --region us-east-2 \
  --bucket arc-calc-dev-storage-150612770165 \
  --expected-bucket-owner 150612770165 \
  --query 'PublicAccessBlockConfiguration' \
  --output json --no-cli-pager
```

```sh
aws s3api get-bucket-ownership-controls --region us-east-2 \
  --bucket arc-calc-dev-storage-150612770165 \
  --expected-bucket-owner 150612770165 \
  --query '{Ownership:OwnershipControls.Rules[].ObjectOwnership}' \
  --output json --no-cli-pager
```

```sh
aws s3api get-bucket-encryption --region us-east-2 \
  --bucket arc-calc-dev-storage-150612770165 \
  --expected-bucket-owner 150612770165 \
  --query '{Encryption:ServerSideEncryptionConfiguration.Rules[].ApplyServerSideEncryptionByDefault}' \
  --output json --no-cli-pager
```

기대값은 Region=us-east-2, 공개 차단4개 모두true, Ownership=[BucketOwnerEnforced], Encryption 안의 SSEAlgorithm=AES256이다. 이는 버킷 설정 확인이며 Lambda 역할의 실제 파일 읽기/쓰기 권한 증거는 아니다.

2. S3와 DB의 자동 삭제 설정을 읽는다. S3의 NoSuchLifecycleConfiguration만은 '규칙 없음'이라는 기대 결과이며 규칙을 새로 만들어 해소하지 않는다. 기존 규칙이 있으면 삭제/변경하지 않고 내용을 확인한다. DB의 TimeToLiveStatus는 DISABLED가 기대값이며 DISABLING은 완료가 아니다. [AWS lifecycle 조회 명세](https://docs.aws.amazon.com/cli/latest/reference/s3api/get-bucket-lifecycle-configuration.html), [TTL 조회 명세](https://docs.aws.amazon.com/cli/latest/reference/dynamodb/describe-time-to-live.html)

```sh
aws s3api get-bucket-lifecycle-configuration --region us-east-2 \
  --bucket arc-calc-dev-storage-150612770165 \
  --expected-bucket-owner 150612770165 \
  --query '{Rules:Rules}' \
  --output json --no-cli-pager
```

```sh
aws dynamodb describe-time-to-live --region us-east-2 \
  --table-name arc-calc-dev-training \
  --query 'TimeToLiveDescription' \
  --output json --no-cli-pager
```

3. 1·2번이 기대값과 일치하면 오하이오 Lambda 콘솔의 `arc-calc-dev-api` → Configuration(구성) → Environment variables(환경 변수) → Edit(편집)에서 기존 `ARC_MOCK_ENABLED`만 소문자 `true`로 바꾸고 저장한다. 다른 변수·키는 보존하고 환경 변수 화면 전체를 공유하지 않는다. CLI에 한 변수만 넣으면 전체 변수 묶음을 교체할 수 있어 콘솔의 기존 행을 편집한다. [AWS 환경 변수 안내](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html)

4. 아래 명령으로 Name=arc-calc-dev-api, State=Active, Update=Successful, Enabled=true를 확인한다. InProgress이면 잠시 후 조회만 다시 하고, Failed나 저장 설정 불일치/예상 밖 조회 오류는 결과를 먼저 확인한다.

```sh
aws lambda get-function-configuration --region us-east-2 \
  --function-name arc-calc-dev-api \
  --query '{Name:FunctionName,State:State,Update:LastUpdateStatus,Enabled:Environment.Variables.ARC_MOCK_ENABLED}' \
  --output json --no-cli-pager
```

이 단계는 저장 설정과 API 기능 플래그 저장 확인이다. 실제 API 기동·로그인·파일 쓰기/읽기·운용 로그·Gateway stage 배포와 앱 호출은 후속 인수다. 이 출력으로 전체 AWS 배포 통과를 선언하지 않으며 API 요청량 제한도 사용자 선택 전 확정하지 않는다.

<a id="beginner-api-direct-check"></a>
### API 활성화 다음: Dummy 로그인·과정 목록 직접 시험

사용자 S3 출력은 실제 버킷/예상 계정으로 조회한 Ohio, 공개 차단4개true, BucketOwnerEnforced, AES256, NoSuchLifecycleConfiguration으로 준비값과 일치한다. API도 true/Active/Successful이다. TTL 출력은 DISABLED이나 붙여넣은 명령이 S3 lifecycle 조회와 섞여 있어 정확한 테이블의 TTL 조회를 한 번 더 수행한다. S3 lifecycle 규칙 없음 오류를 해결하려고 규칙을 만들지 않는다.

1. 오하이오 CloudShell에서 아래 블록 전체를 실행한다. Table=arc-calc-dev-training, TTL=DISABLED가 기대값이다. 다른 값이나 오류면 후속 시험 전에 출력부터 확인한다.

```sh
aws dynamodb describe-time-to-live --region us-east-2 \
  --table-name arc-calc-dev-training \
  --query '{Table:`arc-calc-dev-training`,TTL:TimeToLiveDescription.TimeToLiveStatus}' \
  --output json --no-cli-pager
```

2. 준비한 `var/deployment/dev-runtime-20260923-0octlgf9/api-direct-check.py`를 CloudShell Actions → Upload file로 업로드한다. Mac 파일 선택 창에서는 `⌘⇧G` 후 `/Users/mac/arc_calculator_api/var/deployment/dev-runtime-20260923-0octlgf9`로 이동해 파일을 선택한다. 이 `/Users/mac/...` 주소는 **Mac 파일 선택 창의 폴더 주소**이며 CloudShell 명령창에 실행하는 명령이 아니다. 표준 Python3와 기존 AWS CLI를 사용하므로 추가 패키지나 접근 키 입력은 없다.

3. 아래 명령을 한 번 실행한다. 이 시험은 설정 조회와 달리 Lambda를 실제 호출한다. Dummy 세션과 과정 공급 자료/캐시·진도 gate·운용 기록의 DB/S3 저장이 발생할 수 있다. 실제 훈련 시작·계산·공유 진도 초기화·Gateway 배포는 수행하지 않는다. 공용 Dummy 로그아웃은 공유 epoch를 변경하므로 시험 뒤 자동 로그아웃하지 않는다. 새 세션의 인증 유효기간은24시간이고 DB 행 자동 삭제와 구별한다.

```sh
python3 "$HOME/api-direct-check.py"
```

스크립트는 정확한 계정150612770165/리전us-east-2/API 함수ARN을 사용한다. STS 계정 확인 뒤 인증 없이 세션 조회401 SESSION_REQUIRED → Dummy 로그인201·ready·토큰 발급 → 발급 토큰으로 세션 조회200·동일 세션 → 과정 목록200·15개·Dummy 표시를 확인한다. GET의 query는 REST event의 queryStringParameters에 넣고 URL path에 붙이지 않는다. 기존 과정 status를 NOT_STARTED로 강제하지 않는다. [AWS Lambda 직접 호출 명세](https://docs.aws.amazon.com/cli/latest/reference/lambda/invoke.html)

토큰은 전용0700 임시 폴더의0600 요청/응답 파일에서 다루며 화면·명령 인자에 출력하지 않는다. AWS 인증 호출 전에 로컬 `aws configure get cli_history`를 캡처 조회하여 기록 기능이 기본 비활성 또는 명시 disabled일 때만 진행한다. enabled/조회 실패면 토큰을 만들기 전에 중단하며 기존 AWS 설정을 변경하지 않는다. 선택적 CLI history는 요청/응답 원문을 저장할 수 있기 때문이다. [AWS CLI 데이터 보호 안내](https://docs.aws.amazon.com/cli/latest/userguide/data-protection.html) CLI stdout/stderr와 응답 원문을 공유하지 않고 정해진 상태/건수/허용 오류코드만 출력한다. 종료 시 스크립트가 만든 임시 파일만 정리하며 기존 DB/S3 자료·키는 삭제하지 않는다. Lambda RequestResponse/LogType None, CLI 최대 시도1, 응답 대기45초/하위 프로세스65초 한도로 실행한다. 접수200·FunctionError·실제 HTTP 상태와 응답 envelope를 각각 검사한다. 중단/timeout이면 실제 실행 여부가 불확실할 수 있어 자동 재시도하지 않는다.

정상 출력은 account 및 without_login/login/session/courses 각각 Result=PASS, 마지막 Result=ALL_PASS다. without_login의 HTTPStatus=401은 의도한 접근 차단이다. login은 HTTPStatus=201, TokenPresent=true, LearningState=ready/Reason=null이며 session은200, courses는200/CourseCount15/ReturnedCount15/AllDummyCourses=true여야 한다. FAIL/STOP이면 반복 로그인 전에 출력으로 원인을 확인한다. 출력 전체는 비밀을 제외한 요약이므로 사용자 후속 확인에 사용한다. 실제 토큰 파일을 cat하거나 Lambda 로그인 응답 전체를 대화에 붙이지 않는다.

통과 범위는 직접 Lambda의 인증·세션·과정 공급/저장·조회다. 이는 실물 훈련 파일, Worker/SQS 전달, 점수·차트, 운용 로그의 실제 저장 확인, API Gateway HTTPS 경로/제한/배포 인수를 대신하지 않는다. 후속 사용자 선택으로 Gateway의 Dev 초기 제한은 D98(초당10회/burst20개)이며 실제 적용은 다음 단계다.

<a id="beginner-gateway-stage-readback"></a>
### 직접 로그인 통과 다음: 접속 주소 배포 전 현재 상태 확인

사용자가 정확한 테이블 TTL=DISABLED와 직접 API 시험의 계정PASS, 미로그인401 SESSION_REQUIRED, 로그인201/ready/토큰 있음, 세션200/동일 세션, 과정200/15개/Dummy, 최종 ALL_PASS를 전달했다. 이 출력 범위의 직접 Lambda 시험은 통과했다. Mac 폴더 주소를 CloudShell에 입력해 발생한 No such file or directory는 후속 스크립트 실행 성공으로 파일 재업로드/재실행이 필요하지 않다.

초기 요청량 제한은 사용자 D98에 따라 **초당10회, burst20개**다. AWS throttle은 최선 노력 방식으로 요청/비용의 엄격한 상한은 아니며 초과 요청에429가 발생할 수 있다. [AWS 요청량 제한 안내](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-request-throttling.html) 다음은 실제 stage가 이미 있는지 확인하는 읽기 전용 단계다. 새 stage 생성이나 기존 배포 변경은 이 조회로 수행되지 않는다.

1. CloudShell에서 API 기본 설정을 읽는다. Id=2ftxmdtrx1, Name=arc-calc-dev-rest-api, Type=[REGIONAL], Binary에 multipart/form-data, DefaultEndpointDisabled=false와 기존 태그가 기대값이다. 기본 endpoint가 꺼져 있거나 별도 resource policy가 있으면 임의로 해제하지 않고 현재 조건을 확인한다. [AWS API 설정 조회](https://docs.aws.amazon.com/cli/latest/reference/apigateway/get-rest-api.html)

```sh
aws apigateway get-rest-api --region us-east-2 \
  --rest-api-id 2ftxmdtrx1 \
  --query '{Id:id,Name:name,Type:endpointConfiguration.types,Binary:binaryMediaTypes,DefaultEndpointDisabled:disableExecuteApiEndpoint,HasResourcePolicy:policy != `null`,Tags:tags}' \
  --output json --no-cli-pager
```

2. 현재 배포 구역(stage) 목록을 읽는다. `[]`이면 아직 stage가 없다는 정상 결과다. 기존 항목이 있으면 배포번호/캐시/메서드 설정/접속 로그 대상/WAF/일부 트래픽 배포 여부를 보존해 후속 변경 범위를 판단한다. 변수 값과 로그 format 원문은 출력하지 않는다. [AWS stage 조회](https://docs.aws.amazon.com/cli/latest/reference/apigateway/get-stages.html)

```sh
aws apigateway get-stages --region us-east-2 \
  --rest-api-id 2ftxmdtrx1 \
  --query 'item[].{Stage:stageName,Deployment:deploymentId,Cache:cacheClusterEnabled,Methods:methodSettings,AccessLogDestination:accessLogSettings.destinationArn,WebACL:webAclArn,CanaryPercent:canarySettings.percentTraffic,CanaryDeployment:canarySettings.deploymentId,Tracing:tracingEnabled}' \
  --output json --no-cli-pager
```

두 출력을 대조한 후 dev stage 생성 또는 기존 설정 보존 갱신을 준비한다. create-stage/create-deployment에는 methodSettings/throttle 입력 필드가 없어 생성과 제한 적용을 단일 요청으로 간주하지 않는다. 실제 공개 연결에서는 요청량 제한, 캐시 비활성, 요청/응답 본문 추적 비활성을 확인하고 계정 전체 CloudWatch 역할을 덮어쓰지 않는다. API 연결·HTTPS 호출 검증 전 예상 주소만으로 앱 연결 완료를 보고하지 않는다.

<a id="beginner-gateway-create-dev"></a>
### 빈 stage 확인 다음: dev 배포와 요청량 제한 적용

사용자 조회에서 API2ftxmdtrx1/이름/Regional/multipart/기본 endpoint 사용/정책 없음/태그가 일치하고 stage 목록이 `[]`였다. 기존 사용자 stage를 수정하는 상황이 아니다. 아래는 **사용자가 실제 배포본과 dev stage를 생성하는 단계**이며 에이전트가 AWS에서 실행한 것이 아니다. D98의10/s·burst20을 적용한다.

1. 오하이오 `arc-calc-dev-api` → Configuration(구성) → Environment variables(환경 변수) → Edit(편집)에서 기존 `ARC_MOCK_ENABLED`만 소문자 `false`로 바꾸고 저장 완료를 기다린다. stage 생성과 메서드 제한 적용이 별도 요청이므로 설정 중 신규 로그인·과정 준비를 차단하기 위한 일시 조치다. 이 플래그는 HTTP503을 반환하며 AWS 호출 비용 자체를 없애는 장치는 아니다. 다른 환경 변수/키는 보존한다. [CreateStage 입력](https://docs.aws.amazon.com/cli/latest/reference/apigateway/create-stage.html), [UpdateStage 패치](https://docs.aws.amazon.com/apigateway/latest/api/patch-operations.html)

2. `var/deployment/dev-runtime-20260923-0octlgf9/create-dev-stage.sh`를 오하이오 CloudShell Actions → Upload file로 업로드한다. Mac 파일 선택 창의 `⌘⇧G`에 `/Users/mac/arc_calculator_api/var/deployment/dev-runtime-20260923-0octlgf9`를 입력하고 파일을 선택한다. 이 폴더 주소는 CloudShell 명령창에 입력하는 명령이 아니다.

3. CloudShell에서 아래 명령을 한 번 실행한다.

```sh
bash "$HOME/create-dev-stage.sh"
```

스크립트는 CLI history 비활성, 계정150612770165, API Active/Successful/false, stage0개를 확인한다. 최대 시도1로 배포본(stage-name 없이) → dev stage(캐시/추적 꺼짐, Project/Env 태그) → 메서드 패치5개를 차례로 적용한다. `/*/*/throttling/rateLimit=10`, `burstLimit=20`, `logging/dataTrace=false`, `logging/loglevel=OFF`, `caching/enabled=false`는 모두replace다. 기본 설정 응답 키는 `methodSettings["*/*"]`다. 기존 stage/배포를 덮어쓰거나 삭제하지 않고, 계정 공용 CloudWatch 역할·Lambda 환경 변수·키를 변경하지 않는다.

마지막 GetStage에서 실제 배포ID와 dev 이름, 단일 기본 메서드 설정5개, 태그, 캐시/추적 꺼짐, 접속 로그 대상/일부 트래픽 배포 없음까지 검사한다. 일치하면 `READY_TO_ENABLE_API`를 출력한다. 이 표시는 **저장된 stage 설정 검증 완료**이며 HTTPS 기능 인수 완료가 아니다. 오류·STOP이면 API를false로 두고 출력부터 확인한다. 배포본만 생성됐거나 응답 유실로 실제 변경 여부가 불명확할 수 있어 무조건 재실행하거나 자원을 삭제하지 않는다. 자체 생성한 로컬 임시 진단 파일만 정리한다.

4. `READY_TO_ENABLE_API`가 나온 경우에만 1번과 같은 API 환경 변수의 기존 `ARC_MOCK_ENABLED`를 소문자 `true`로 복원하고 저장한다. 아래 명령으로 Active/Successful/true를 확인한다. InProgress이면 조회만 잠시 뒤 반복하며 Failed나 기대값 불일치이면 다음 호출 전에 확인한다.

```sh
aws lambda get-function-configuration --region us-east-2 \
  --function-name arc-calc-dev-api \
  --query '{Name:FunctionName,State:State,Update:LastUpdateStatus,Enabled:Environment.Variables.ARC_MOCK_ENABLED}' \
  --output json --no-cli-pager
```

5. API 설정이 위 값이면 같은 CloudShell에서 실제 HTTPS 경로에 로그인 없이 접근한다. 토큰이나 로그인 정보를 보내지 않으며 마지막 `/`를 포함한다.

```sh
curl --silent --show-error --include \
  --connect-timeout 10 --max-time 45 \
  'https://2ftxmdtrx1.execute-api.us-east-2.amazonaws.com/dev/api/v2/session/'
```

기대값은 HTTP401과 `error.code=SESSION_REQUIRED`다. 이는 인터넷 경로가 API의 미로그인 거절까지 도달한 근거다. 403/404/503 등 다른 결과면 해당 출력과 stage 설정을 대조하며 성공으로 처리하지 않는다. 이 조회에는 로그인 토큰/복구 증표/서명URL이 없어 상태·헤더·정제된 오류 본문을 함께 확인할 수 있다. 3번 전체 출력, 4번 설정, 5번 응답으로 다음 단계를 판단한다. HTTPS 로그인/과정 조회·실제 multipart 파일 보존·Worker 작업 전달/계산·차트·운용 로그 저장 확인은 후속 인수다.

<a id="beginner-api-https-check"></a>
### dev 주소의 401 확인 다음: HTTPS 로그인·조회 시험

2026-09-23 사용자 출력에서 배포본 `t58l2a`/stage `dev`, 요청량10/s·burst20, 캐시/본문 로그/추적 꺼짐, 접속 로그 대상/일부 트래픽 배포 없음, API Active/Successful/true를 확인했다. 실제 `GET /dev/api/v2/session/`은401/SESSION_REQUIRED 및 JSON/no-store를 반환했다. 로그인하지 않은 요청을 정상 거절한 결과다. stage 생성 명령이나 환경 변수 변경을 반복할 필요는 없다.

1. Mac에서 준비한 `var/deployment/dev-runtime-20260923-0octlgf9/api-https-check.py`를 같은 CloudShell의 Actions → Upload file로 올린다. 파일 선택창에서 `⌘⇧G`를 누르고 `/Users/mac/arc_calculator_api/var/deployment/dev-runtime-20260923-0octlgf9`를 입력한 뒤 파일을 선택한다. 이 Mac 폴더 주소는 CloudShell에서 실행하는 명령이 아니다. 앞서 실행한 `api-direct-check.py`도 같은 CloudShell 홈에 남겨 둔다.

2. CloudShell에서 아래 한 줄을 한 번 실행한다.

```sh
python3 "$HOME/api-https-check.py"
```

추가 패키지·AWS 키 입력은 없다. 고정 Dev HTTPS 주소로 미로그인401 → Dummy 로그인201/ready → 동일 세션200 → 임시 과정15개200 → 한 개씩 조회200 → 마지막 `/` 없는 경로404를 검사한다. `pagination`은 Gateway가 query를 전달하는지, `missing_trailing_slash`는 경로를 임의 보정하지 않는지 확인한다. 마지막 두 요청의 기대 상태가 각각200/404인 것은 정상이다.

3. 여섯 단계 모두 `Result=PASS`, 마지막에 `Result=ALL_PASS`, `Scope=https_login_session_courses_query_path`면 이 범위를 통과한 것이다. 출력에는 토큰·응답 원문·세션ID·서명URL이 없다. `STOP`이면 출력부터 확인하고 무조건 재실행하지 않는다. 시간 초과나 연결 중단은 이미 로그인됐을 수도 있어 자동 재시도하지 않는다. `DIRECT_CHECK_HELPER_MISSING/MISMATCH`는 기존 `api-direct-check.py`의 누락/내용 불일치이므로 준비한 원본 파일을 대조한다. `TLS_KEY_LOGGING_CONFIGURED`는 TLS 통신 키 기록 설정을 발견한 것으로 원인 확인 전 시험을 진행하지 않는다.

시험은 검증한 기존 helper의 SHA256을 확인한 뒤 해당 bytes의 응답 검사를 재사용한다. 토큰은 메모리와 고정 HTTPS 요청 헤더에서만 사용하며 AWS CLI·응답 파일·redirect 추종·로그아웃/초기화·계산 업로드는 없다. TLS 인증서/호스트명 검증, debug0, 요청별 socket timeout45초, 응답 읽기1MiB 상한을 사용한다. 시험 도구의 읽기 상한은 서버 업로드 한도 변경이 아니다. [Python HTTPSConnection](https://docs.python.org/3/library/http.client.html#http.client.HTTPSConnection), [TLS 설정과 키 기록](https://docs.python.org/3/library/ssl.html#ssl.create_default_context)

로그인은 새 세션과 과정 준비에 필요한 DB/S3 기록을 만들지만 공용 진도를 초기화하지 않는다. 이 시험의 성공은 multipart 파일 보존·Worker 계산·차트·운용 로그 저장 조회를 대신하지 않는다. 다음 계산은 사용자 D99에 따라 저장소의 `tests/dataset/cco_1.bin`(31,380 bytes, 성인 압박 전용)을 사용한다. 저장소 manifest가 recorded 자료로 분류한 파일이며 현재 앱에서 새로 수집한 파일은 아니다. 현재 코드의 오프라인 예상 결과는 압박101회/환기0회/전체 점수100이며 실제 AWS 결과와 비교한다. AED 파일은 필요 없다. 과정/시도 시작 응답의 ID·definitionHash·condition을 사용하고 공유 진도 초기화를 자동 수행하지 않는다.

<a id="beginner-api-calculation-check"></a>
### HTTPS 로그인·조회 통과 다음: 기존 압박 파일 한 건 계산

사용자가 `api-https-check.py`의 여섯 PASS와 `ALL_PASS/https_login_session_courses_query_path` 출력을 제공했다. 미로그인401, 로그인201/ready, 동일 세션200, 과정15개200, pageSize1 query 전달200, 마지막 슬래시 누락404와 JSON/no-store/요청ID 검사가 실제 Dev HTTPS에서 통과했다. 다음은 D99의 기존 자료를 실제 multipart로 업로드하는 최초 계산 시험이다.

1. 같은 오하이오 CloudShell의 Actions → Upload file에서 **`api-calculation-check.py`와 `cco_1.bin` 두 파일을 각각 업로드**한다. Mac 파일 선택창에서 `⌘⇧G`를 누르고 `/Users/mac/arc_calculator_api/var/deployment/dev-runtime-20260923-0octlgf9`를 입력한다. 이 폴더에는 검증한 원본 `tests/dataset/cco_1.bin`의 동일 사본을 준비했다. 폴더 경로를 CloudShell 명령으로 실행하지 않는다. 기존 시험 파일들은 보존한다.

2. 아래 한 줄을 실행한다. 표준 Python3만 사용하며 패키지 설치나 AWS 키 입력은 없다.

```sh
python3 "$HOME/api-calculation-check.py"
```

파일31,380bytes/SHA256을 먼저 확인하고, Dummy 로그인 → 성인 압박 과정910004/등록920004의 실제 상세 조회 → 미완료 일반 훈련940041 시작 → 반환된 condition6개를 그대로 multipart 전송 → 같은 시도의 계산 결과 조회 → 일반 훈련 완료/최종평가 미완료 확인 순서다. 생성 요청의 UUID·definitionHash는 실제 응답을 사용한다. 프로그램 완료는 이 일반 훈련 완료이며 과정 전체/최종평가 통과나 ARC 제출 성공을 뜻하지 않는다. 다른 팀원이 먼저 완료했거나 도중 진도가 달라지면 초기화하지 않고 중단한다. 미완료 표시만으로 다른 진행 중 일반 훈련이 없음을 보장하지는 않는다.

업로드는202/pending 또는 빠른 처리의200/succeeded 모두 정상이다. 접수 뒤에는 GET만 반복하며, 업로드 시작부터30초를 기준으로 추가 조회를 제한한다. 각 HTTP 작업에는 남은 시간을 timeout으로 전달하지만 DNS·여러 socket read·운영체제 지연까지 엄격한 실시간30초를 증명하는 앱 인수 시험은 아니다. 30초가 지나도 서버 작업을 취소하거나 같은 자료를 자동 재업로드하지 않는다. 이전 계산의 `judg_result` 문자열은 수정하지 않으며 완료 판정은 `evaluation.score.decision=pass`로 확인한다.

3. 성공이면 `result`에 `Compressions=101`, `Ventilations=0`, `OverallScore=100`, `GoalMet=true`, `ProgramCompleted=true`, `ProgressApplied=true`, `SubmitARC=excluded`가 나오고 마지막은 `ALL_PASS/https_recorded_practice_calculation_progress`다. `progress_after`는 PracticeCompleted=true/FinalCompleted=false다. `ChartLinkPresent=true`는 URL 존재 확인만이며 실제 차트 다운로드나 만료 시험은 후속 단계다.

실행은 `$HOME/arc-calc-dev-calculation-check/`에 폴더0700/체크포인트0600을 만든다. 로그인 토큰·시작 UUID/요청·시도ID·복구 증표를 여기에 보존해 응답 유실 때 같은 시험을 확인할 수 있게 한다. 토큰/복구 증표/차트 서명URL·응답 원문은 화면·명령 인자·로그에 쓰지 않는다. **이 폴더의 `state.json`은 비밀을 포함하므로 열어서 대화에 붙이거나 Git/ZIP에 넣지 않는다.** 일반 명령을 다시 실행하면 CHECKPOINT_EXISTS_NO_NEW_ATTEMPT로 멈춰 새 시도를 만들지 않는다. 기존 폴더·체크포인트·서버 자료를 지우고 재시도하지 않는다.

WAITING/RUN_STATUS_ONLY는 아직 처리 중이라는 뜻이다. 다음 명령은 보관된 같은 세션·시도로 **조회만** 이어간다. 새 로그인·시작·업로드·로그아웃·초기화는 없다. 추가 수동 조회는 앞선 앱 대기 한도를 변경하는 것이 아니다.

```sh
python3 "$HOME/api-calculation-check.py" --status
```

STOP이면 원문 파일 대신 화면의 정제된 출력만 확인한다. 특히 생성/업로드 응답 유실은 실제 서버 실행 여부가 불명확하므로 새 시도나 새 업로드를 만들지 않는다. 업로드 요청 이전에 멈춘 체크포인트는 --status도 안전하게 중단하며 별도 복구 검토가 필요하다. 시험 종료는 서버 작업 취소가 아니고 최종평가는 수행하지 않는다. 입력이 JSON text로 바뀌지 않도록 raw bytes와 multipart Content-Type을 사용한다. [API Gateway binary 처리](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-payload-encodings.html)

이 시험의 성공으로 실제 HTTPS multipart 접수·비동기 계산 결과·현재 공유 진도 반영을 확인한다. Stream 전달과 Scheduler 복구 중 어느 경로가 전달했는지는 결과만으로 구별하지 않는다. S3 원본의 독립 hash 검증은 해당 시도의 DDB manifest 참조→S3 원본을 읽어 비교해야 한다. 차트 실제 다운로드/권한/만료, 운용 로그 저장, 최종평가·재전송·소유권·장애 시험, 현재 앱 파일/기기·최대 크기/시간/동시성 인수는 별도로 남는다.

<a id="beginner-api-artifacts-check"></a>
### 첫 압박 계산 통과 다음: 저장 원본·차트·운용 로그 읽기 확인

사용자 출력에서 원본31380bytes/hash 확인, 로그인201, 훈련 시작201, multipart 업로드202, pending 출력2회 뒤 succeeded, 압박101/환기0/점수100/목표 충족/프로그램 완료/진도 반영/Dummy 제외가 모두 PASS였다. 일반 훈련 완료true·최종평가 완료false와 마지막 ALL_PASS/https_recorded_practice_calculation_progress도 확인했다. 이것은 실제 AWS의 첫 계산 성공이며 새 파일 업로드나 훈련 시작을 반복할 필요가 없다.

후속 사용자 실행은 S3 원본31380bytes/hash·manifest/최종 결과 hash·시도/작업 참조·입력 digest/API 결과 일치, 운용 기록16행/1페이지 및 접수/계산 시작/완료 각1건 검증까지 PASS였다. 최초 점검 도구는 `CHART_URL_OUTSIDE_DEV`에서 중단돼 차트 다운로드는 아직 미확인이다. 계산 명령의 재실행이 `CHECKPOINT_EXISTS_NO_NEW_ATTEMPT`로 멈춘 것은 중복 시도를 막는 정상 동작이다. 해당 체크포인트를 삭제하지 않는다.

수정본 v2의 실제 사용자 실행은 global 주소 형식, 차트200/압박101점/저장 hash·독립 차트 일치, 서명 없는 접근403 및 최종 `ALL_PASS/existing_practice_storage_chart_logs`였다. 따라서 일반 훈련 한 건의 원본·최종 결과·운용 로그·차트 다운로드·익명 접근 차단은 확인됐다. 300초 서명 설정 확인과 실제 만료 후 거절은 구별하며 후자는 아직 별도다.

1. 같은 CloudShell에서 기존 `api-calculation-check.py`, `cco_1.bin`, 비공개 `arc-calc-dev-calculation-check/state.json`을 보존한다. Actions → Upload file로 **수정본 `api-artifacts-check-v2.py` 한 파일**만 추가한다. Mac 파일 선택창의 `⌘⇧G`에 `/Users/mac/arc_calculator_api/var/deployment/dev-runtime-20260923-0octlgf9`를 입력해 찾는다. 이 폴더 주소는 CloudShell 명령이 아니다. 기존 점검 파일은 덮어쓰지 않고 새 이름으로 구별한다.

2. 아래 한 줄을 실행한다. 기존 AWS 로그인 권한과 보관된 앱 세션을 사용하며 새 키·로그인을 입력할 필요는 없다.

```sh
python3 "$HOME/api-artifacts-check-v2.py"
```

이 시험은 API GET, STS 계정 조회, 정확한 시도/Job의 DynamoDB GetItem, S3 GetObject, 한정된 시간의 OPS Query만 수행한다. 새 훈련·재계산·로그아웃·초기화·취소·권한 변경은 없다. 기존 체크포인트도 수정하지 않는다. API GET 자체의 진단 기록은 서버의 정상 동작이다.

수정본의 첫 줄에는 `Revision=s3-address-check-v2`가 나온다. `chart_address`의 INFO는 주소 원문 없이 `EndpointForm=regional/global/unrecognized`와 보안 조건의 참/거짓만 보여 준다. 이 Dev 버킷의 정확한 regional/global 주소 두 개만 허용하고 Ohio 서명 범위·객체 경로·300초·TLS·redirect 거절을 유지한다. 주소를 바꿔서 서명을 재사용하지 않으며 서버가 발급한 host로 조회한다. 다른 형식이면 다운로드 전에 중단한다. [AWS S3 주소 형식](https://docs.aws.amazon.com/AmazonS3/latest/userguide/VirtualHosting.html)

3. `stored_artifacts`는 S3의 manifest/원본/최종 결과 bytes를 실제 읽어 크기·SHA256·typed 입력 digest와 DB 참조를 확인한다. `stored_result`는 저장된 계산 JSON과 같은 시도의 API 결과가 자료형까지 일치하는지 검사한다. 원본 비교 기대값은31380bytes와 기존파일SHA256이다. `input_digest`나 S3 ETag를 원본 SHA256 대신 사용하지 않는다.

`ops_logs`는 이 시험의 API calculation_accepted 및 Worker calculation_started/calculation_completed 기록을 검사한다. 현재 체크포인트 시작/완료 UTC 구간에 앞뒤120초를 더하고, 구간 원래 길이2시간 이하·총12페이지·페이지당100행으로 제한한다. 정확한 날짜별 OPS 파티션·SK 범위에서 Query하며 테이블 Scan을 사용하지 않는다. 기록의 hash·정제 schema·관련 attempt/job/session/epoch·페이지 연속성을 확인한다. `RecordsVerified=true`와 세 이벤트 수가 각각1 이상이어야 PASS다. 페이지 예산 초과·손상은 중단하며 로그 누락만 확인되면 FAIL을 표시하고 독립적인 차트 확인을 계속하되 최종 ALL_PASS를 내지 않는다. 로그 문제를 해결하려고 이미 성공한 계산을 다시 실행하지 않는다. [DynamoDB Query](https://docs.aws.amazon.com/cli/latest/reference/dynamodb/query.html)

`chart`는 새 chart-link를 받아 정확한 Dev S3 객체를 메모리로 내려받는다. DB 발행 SHA, 동일 입력을 배포 ZIP 계산기로 생성한 독립 차트의 정규화 SHA, 압박101점·AED 없음이 일치해야 PASS다. 발행 JSON22612bytes의 SHA256은 `2b2f4e4c27e19c0117778ffd880ffead65deeb2f3ca65644e9aaac99bb98d140`, 정규화 JSON20585bytes는 `b614d5625eb70355dfc48f23bf884cb478e576214d9ddb66c4b7785713018803`이다. S3 Content-Type은 application/octet-stream일 수 있어 JSON 본문을 검증한다. 같은 객체를 서명 없이 GET하는 `private_chart`는403 또는404여야 PASS다. 300초 서명 설정 확인과 실제5분 후 만료 거절 시험은 구별하며 후자는 아직 수행하지 않는다.

모두 통과하면 마지막은 `ALL_PASS/existing_practice_storage_chart_logs`다. 화면에는 안전한 상태·개수·일치 여부만 나온다. 서명URL/접근 토큰/복구 증표·DB/S3 원문·CLI 오류 원문은 출력하지 않는다. CLI history가 꺼져 있는지 먼저 확인하고 호출 endpoint·계정·리전·버킷/경로를 고정한다. S3 조회는 예상 소유자와 byte range를 사용하며 자체0700 임시 폴더/0600 파일을 읽은 뒤 정리한다. 기존 체크포인트를 임시파일로 취급하지 않는다. [S3 GetObject](https://docs.aws.amazon.com/cli/latest/reference/s3api/get-object.html)

STOP/FAIL이면 화면 출력만 공유한다. 비공개 state.json이나 DB/S3 응답을 열어서 붙이지 않는다. AWS 로그인 만료·권한 오류와 앱 세션 만료는 다른 문제이므로 코드에 맞춰 재인증/복구를 판단한다. 성공한 훈련이나 원본을 지우고 다시 시작하지 않는다. 이 범위가 통과해도 최종평가, 중복 재전송·소유권·만료·장애 시험, 실제 앱 파일·기기·동시성/최대 크기·비용 확인은 별도로 남는다.

<a id="beginner-api-assessment-check"></a>
### 일반 훈련의 저장·차트 확인 다음: 최종평가와 과정 완료

사용자의 `api-artifacts-check-v2.py`가 ALL_PASS였다. 이제 같은 Dev 성인 압박 과정910004/등록920004의 마지막 평가940042를 시험한다. 이전 일반 훈련940041은 유지한다. 기존 저장소 자료 `cco_1.bin`을 쓰는 Dummy 시험이며 실제 앱에서 새로 수집한 파일 인수는 별도다.

1. 같은 CloudShell의 Actions → Upload file로 **`api-assessment-check.py` 한 파일**을 추가한다. Mac 파일 선택창에서 `⌘⇧G`를 누르고 `/Users/mac/arc_calculator_api/var/deployment/dev-runtime-20260923-0octlgf9` 폴더를 연다. 이 주소는 CloudShell 명령이 아니다. 기존 `cco_1.bin`과 `arc-calc-dev-calculation-check/state.json`을 보존한다.

2. 아래 명령을 한 번 실행한다. 기존 일반 훈련의 유효한 로그인 세션을 재사용하므로 로그인 정보를 다시 입력하지 않는다. 이 명령은 최종평가 한 건을 실제로 시작·업로드하며 성공 시 Dummy 과정 진도를 갱신한다.

```sh
python3 "$HOME/api-assessment-check.py"
```

파일 hash·완료된 일반 훈련의 체크포인트를 검사하고 현재 API에서 일반 훈련 완료/통과·최종평가 미완료·과정 IN_PROGRESS를 확인한다. 이어 최신 definitionHash와 새 clientRequestId로 마지막 평가만 시작해 서버가 돌려준 condition을 그대로 업로드한다. 최종평가 역할은 final_assessment지만 승인된 Dummy 실행 정의의 mode는 training 그대로다. 이를 assessment로 바꾸지 않는다.

일반 훈련의 체크포인트는 읽기만 하고 평가용 `arc-calc-dev-assessment-check/state.json`을 새 폴더0700/파일0600에 따로 보관한다. 세션·시작 요청·실제 시도/복구 증표가 들어 있어 원문을 공유하거나 Git/배포물에 넣지 않는다. 새 로그인/로그아웃/진도 초기화/취소/자동 재업로드는 하지 않는다. 세션 만료·정의 변경·다른 활성 평가·이미 합격한 상태면 중단하며 기존 자료를 지우고 다시 시작하지 않는다.

3. 성공 시 계산 결과는 압박101회/환기0회/전체100/목표 충족/프로그램 완료/진도 반영/Dummy 제출 제외다. 뒤이어 `progress_after`의 PracticeCompleted/FinalCompleted/FinalPassed가 모두 true이고 `course_completion`의 CourseStatus가 FINISHED여야 한다. 마지막은 `ALL_PASS/https_recorded_final_assessment_course_completion`이다. 프로그램 판정의 score.decision과 목표를 검사하며 기존 tester의 judg_result를 임의로 Pass로 바꾸지 않는다.

WAITING/RUN_STATUS_ONLY이면 아래 명령으로 **같은 평가의 조회만** 이어간다. 업로드 시작부터30초를 사용하는 최초 대기와 구별한다.

```sh
python3 "$HOME/api-assessment-check.py" --status
```

일반 명령을 다시 실행해 평가용 체크포인트가 이미 있으면 CHECKPOINT_EXISTS_NO_NEW_ASSESSMENT로 중단한다. --status는 업로드 전 미확정 시작 상태에서 새 요청을 만들지 않고 CHECKPOINT_NEEDS_REVIEW_NO_RETRY로 멈춘다. STOP/FAIL이면 터미널 출력만 확인하고 체크포인트·원본·진도를 삭제하지 않는다. 시작/업로드의 시간 초과는 실제 서버 실행 여부가 불명확하므로 자동 재시도하지 않는다. 이번 단계는 최종평가 계산·과정 완료 확인이며 재응시 차단/소유권/실제 차트 만료/장애/실물 앱·최대 크기·동시성·비용 인수는 별도로 남는다.

<a id="beginner-post-assessment-queues"></a>
### 최종평가 FINISHED 다음: 대기줄·실패 보관함 확인

사용자 실제 평가 실행이101회/100점, 두 항목 완료/평가 통과, 과정 FINISHED와 ALL_PASS였다. **AWS Dev 배포와 성인 압박 샘플의 기본 흐름은 통과**다. 소유권·같은 요청 재전송/중복 전달·실제 차트 만료·장애/복구·경보/비용·실물 앱/용량 검증을 포함한 전체 인수와 구별한다. 앱팀 연결 정보는 [APP_API](APP_API.md)의 AWS Dev 연결 정보에 모았다. 현재 완료된 과정의 공유 진도와 두 체크포인트는 유지한다.

이번 단계는 새 파일 업로드 없이 같은 오하이오 CloudShell에서 아래 두 명령을 각각 실행한다. 요청량을 조회할 뿐 메시지 수신/숨김/삭제/재전송이나 설정 변경은 없다.

1. 계산 작업 대기줄을 확인한다.

```sh
aws sqs get-queue-attributes --region us-east-2 \
  --queue-url 'https://sqs.us-east-2.amazonaws.com/150612770165/arc-calc-dev-calculation-jobs' \
  --attribute-names QueueArn ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible ApproximateNumberOfMessagesDelayed \
  --query 'Attributes.{QueueArn:QueueArn,Waiting:ApproximateNumberOfMessages,Processing:ApproximateNumberOfMessagesNotVisible,Delayed:ApproximateNumberOfMessagesDelayed}' \
  --output json --no-cli-pager
```

2. 실패한 작업의 보관함을 확인한다.

```sh
aws sqs get-queue-attributes --region us-east-2 \
  --queue-url 'https://sqs.us-east-2.amazonaws.com/150612770165/arc-calc-dev-calculation-dlq' \
  --attribute-names QueueArn ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible ApproximateNumberOfMessagesDelayed \
  --query 'Attributes.{QueueArn:QueueArn,Waiting:ApproximateNumberOfMessages,Processing:ApproximateNumberOfMessagesNotVisible,Delayed:ApproximateNumberOfMessagesDelayed}' \
  --output json --no-cli-pager
```

QueueArn이 각각 `arn:aws:sqs:us-east-2:150612770165:arc-calc-dev-calculation-jobs`와 `arn:aws:sqs:us-east-2:150612770165:arc-calc-dev-calculation-dlq`인지 확인한다. Waiting은 아직 가져가지 않은 작업, Processing은 가져가 처리 중인 작업, Delayed는 지연된 작업이다. 다른 시험이 없다면 모두 문자열 `"0"`이 기대값이다. 숫자는 근삿값이며 송신 중단 후에도 적어도1분간 일관되지 않을 수 있다. 양수이면 동일 조회 결과의 변화와 실제 작업 상태를 보고 판단하며 무조건 실패/중복 계산으로 단정하지 않는다. 조회값0은 현재 뚜렷한 적체가 관측되지 않았다는 뜻이며 무손실/장애 복구·경보 완료 증명이 아니다. [AWS GetQueueAttributes](https://docs.aws.amazon.com/cli/latest/reference/sqs/get-queue-attributes.html)

두 출력만 확인한다. 값을0으로 만들려고 큐 삭제·Purge·메시지 수신/삭제·강제 재전송을 하지 않는다. 후속 검증에서도 개인별 접근 장치 신설은 이번 Dev 필수로 다시 요구하지 않고 D94를 유지한다.

2026-09-23 후속 사용자 출력에서 두 큐의 ARN이 일치하고 Waiting/Processing/Delayed가 모두0이었다. 조회 시점에 적체가 관측되지 않았음을 확인했다. 앱팀은 위 APP_API의 Dev 주소로 로그인·과정 조회·완료 상태 표시부터 연결 시험을 진행할 수 있다. 새 세션에서 이전 시도의 결과까지 자동 조회할 수 있는 것은 아니며, 이미 완료된 성인 압박 과정의 공유 진도를 초기화하지 않는다.

<a id="beginner-dev-monitoring-inventory"></a>
### 기본 흐름 통과 다음: 오류 기록·경보 설정 조회

앱 연결 시험과 함께 진행할 AWS 점검이다. 같은 오하이오 CloudShell에서 아래 두 명령을 각각 실행한다. 로그 원문을 출력하거나 경보·알림·보관기간을 변경하지 않는다.

1. 세 함수의 기본 로그 보관함을 조회한다. [AWS 로그 그룹 조회](https://docs.aws.amazon.com/cli/latest/reference/logs/describe-log-groups.html)

```sh
aws logs describe-log-groups --region us-east-2 \
  --log-group-name-prefix '/aws/lambda/arc-calc-dev-' \
  --query 'logGroups[].{Name:logGroupName,StoredBytes:storedBytes,RetentionDays:retentionInDays}' \
  --output json --no-cli-pager
```

확인할 이름은 `/aws/lambda/arc-calc-dev-api`, `/aws/lambda/arc-calc-dev-worker`, `/aws/lambda/arc-calc-dev-relay`다. RetentionDays가 null이면 만료 일수를 따로 설정하지 않은 상태로, N06에 따라 기간을 임의 설정하지 않는다. StoredBytes가0이거나 이름이 빠졌다고 즉시 실행 실패로 단정하지 않고 실제 Lambda 로그 대상과 수집 상태를 확인한다. 이 목록의 존재만으로 실제 오류 기록·민감정보 제외·로그 무누락을 입증하지 않는다.

2. 프로젝트 이름으로 만든 수치 경보의 현재 상태와 연결된 동작을 조회한다. [AWS 경보 조회](https://docs.aws.amazon.com/cli/latest/reference/cloudwatch/describe-alarms.html)

```sh
aws cloudwatch describe-alarms --region us-east-2 \
  --alarm-name-prefix 'arc-calc-dev-' \
  --alarm-types MetricAlarm \
  --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,ActionsEnabled:ActionsEnabled,AlarmActions:AlarmActions}' \
  --output json --no-cli-pager
```

`[]`는 해당 접두사의 수치 경보가 없다는 뜻이며 명령 오류가 아니다. 다른 이름의 공용 경보·복합 경보·로그 경보까지 없다는 뜻으로 확대하지 않는다. 목록이 있어도 감시 대상·기준·알림 수신은 후속 확인이 필요하다. 다음 경보 설정은 이 결과와 사용자가 정한 수신 방법·기준을 바탕으로 준비한다. 비용 조회/예산 알림은 관리자 회신의 FitCloud 경로를 유지하며 계정 전달·실제 설정은 아직 미확인이다.

후속 사용자 출력에서는 세 기본 로그 그룹이 모두 존재하고 StoredBytes0/RetentionDays null이었다. 해당 접두사의 MetricAlarm은 빈 목록이었다. 로그 그룹 존재와 보관기간 미설정만 확인한 것으로, CloudWatch 실제 로그 수집·무누락·오류 없음은 아직 판정하지 않는다. 원문 없이 최근 이벤트·수집 시각을 확인하는 후속 조회가 가능하며, 로그 스트림의 시각은 지연 갱신될 수 있다. [AWS 로그 스트림 조회](https://docs.aws.amazon.com/cli/latest/reference/logs/describe-log-streams.html)

<a id="beginner-dev-email-topic"></a>
### 이메일 알림 준비: SNS 주제와 구독

사용자 D100에 따라 새 이메일 알림 경로를 준비한다. 이번 단계는 주제 생성과 이메일 구독 확인까지이며 경보 기준·연결·실제 장애 알림 시험은 다음 단계다. 이메일 주소는 콘솔에 직접 입력하고 이 문서·CLI 인자·대화에 복사하지 않는다.

1. 작업용 `arc-dev-operator-role` 상태에서 [오하이오 SNS 콘솔](https://us-east-2.console.aws.amazon.com/sns/v3/home?region=us-east-2#/topics)을 연다. 리전이 미국 동부(오하이오)인지 확인한다. 왼쪽 **주제(Topics)** 목록에서 `arc-calc-dev-alerts`를 검색한다. 같은 이름이 이미 있으면 기존 설정·구독을 보존하고 ARN부터 확인하며 새 생성 절차를 반복하지 않는다.
2. 없으면 **주제 생성(Create topic)**을 누른다. 유형은 **표준(Standard)**, 이름은 **`arc-calc-dev-alerts`**로 입력한다. 이메일 직접 구독은 Standard에서 지원된다. 표시 이름은 비워도 된다. 액세스 정책은 주제 소유자 계정으로 제한된 기본값을 유지하고 나머지 선택 설정도 기본값으로 둔다. 태그는 `Project=arc-calc`, `Env=dev` 두 개를 추가하고 **주제 생성**을 누른다. [AWS 주제 생성](https://docs.aws.amazon.com/sns/latest/dg/sns-create-topic.html), [이메일 지원](https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html)
3. 만들어진 주제 ARN이 `arn:aws:sns:us-east-2:150612770165:arc-calc-dev-alerts`인지 확인한다. 왼쪽 **구독(Subscriptions)** → **구독 생성(Create subscription)**에서 이 주제를 선택한다. 프로토콜은 **이메일(Email)**, 엔드포인트는 본인이 알림을 받을 이메일 주소다. 구독 필터·기타 선택 설정은 기본값으로 두고 **구독 생성**을 누른다.
4. 해당 메일함에서 AWS의 확인 메일을 열고, 방금 만든 주제의 요청인지 확인한 뒤 **Confirm subscription**을 누른다. 메일이 안 보이면 스팸함을 확인한다. 확인 링크는 공유하지 않는다. AWS 화면을 새로고침해 확인 대기 표시가 해소됐는지 본다. [AWS 이메일 구독·확인](https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html)
5. CloudShell에서 아래 읽기 명령을 실행한다. 이메일 주소와 확인 링크를 출력하지 않는다.

```sh
aws sns list-subscriptions-by-topic --region us-east-2 \
  --topic-arn 'arn:aws:sns:us-east-2:150612770165:arc-calc-dev-alerts' \
  --query 'Subscriptions[].{Topic:TopicArn,Protocol:Protocol,Subscription:SubscriptionArn}' \
  --output json --no-cli-pager
```

새 구독의 Protocol은 `email`, Topic은 위 ARN, Subscription은 `arn:aws:sns:us-east-2:150612770165:arc-calc-dev-alerts:`로 시작하는 실제 구독 ARN이어야 한다. `PendingConfirmation`이면 아직 확인 대기, `[]`면 이 주제에 구독이 조회되지 않은 상태다. 확인 완료는 구독 준비 증거이며 실제 경보 메일 수신 검증이 아니다. 주제 권한·암호화 설정과 경보 발행 연결을 함께 확인한 뒤 승인된 경보 기준을 적용한다. 실제 AWS 주제/구독 생성과 이메일 확인은 사용자가 수행하며 에이전트가 실행한 것으로 보고하지 않는다.

후속 사용자 출력에서 위 주제의 email 구독 ARN `arn:aws:sns:us-east-2:150612770165:arc-calc-dev-alerts:b4f374e0-b15e-4e0d-8a49-f350b7f35786`을 확인했다. 확인 대기 상태는 해소됐으며 새 주제/구독 생성이나 확인 메일 요청을 반복할 필요는 없다.

<a id="beginner-dev-alarm-preflight"></a>
### 이메일 확인 다음: 경보 발송 권한·구독 필터 조회

같은 CloudShell에서 아래 두 명령을 각각 실행한다. 구독의 Endpoint(이메일 주소)는 출력 항목에서 제외하며 주제 정책·암호화·필터를 읽기만 한다. 기존 설정을 보고 필요한 발행 권한만 보완한다. CloudWatch에 발행 권한을 부여할 때는 정확한 SNS 주제와 이 계정·리전의 Dev 경보 ARN으로 제한한다. [AWS CloudWatch 알림 권한](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Notify_Users_Alarm_Changes.html)

1. 주제의 소유 계정·암호화·발행 권한을 확인한다.

```sh
aws sns get-topic-attributes --region us-east-2 \
  --topic-arn 'arn:aws:sns:us-east-2:150612770165:arc-calc-dev-alerts' \
  --query 'Attributes.{Topic:TopicArn,Owner:Owner,Fifo:FifoTopic,KMS:KmsMasterKeyId,Confirmed:SubscriptionsConfirmed,Pending:SubscriptionsPending,Policy:Policy}' \
  --output json --no-cli-pager
```

2. 이미 확인된 이메일 구독의 필터·확인 상태를 확인한다.

```sh
aws sns get-subscription-attributes --region us-east-2 \
  --subscription-arn 'arn:aws:sns:us-east-2:150612770165:arc-calc-dev-alerts:b4f374e0-b15e-4e0d-8a49-f350b7f35786' \
  --query 'Attributes.{Subscription:SubscriptionArn,Topic:TopicArn,Owner:Owner,Protocol:Protocol,Pending:PendingConfirmation,Filter:FilterPolicy,FilterScope:FilterPolicyScope,Redrive:RedrivePolicy}' \
  --output json --no-cli-pager
```

`Policy`에 따옴표 앞 역슬래시가 보이는 것은 JSON 문자열의 정상 표기다. `null`은 해당 속성이 응답에 없는 것이며 오류 자체가 아니다. KMS 키가 있으면 CloudWatch 발행에 필요한 키 권한도 함께 확인하고 임의로 암호화를 끄지 않는다. 필터가 있으면 실제 경보 내용이 통과하는지 확인하며 기존 필터를 무조건 삭제하지 않는다. [SNS 주제 속성](https://docs.aws.amazon.com/cli/latest/reference/sns/get-topic-attributes.html), [구독 속성](https://docs.aws.amazon.com/cli/latest/reference/sns/get-subscription-attributes.html)

사용자 D101에 따라 초기 경보 기준은 **오류/호출 제한은5분1회 이상, DLQ는1분1건 이상**이다. 이를 Lambda 세 함수 각각 Errors/Throttles 5분 Sum≥1(6개), API Gateway `arc-calc-dev-rest-api`/`dev`의 5XXError 5분 Sum≥1(1개), 계산 DLQ ApproximateNumberOfMessagesVisible 1분 Maximum≥1(1개)의 총8개로 준비한다. 각1구간 충족, 데이터 없음은 notBreaching으로 처리하는 초기 구성이다. 실제 생성·저장 확인은 후속 단계다. API Gateway는 ApiName/Stage 차원의 기본 지표를 사용하며 상세 메서드 지표·본문 로그를 켜는 작업이 아니다. [Lambda 지표](https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics-types.html), [API Gateway 지표](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-metrics-and-dimensions.html), [DLQ 지표](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html)

이는 최소 인프라/HTTP 오류 감시다. Worker/Relay의 정상 반환에 포함된 부분 실패, DB에 확정 저장한 계산 실패, 미전달 Outbox와 장기 지연을 모두 잡는 경보가 아니며 Lambda Throttles는 Gateway429와 다르다. 데이터 없음의 정상 처리는 수집 중단을 감시하지 않는다. 구독 확인·정책 저장·경보 생성과 실제 CloudWatch→SNS→이메일 수신은 각각 검증한다. 이 단계의 조회 결과만으로 경보 설치/수신 완료를 보고하지 않는다.

후속 사용자 출력에서 SNS 주제/소유 계정 일치, 확인된 구독1/대기0, Fifo/KMS 미설정과 정확한 email 구독의 확인 완료·필터/범위/Redrive 미설정을 확인했다. 주제 정책은 Version2008-10-17의 기본 Statement1개이며 Principal AWS:*에 `AWS:SourceAccount=150612770165` 조건이 있었다. 이를 SourceOwner 정책으로 바꿔 기록하거나 조건 없는 공개 정책으로 해석하지 않는다.

<a id="beginner-dev-create-alarms"></a>
### 초기 경보 8개 생성

사용자 D100·D101을 적용하는 도구는 `var/deployment/dev-runtime-20260923-0octlgf9/create-dev-alarms-v3.py`다. v2 사용자 실행은 CLI2.36.47의 `CLOUDWATCH_MODEL_SKELETON`에서 종료252로 중단됐다. 로컬 실제 CLI2.36.44에서도 같은 검사 명령이 만든 예시 응답의 0값·필수 선택 항목 때문에 실패함을 재현했다. **v3는 이 예시 생성 검사를 제거하고 실제 경보 ARN 조회로 존재를 확인한다.** CLI 버전·실패 명령·종료 코드·정해진 오류 분류만 표시하며 명령 인자나 오류 원문은 출력하지 않는다. 후속 사용자 실행은 READY_TO_CREATE→경보8개 생성→SNS 정책 보존·구독1개 확인→8개 설정 검증→ALARMS_CONFIGURED를 통과했다. 초기 상태는 모두 INSUFFICIENT_DATA였으며 실제 메일 수신은 아직 미확인이다. 이미 설치됐으므로 최초 생성 명령을 반복하지 않는다.

설치 실행 중에는 같은 SNS 주제 정책과 아래 경보를 다른 터미널/콘솔에서 동시에 편집하지 않는다. PutMetricAlarm에는 이 도구가 사용할 원자적 생성 전용 기능이 없어, 사전 조회와 재조회만으로 동시 편집 충돌을 완전히 막을 수 없다.

| 새 경보 이름 | 감시 대상 | 기준 |
|---|---|---|
| `arc-calc-dev-api-errors` | API Lambda 실행 오류 | 5분 합계1회 이상 |
| `arc-calc-dev-api-throttles` | API Lambda 호출 제한 | 5분 합계1회 이상 |
| `arc-calc-dev-worker-errors` | Worker Lambda 실행 오류 | 5분 합계1회 이상 |
| `arc-calc-dev-worker-throttles` | Worker Lambda 호출 제한 | 5분 합계1회 이상 |
| `arc-calc-dev-relay-errors` | Relay Lambda 실행 오류 | 5분 합계1회 이상 |
| `arc-calc-dev-relay-throttles` | Relay Lambda 호출 제한 | 5분 합계1회 이상 |
| `arc-calc-dev-api-5xx` | REST API의 dev stage 서버 오류 | 5분 합계1회 이상 |
| `arc-calc-dev-calculation-dlq-visible` | 계산 실패 보관함의 대기 메시지 | 1분 최댓값1건 이상 |

1. CloudShell의 **작업(Actions) → 파일 업로드(Upload file)**를 누른다. Mac 파일 선택창에서 `⌘⇧G`를 누르고 `/Users/mac/arc_calculator_api/var/deployment/dev-runtime-20260923-0octlgf9/`를 입력해 새 `create-dev-alarms-v3.py`만 홈 디렉터리에 올린다. Mac 경로를 CloudShell 명령으로 실행하지 않는다.
2. 같은 오하이오 CloudShell에서 아래 읽기 전용 명령을 한 번 실행하고 출력을 확인한다. 끝의 `--status`를 포함한다.

```sh
python3 "$HOME/create-dev-alarms-v3.py" --status
```

이 실행은 경보를 만들거나 설정을 바꾸지 않는다. 마지막 결과가 `READY_TO_CREATE`면 계정·API·SNS 설정 확인과 경보8개 부재 확인을 통과한 것이다. `ALARMS_CONFIGURED`면 이미 설치된 경보의 설정 검증을 통과한 것이므로 새로 생성하지 않는다. `STOP`이면 표시된 `LastOperation`·`CLIExitCode`·`ErrorClass`와 전체 출력을 확인하고 설치를 진행하지 않는다. 종료 코드만으로 세부 원인을 단정하지 않는다. [AWS CLI 종료 코드](https://docs.aws.amazon.com/cli/latest/userguide/cli-usage-returncodes.html)

3. **`READY_TO_CREATE`가 나온 경우에만**, 아래 최초 생성 명령을 한 번 실행한다. 같은 자원을 다른 창에서 동시에 편집하지 않는다. 도구가 사전 조회를 다시 수행하고 경보8개를 생성한다.

```sh
python3 "$HOME/create-dev-alarms-v3.py"
```

도구는 CLI history 비활성·AWS 계정과 고정 리전/endpoint, SNS 주제·이미 확인된 구독·기존 정책, REST API/Dev stage 및 같은 경보 이름의 부재를 확인한다. SNS 정책은 사용자 제공 기본 정책과 일치하는지 검증하고 변경하지 않는다. 정책의 Principal AWS:*·SNS:Publish·정확한 SourceAccount 조건과 CloudWatch의 SourceAccount 지원에 근거해 같은 계정 발행을 이미 포괄한다고 판단했으며, 중복 허용 문장을 추가하지 않는다. 실제 발행/수신 검증과는 구별한다. [SNS 조건 지원](https://docs.aws.amazon.com/sns/latest/dg/sns-access-policy-use-cases.html), [Principal 의미](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_principal.html)

첫 생성 전8개 전체와 각 경보 생성 직전에 정확한 경보 ARN의 `ListTagsForResource`를 조회한다. 성공이면 태그가 비어 있어도 기존 경보가 있다는 뜻이다. 해당 조회의 정확한 `ResourceNotFoundException`만 부재로 인정하며 권한·세션·통신·기타 오류는 중단한다. 이 조회는 경보 유형에 공통인 ARN을 사용하므로 CLI가 LogAlarm 응답을 해석하지 못해도 충돌을 놓치지 않는다. 설치 후에는 MetricAlarm 설정·태그를 확인하고, 상태 조회에서 MetricAlarm이 없는데 ARN이 존재하면 불일치로 처리한다. [경보 ARN·부재 응답](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_ListTagsForResource.html) 생성 요청 JSON은 이 도구의 비공개 임시 파일로 전달한다. SNS 정책·구독·암호화, Lambda/API/SQS 설정에는 쓰기 요청을 하지 않는다.

새 경보는 Project=arc-calc/Env=dev 태그와 이 SNS 주제만의 ALARM 알림 동작을 갖는다. OK/데이터 부족 알림이나 서버 재시작/중지/삭제 동작은 없다. 기존 메트릭이 기준을 충족하면 생성 직후 실제 경보 메일이 올 수 있다. 테스트 메시지 전송·메트릭 주입·강제 경보 상태 변경은 이 도구에 포함하지 않는다.

마지막 `ALARMS_CONFIGURED`는 보존된 SNS 설정과 경보8개의 저장 설정·태그를 재조회해 일치를 확인했다는 뜻이다. 초기 `INSUFFICIENT_DATA`는 평가 전 상태일 수 있으며 `ALARM`이면 실제 감시 수치를 확인해야 한다. `OK` 역시 이 도구가 모든 업무 오류나 실제 메일 수신까지 검증했다는 뜻은 아니다. [AWS 경보 생성/평가](https://docs.aws.amazon.com/cli/latest/reference/cloudwatch/put-metric-alarm.html)

4. STOP/오류/시간 초과면 일반 설치 명령을 반복하지 않는다. 일부 경보가 이미 저장됐을 수 있으므로 출력부터 확인한다. 현재 설정만 확인하려면 아래 읽기 전용 명령을 사용한다.

```sh
python3 "$HOME/create-dev-alarms-v3.py" --status
```

도구는 조회에서 발견한 기존 경보의 덮어쓰기를 중단하고, 실패 뒤 생성된 자원을 삭제하지 않는다. 조회 직후 다른 관리자가 같은 이름을 생성하는 경쟁 조건까지 원자적으로 막을 수는 없다. 앞서 완료한 훈련·평가 체크포인트도 보존한다. 다음 단계는 실제 CloudWatch 알림 수신 시험이며, 서버를 고장 내거나 훈련 작업을 재실행하는 방식으로 시험하지 않는다.

<a id="beginner-dev-alarm-email-test"></a>
### 설치된 경보의 이메일 수신 확인

앞 단계의 ALARMS_CONFIGURED는 경보8개의 저장 설정·태그·알림 연결 확인이다. 다음은 기존 `arc-calc-dev-calculation-dlq-visible` 경보의 표시 상태를 한 번 시험용 ALARM으로 바꿔 CloudWatch→SNS→이메일 경로를 확인한다. 이는 AWS가 제공하는 시험 기능이며 지표·큐 메시지·계산 함수는 변경하지 않는다. MetricAlarm은 실제 지표 평가로 빠르게 다시 바뀔 수 있으므로 화면 상태만 보지 않고 이력과 메일을 확인한다. 수동 OK 복원은 하지 않는다. [AWS 경보 시험 기능](https://docs.aws.amazon.com/cli/latest/reference/cloudwatch/set-alarm-state.html)

1. 오하이오 CloudShell에서 현재 상태와 동작을 읽는다. 앞서 v3가 검증한 경보의 설정을 다른 창에서 동시에 편집하지 않는다.

```sh
aws cloudwatch describe-alarms --region us-east-2 \
  --alarm-names arc-calc-dev-calculation-dlq-visible \
  --alarm-types MetricAlarm \
  --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Enabled:ActionsEnabled,SendTo:AlarmActions,OnOK:OKActions,OnNoData:InsufficientDataActions}' \
  --output json --no-cli-pager
```

한 경보가 나오고 State가 OK 또는 INSUFFICIENT_DATA, Enabled가 true, SendTo가 `arn:aws:sns:us-east-2:150612770165:arc-calc-dev-alerts` 하나, OnOK/OnNoData가 빈 배열인지 확인한다. 이미 ALARM이거나 다른 설정/오류/빈 목록이면 시험을 진행하지 않고 해당 출력을 확인한다. 실제 ALARM을 먼저 OK로 바꾸지 않는다.

2. 위 조건을 충족하면 시험 알림을 한 번 발생시킨다. 성공 시 아무 출력 없이 프롬프트로 돌아올 수 있다. 이 단계부터 실제 구독 이메일로 알림이 전달될 수 있다.

```sh
AWS_MAX_ATTEMPTS=1 aws cloudwatch set-alarm-state --region us-east-2 \
  --alarm-name arc-calc-dev-calculation-dlq-visible \
  --state-value ALARM \
  --state-reason 'TEST ONLY: ARC Dev CloudWatch to SNS email delivery check. No actual failure was induced.' \
  --no-cli-pager
```

3. 최근 알림 실행 기록을 읽고 받은 편지함·스팸함을 확인한다. 해당 경보 이름과 TEST ONLY 이유를 가진 메일이 도착했는지 확인하며 전체 메일 본문이나 구독 해지 링크를 공유할 필요는 없다.

```sh
aws cloudwatch describe-alarm-history --region us-east-2 \
  --alarm-name arc-calc-dev-calculation-dlq-visible \
  --alarm-types MetricAlarm \
  --history-item-type Action --scan-by TimestampDescending \
  --max-items 5 --page-size 5 \
  --query 'AlarmHistoryItems[].{Time:Timestamp,Summary:HistorySummary}' \
  --output json --no-cli-pager
```

시험 시각의 SNS 동작 성공 기록은 CloudWatch의 발행 확인이며 이메일 수신함 도착 확인과는 구별한다. 출력이 비어 있거나 시간 초과·동작 실패·미수신이면 즉시 시험을 반복하지 않고 기록부터 확인한다. 이 조회는 최근5개이며 전체 이력을 증명하지 않는다. 메일을 확인해도 실제 오류·큐 적체 발생에서의 지표 탐지나 모든 경보의 개별 수신까지 시험한 것은 아니다. [경보 실행 기록](https://docs.aws.amazon.com/cli/latest/reference/cloudwatch/describe-alarm-history.html)

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

기존 두 도구는 commercial `aws` partition만 지원하며 `aws-cn`/`aws-us-gov`는 사전 거절한다. 실제 STS 계정·기존 Role/함수·`lambda_handler.run`을 확인하고 Lambda는 기존 Runtime=`python3.12`, PackageType=`Zip`도 확인한다. 다른 Handler·새 자원·광범위 Invoke 권한을 자동 생성해 우회하지 않는다. Lambda 도구는 전체 환경을 교체하며 코드→설정→버전 발행은 단일 거래가 아니다. Gateway는 현재 별칭 없는 함수를 호출하므로 갱신 중에도 새 코드가 요청을 받을 수 있다. 자동 원복을 가정하지 않는다.

Gateway 도구의 throttle은 해당 POST만 변경하지만 binaryMediaTypes는 REST API 전체에, create-deployment는 Stage에 영향을 준다. 완료 메시지는 전체 Journey 인수 성공이 아니다. 로그 retention 숫자는 기존 로그를 삭제 대상으로 만들 수 있으며 null은 기존 정책 유지다. 실제 custom LogGroup을 조회해 적용하고 공유 함수 영향을 확인한다.

[기존 배포 workflow](../.github/workflows/deploy_arc_lambdas.yml)는 **수동 실행만** 한다. Dev는 `develop`, Prod는 `main`만 허용하며 같은 revision의 오프라인 회귀를 AWS 자격증명 설정 전에 실행한다. GitHub Environment는 `development`/`production`으로 연결한다. 코드에 이름을 넣는 것만으로 required reviewer 승인이 생기지는 않으므로 GitHub 설정에서 reviewer·허용 branch·Secrets를 확인한다. Environment 사용 시 OIDC trust의 subject도 실제 저장소 구성과 맞아야 한다. [GitHub OIDC 안내](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws). 과거 IAM JSON·장기 키 fallback을 그대로 활성화하지 않는다. 이 workflow는 기존 Calculator 코드·설정만 갱신하며 Gateway 경로 변경은 별도다. 이번 세 역할·`/api/v2` 전체 설치를 대신하지 않는다.

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
