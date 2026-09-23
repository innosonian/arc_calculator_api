# 검증 상태와 재현 방법

최신 전체 회귀: **2026-09-22 AWS Dev Dummy 배포 준비 고도화 후 로컬 전체 3312 passed, 9 subtests passed**, exit 0. 이번 검토는 `/api/v2`·공유 Dummy Dev 범위이며 아래의 2026-09-18 AI 5인 인수 기록과 구별한다. **소스·배포 ZIP 준비 검증과 사용자 실행 AWS HTTPS 로그인·조회, 기존 압박 파일 한 건의 실제 계산·일반 훈련 완료·진도 반영, S3 원본·최종 결과 일치와 운용 로그·서명 차트 다운로드·익명 접근 차단은 통과했다. 동일 파일의 최종평가 합격·두 항목 완료/통과·과정 FINISHED도 확인했다. 소유권·재전송·실제 만료·장애·실물 앱·용량/비용 등을 포함한 전체 AWS 인수는 미완료**다. 2026-09-23 사용자의 자원 생성·실행 출력과 에이전트의 오프라인 설정 검증은 아래에 구별한다.

이는 사람의 인증·실물 앱 인수·AWS 운영 승인·ARC 공식 승인이 아니다. 기본 서버는 `/mock/v1`, 새 과정 모델은 명시 조립한 `course_v2`/`/api/v2`다. 실제 외부 송신은 비활성이고 에이전트가 기본 서버 전환·commit·push·AWS 배포를 수행하지 않았다.

현재 구조는 [ARCHITECTURE](ARCHITECTURE.md), 확정 정책과 미정 계약은 [DECISIONS](DECISIONS.md), 앱 연결 계약은 [APP_API](APP_API.md), 실행 환경 준비는 [LOCAL_RUN](LOCAL_RUN.md)을 따른다. 이 문서는 현재 재현 가능한 시험과 최신 검토 결과만 유지한다.

## 2026-09-23 Lambda 설정 준비

사용자가 세 Lambda 생성·ZIP 업로드·Handler 설정 완료를 보고하고 D95의 메모리/시간/약2MB 초기 업로드안을 선택했다. 주 검토자와 별도 AWS 설정 담당 AI가 역할별 숫자를 대조했다. `var/deployment/dev-runtime-20260923-0octlgf9/`에 압축 JSON 세 장, 새 API 전용 복구 키, 비밀 없는 설정 요약, 후속 Relay 조건부 최초 생성 요청을 준비했다. 폴더0700/파일0600이며 Git에서 제외하고 기존 키·파일은 덮어쓰지 않았다.

- 실제 생성 파일의 세 역할 schema와 묶음 검사가 `dummy_dev_bundle_valid` 통과. API30/Worker120/Relay60초, SQS visibility720초/batch window0초를 대조했다.
- Relay의 설정상 획득8초+한 항목36초+종료 여유4초=48초로 60초 안에 들어간다. SDK/DNS/실제 AWS 전체 처리시간 보장은 아니다.
- 업로드 대상으로 확인한 기존 ZIP에서 runtime과 의존성을 import하고 네트워크 audit 차단 및 SDK 대역으로 API/Worker/Relay를 모두 조립했다. 실제 AWS 접근은 없었다.
- 환경 변수 key/value UTF-8 합계는 API1338·Worker1344·Relay741 bytes로 4096bytes 이하. API 키는 값 출력 없이 버전/32bytes 형식만 검사했다. `ARC_MOCK_ENABLED=false`는 콘솔 준비용이며 로컬 대역 조립에서만 true를 사용했다.
- Relay 최초 행 요청의 `attribute_not_exists` 조건과 준비한 IAM 정책의 정확한 진행 PK 일치를 확인했다. 실제 DynamoDB PutItem은 수행하지 않았다.

코드/ZIP 변경은 없고 이번에는 설정·문서만 준비했으므로 전체 회귀를 재실행하지 않았다. 실제 콘솔 설정 저장·IAM·로그 전달·앱 파일 크기/계산시간·AWS 전체 호출은 다음 단계에서 확인한다.

사용자는 이어서 메모리·시간·환경 변수 설정 완료를 보고했다. 다음 단계용 `worker-trigger-disabled.json`을 같은 비공개 폴더에 추가했다. 별도 AI가 Worker/Stream handler·부분 실패·비활성 동작을 검토했고, 주 검토자가 최초 PutItem 및 Worker 연결 요청의 핵심 필드를 배포 ZIP의 botocore 서비스 모델로 검사했다. 오래된 모델에 없는 연결 `Tags` 필드는 최신 AWS CLI 공식 명세와 따로 대조했다. 작업 큐/Worker 계정·리전 binding, 배치1/대기0/부분 실패/비활성을 확인했다. 실제 CloudShell 실행·최초 행 생성·트리거 생성은 에이전트가 수행하지 않았다. DynamoDB Stream과 예약은 후속 단계이며 이 준비를 전체 작업 전달 완료로 해석하지 않는다.

사용자가 Worker mapping UUID `eef89e6e-b897-40e9-885f-87ebeddbf094` 생성 결과와 `State=Disabled`, `BatchSize=1`, `FunctionResponseTypes=[ReportBatchItemFailures]` 조회 결과를 붙여넣었다. 이는 사용자 제공 AWS 출력이며 에이전트의 직접 조회가 아니다. 후속 `relay-trigger-disabled.json`을 준비하고 기존 서비스 모델의 핵심 요청 schema·역할/계정/리전·IAM Stream 범위·키 필터를 확인했다. 별도 AI가 KEYS_ONLY·INSERT 코드 검사·SequenceNumber 부분 실패·로그/진행 행 제외·이전 Stream mapping 확인 순서를 독립 검토했다. 실제 Stream ARN은 아직 제공되지 않아 파일에서 생략하고 CloudShell 조회값을 명령 인자로 넘기도록 했다. D96의 1분 복구 점검은 사용자 선택만 완료했으며 예약 생성은 미실행이다. 코드/ZIP 변경은 없다.

후속으로 사용자가 제공한 DescribeTable 출력에서 `arn:aws:dynamodb:us-east-2:150612770165:table/arc-calc-dev-training`은 일치했으나 StreamEnabled/StreamViewType/LatestStreamArn이 모두 null이었다. 따라서 앞선 테이블 생성 완료 보고를 Stream 구성 완료로 취급하지 않고 KEYS_ONLY 활성화 및 ACTIVE/주소 재조회 절차를 보완했다. 이후 사용자가 Relay mapping UUID `94038f0a-a725-4324-ba22-b14bdc2fc6d7`, Source `arn:aws:dynamodb:us-east-2:150612770165:table/arc-calc-dev-training/stream/2026-09-23T02:57:25.829`, `Disabled`, 배치1, 부분 실패와 `OUTBOX#`/`DISPATCH` 필터를 포함한 조회 결과를 전달했다. 이 출력의 연결 설정은 준비값과 일치하며 실제 실행·전달 성공은 아직 확인하지 않았다.

예약 단계는 최신 AWS 권고의 EventBridge Scheduler로 준비한다. 별도 AI가 `source=aws.events` 명시 입력, 비동기 호출과 실제 실행 성공의 구별, schedule-group에 제한한 신뢰 정책, 호출 전용 역할을 검토했다. `scheduler-trust.json`과 `scheduler-invoke-relay.json`을 기존 비공개 폴더에 새로 준비했으며 CreateRole/PutRolePolicy/CreateScheduleGroup 요청 형식을 SDK 모델로 검사했다. 기존 파일·역할은 변경하지 않았고 AWS 생성/호출은 수행하지 않았다. 사용자는 이후 정확한 Relay ARN의 `lambda:InvokeFunction` 인라인 정책 조회 결과를 전달했다.

사용자 D97(예약 추가 전달 재시도0회) 확정 후 `relay-schedule-disabled.json`을 새 0600 파일로 준비했다. SDK CreateSchedule 입력 형식, 그룹/역할/대상 IAM 일치, 1분/OFF/DISABLED/재시도0을 검사했다. 업로드한 ZIP의 `dispatch.run`에 실제 Target.Input을 주고 네트워크 차단·대역 경계에서 `reconcile`이 한 번 선택됨을 확인했다. 별도 AI가 최종 예약 파일과 IAM을 교차 검토해 차단 문제를 찾지 못했다. 전체 회귀 재실행이나 AWS 예약 생성·호출은 하지 않았으며 실제 get-schedule 결과·활성화/권한/업무 실행은 후속 인수다.

이후 사용자가 `get-schedule` 출력으로 `arc-calc-dev-relay-reconcile`의 DISABLED/1분/OFF/재시도0과 정확한 Relay·Scheduler 역할 ARN, `{"source":"aws.events"}` 입력을 전달했다. 사용자 제공 설정은 준비값과 일치하며 실제 실행 성공은 아직 미검증이다. 다음 단계용 새 Regional REST API의 이름 중복 조회·생성 안내를 배포 문서에 추가했다. 별도 AI가 REST 이벤트/바이너리 형식과 생성 범위를 검토했고, 주 검토자는 배포 ZIP의 SDK CreateRestApi 요청 schema, 두 shell 블록 구문, 조회·출력 JMESPath를 오프라인 검사해 통과했다. 코드/ZIP 변경이나 AWS 호출은 없었다. 실제 API ID, Lambda 연결·stage 배포·호출량, 경로 끝 슬래시 및 원본 바이너리 보존은 후속 확인 대상이다.

사용자는 이어서 API ID `2ftxmdtrx1`, `arc-calc-dev-rest-api`, Regional·multipart·Project/Env 태그 일치 생성 결과를 전달했다. 같은 비공개 설정 폴더에 새 0600 파일 `api-gateway-v2-integration.json`을 준비했다. 단일 `ANY /api/v2/{proxy+}`의 `aws_proxy`/POST와 정확한 API Lambda URI, 29000ms, multipart를 확인했다. SDK PutRestApi/GetResources/AddPermission/PutIntegration 요청 형식, dev stage·경로로 제한한 호출 권한의 허용/제외 예, 안내 shell4개·JMESPath3개가 오프라인 검사를 통과했다. 별도 AI가 실제 파일과 공식 가져오기/권한 문서를 검토해 연결 준비의 차단 문제를 찾지 못했다. 가져오기 응답200 정의가 Lambda proxy의 실제 상태를 강제하지 않음도 확인했다. 이는 OpenAPI의 AWS 실제 수용 검증이나 API/Lambda 실행 인수가 아니며, 사용자가 병합·권한 추가 뒤 저장 결과를 확인해야 한다. 코드/ZIP·기존 키·AWS 자원은 에이전트가 변경하지 않았다.

사용자는 후속 AWS 출력으로 API `2ftxmdtrx1`의 merge 성공(Warnings=null), 계정150612770165·dev stage·`/api/v2/*`에 제한한 API Lambda 호출 권한, `ANY /api/v2/{proxy+}`의 AWS_PROXY/POST/정확한 Lambda URI/29000ms를 전달했다. 이 설정은 준비한 파일·권한과 일치하며 stage 배포·실제 호출 성공 증거는 아니다. 다음 읽기 안내는 실제 GSI1 타입/상태, 정확한 Relay 진행 행, SQS visibility/redrive/암호화/근사 메시지 수, 세 Lambda의 역할·handler·용량·코드 해시·비밀 없는 환경값을 대상으로 한다. 별도 AI가 미확인 항목과 코드상 진행 행 검증 조건을 검토했고, 주 검토자는 SDK 요청 형식·shell4개·JMESPath3개·진행 키·합성 비밀값의 출력 제외·업로드 ZIP 해시를 오프라인 검사해 통과했다. AWS 조회/변경·전체 회귀 재실행은 하지 않았다. Gateway 요청량 제한은 사용자에게 선택을 요청했으며 답변 전 숫자를 확정하지 않았다.

그 뒤 사용자의 DescribeTable은 CloudShell container-role 자격증명 metadata500 오류로 결과를 얻지 못했다. 후속 첨부에서는 세 Lambda 설정 조회(동일 결과 두 번), Relay GetItem, SQS 속성 조회가 성공했다. 첨부 JSON을 오프라인 대조한 결과 세 함수 역할/Handler/Python3.12/x86_64/메모리/시간/Active/Successful/동일 ZIP 해시/false/dev/Ohio가 모두 준비값과 일치하고, Relay 행은 초기화 artifact의 Item과 전체가 동일했다. SQS visibility720, 정확한 DLQ/maxReceiveCount5, SSE-SQS=true, 대기/처리 중 근사 수0도 확인했다. 별도 AI가 첨부를 독립 검토해 같은 결론을 냈다. 이 성공 조회들 이후에는 CloudShell 재시작을 요구하지 않는다. 이번 조회 묶음에서 테이블/GSI1 구조 결과만 아직 없어 실패했던 DescribeTable만 다시 안내한다. 사용자 제공 AWS 출력 대조이며 에이전트의 직접 AWS 접근, 런타임 JSON/비밀 키 내용 검사, S3·Lambda 실행 인수를 뜻하지 않는다.

사용자가 다시 전달한 DescribeTable에는 테이블/GSI1 ACTIVE, PK/SK/GSI1PK=S·GSI1SK=N, 정확한 HASH/RANGE 키, ALL projection, KEYS_ONLY와 기존 Relay mapping의 Stream ARN이 모두 일치했다. 이에 Relay만 환경 변수 true로 바꾼 후 실제1회 동기 호출하는 안내를 준비했다. 별도 AI가 실제 코드의 초기 빈 점검 결과와 진행 행 revision6/fence1 도달 조건을 검토했다. 주 검토자는 SDK Invoke/GetFunctionConfiguration/GetItem 요청 형식, shell4개/JMESPath3개, source 입력·RequestResponse·LogType None·정확한 진행 키 및 FunctionError 출력 보존을 오프라인 검사해 통과했다. counts0만으로 성공을 확정하지 않고 실행 후 진행 행을 함께 확인하도록 했다. 사용자 실행 전이며 AWS 함수 활성화·호출/진행 행 변경을 에이전트가 수행한 것은 아니다. 빈 점검으로 SQS 송신·실계산·S3·로그 저장 검증을 대체하지 않는다.

사용자는 Relay를 true로 저장한 결과(Active/Successful), 최초 동기 실행200/FunctionError=null/세 처리 건수0, 실행 후 revision6/fence1/owner NULL/lease0/next OUTBOX/두 scans NULL을 전달했다. 이는 최초 빈 OUTBOX/JOB 점검과 진행 행 조건부 갱신·lease 해제의 기대값에 일치하여 **사용자 제공 출력 범위의 Relay 빈 점검은 통과**다. 실제 SQS 송신·Worker 계산·S3·운용 로그 저장은 아직 별도다. 후속 Worker 빈 Records 호출과 기존 SQS mapping 활성화 안내를 준비했고 별도 AI가 조립 시험의 범위 및 Enabled 조회를 검토했다. 주 검토자는 SDK 요청4종·shell5개·JMESPath4개·정확한 mapping UUID와 빈 입력을 오프라인 검사해 통과했다. 새 mapping/메시지 생성·큐 삭제·AWS 호출은 에이전트가 하지 않았고, Worker 실제 시작·연결 활성화 결과는 사용자 실행 후 확인한다.

사용자는 이후 Worker true/Active/Successful, 빈 Records 동기 호출200/FunctionError=null/`batchItemFailures=[]`를 전달해 **Worker 시작 시험이 통과**했다. SQS mapping 활성화 요청 및 후속 두 조회는 아직 Enabling이며, 대상 Worker/큐·배치1·대기0·부분 실패·필터 없음은 일치한다. 경과 시간이 제공되지 않았으므로 장애나 완료로 단정하지 않았다. LastResult=null도 독립적인 실패 증거가 아니다. 별도 AI와 판정을 대조하고 상태/최근 변경 정보를 읽는 후속 명령을 안내한다. mapping Enabled·실제 메시지 처리 성공은 아직 확인되지 않았다.

후속 사용자 조회에서 Worker mapping은 Enabled, Reason=USER_INITIATED, LastResult=null, LastModified=2026-09-23T04:42:30.102000+00:00으로 확인됐다. 연결 활성화 완료와 실제 계산 메시지 처리 완료를 구별한다. 다음 Relay Stream 활성화·조회 및 Scheduler의 현재 갱신 가능 필드 전체 조회 명령을 준비했다. 별도 AI 검토와 AWS 공식 명세에 따라 Scheduler는 최신 값을 보존하는 전체 갱신 요청을 준비해야 하므로 과거 생성 파일로 바로 덮어쓰지 않는다. 이 시점에서는 Relay Stream 활성화 결과·예약의 현재 선택 필드 및 활성화·자동 실행은 사용자 후속 확인 대상이다.

최신 사용자 출력에서 Relay Stream mapping은 Enabled, Reason=User action, 정확한 함수/Stream·배치1·대기0·부분 실패·기존 키 필터, LastResult=No records processed로 확인됐다. 이는 연결 활성화 증거이며 실제 새 작업 전달 성공은 아니다. Scheduler 전체 조회는 DISABLED/1분/UTC/OFF/NONE, 정확한 역할·함수·입력, 추가 재시도0/최대 사건 나이86400, 날짜/KMS null이었다. 이 현재값을 기준으로 같은 비공개 폴더에 새0600 파일 `relay-schedule-enabled.json`을 준비했다. 모든 non-null 갱신 필드를 보존하고 State만 ENABLED로 변경했으며 별도 AI가 실제 파일을 독립 대조했다. SDK 요청3종·shell3개·JMESPath2개·정확한 진행 키·파일 권한/Git 제외·문서 diff 공백 검사를 통과했다. 코드/ZIP·AWS 자원은 변경하지 않았고, 실제 활성화 저장 및 수동 호출 없는 자동 진행 증가는 사용자 후속 확인 대상이다.

후속 사용자 출력에서 Scheduler는 ENABLED이고 실제 저장된1분/UTC/OFF/재시도0/최대 사건 나이86400/정확한 역할·함수·입력이 준비값과 일치했다. 수동 재호출 없이 확인하는 안내에 대한 진행 행 출력은 revision54/fence9로 이전6/1보다 증가했고 owner NULL/lease0/next OUTBOX/두 scans NULL이었다. **사용자 제공 출력 범위의 자동 빈 점검 및 종료 확인은 통과**다. 호출별 모든 성공·실제 작업 전송/계산·파일·운용 로그 저장까지 확장하지 않는다. 다음 S3 리전/공개 차단/소유권/암호화/lifecycle, DB TTL 읽기와 API 플래그 활성화·비밀 없는 설정 조회를 준비했다. 별도 AI가 실제 미확인 저장 경계, 예상 계정 제한, lifecycle 없음 응답, TTL 완료 상태, 환경 변수 보존 및 설정 저장/실행 성공의 구별을 검토했다. SDK 읽기 요청7종·shell7개·JMESPath7개·정확한 계정/버킷/리전·합성 비밀 환경값 출력 제외·기존 lifecycle 규칙 출력 보존·문서 diff 공백 검사를 통과했다. 코드/ZIP은 변경하지 않아 전체 회귀를 반복하지 않았다. AWS 실제 조회·API 활성화는 사용자 후속 수행 대상이고 에이전트의 직접 AWS 작업은 없었다.

최신 사용자 S3 조회는 정확한 버킷/예상 계정에 대해 리전us-east-2, 공개 차단4개true, BucketOwnerEnforced, AES256, lifecycle 규칙 없음이었다. API 함수 설정도 두 조회에서 true/Active/Successful이었다. 설정 저장 확인은 통과하며 Lambda 실제 기동/로그인/파일 인수는 아직 별도다. TTL은 출력DISABLED와 달리 붙여넣은 명령이 S3 조회로 표시되어 정확한 DynamoDB 테이블 조회를 한 번 더 안내한다. 다음 직접 Lambda 시험용 `api-direct-check.py`를 기존 비공개 설정 폴더에 준비했다. 인증 없는 접근 차단·Dummy 로그인·동일 세션·임시 과정15개를 확인하고 토큰/원문을 출력하지 않는다. 이 시험은 로그인/과정 준비에 따른 DB/S3 기록을 만들며 공용 진도에 영향을 주는 로그아웃/초기화는 수행하지 않는다. 독립 파일 검토에서 선택적 AWS CLI history가 원문을 저장할 수 있음을 찾아, AWS 호출 전 해당 로컬 설정이 비활성인지 확인하고 활성/불명확하면 중단하도록 보완했다. 별도 시험 담당 AI가 최종 SHA256 `12fb05c2e4545ea200eb91600198282bce8ff0f6d0d39ae5a0f99ae3b9f8a538`의29개 대역 시나리오를 통과했다. 정상 STS1회/Invoke4회, history 켜짐·잘못된 계정의 호출 전 중단, FunctionError/HTTP/ready/세션/목록 실패, 원문·토큰 출력 및 명령 인자 제외, 파일0600/폴더0700/성공·실패 뒤 자체 임시파일 정리, timeout 무재시도를 확인했다. 주 검토자는 Python 구문·SDK 요청3종·shell2개·TTL JMESPath·권한/Git 제외·문서 diff 공백 검사를 통과했다. 실제 AWS 호출은0회이며 앱 runtime/ZIP은 변경하지 않았다. 실제 실행은 사용자가 CloudShell에서 수행하는 후속 단계다.

이후 사용자 출력에서 `arc-calc-dev-training`의 TTL=DISABLED가 명확히 확인됐다. 직접 Lambda 시험은 계정PASS, 미로그인401/SESSION_REQUIRED, 로그인201/ready/토큰 있음, 세션200/동일 세션, 과정200/count15/returned15/Dummy 표시, ALL_PASS였다. **이 범위의 실제 AWS 직접 로그인·세션·과정 준비/조회는 통과**이며 공개 HTTPS·실물 업로드·Worker 계산·차트·운용 로그 저장 조회는 아직 별도다. Mac 로컬 폴더 주소를 CloudShell에 입력한 오류는 후속 파일 실행 성공과 무관해 재업로드나 재로그인을 요구하지 않는다. 사용자 D98로 Dev 요청량10/s·burst20을 확정했고, 실제 적용 전 기존 REST API 기본 endpoint/정책 유무와 stage 목록의 비밀 없는 읽기 요청을 준비했다. 별도 AI가 조회 범위와 stage 생성/제한 설정이 별도 요청임을 검토했다. SDK 요청2종·shell2개·JMESPath2개, 빈/기존 stage, 정책 원문·stage/일부 트래픽 배포 변수·접속 로그 format 제외와 diff 공백 검사를 통과했다. 실제 Gateway 설정 변경은 수행하지 않았다.

후속 사용자 get-rest-api는 정확한 ID/이름/Regional/multipart, 기본 endpoint 활성(false), resource policy 없음, Project/Env 태그 일치였고 get-stages는 빈 목록이었다. 이 현재 상태와 D98을 바탕으로 기존 비공개 폴더에 `create-dev-stage.sh`를 준비했다. API를 사용자 콘솔에서 일시false로 둔 상태를 확인한 뒤 새 배포본/dev stage/5개 메서드 설정을 적용하고 실제 저장값을 검사하는 최초 생성 도구다. API 자동 재활성화·기존 stage 덮어쓰기·AWS 자원 삭제는 없고 사용자 출력의 READY 확인 뒤 true를 복원하도록 안내한다. 별도 AI가 실제 스크립트의 guard, 공식replace패치경로, `*/*` 기본설정키, 단계별 중단과 검사 범위를 읽고 차단 문제를 찾지 못했다. 다른 AI의 실제Bash/Python+가짜AWS CLI 실행은33/33개 시나리오 통과로 정상2건과 history/계정/API/stage/변경/저장값 오류31건의 중단·후속 호출 차단, 자체 임시파일0600/정리·다른파일 보존을 확인했다. 검증 SHA256은 `f9655cef87619e7245fc6e9a4e78ecc7634de581da1f6f26f1b6577c3d70bc9d`다. 주 검토자는 SDK 요청7종·패치5개·Bash/Python구문·안내shell3개·JMESPath4개·파일권한/Git제외·민감 필드 출력제외·curl옵션·문서diff 검사를 통과했다. 실제 AWS 호출은0회이고 runtime/ZIP은 변경하지 않았다. 실제 AWS 변경과 HTTPS 응답 확인은 사용자 후속 수행 대상이다.

후속 사용자 실행에서 실제 배포본 `t58l2a`/dev stage 생성과 요청량10/s·burst20, 캐시/본문 로그/추적 꺼짐, 태그 일치, 접속 로그 대상/일부 트래픽 배포 없음 및 READY_TO_ENABLE_API를 확인했다. 이어 API Active/Successful/true와 실제 HTTPS `GET /dev/api/v2/session/`의401/SESSION_REQUIRED, application/json/no-store를 확인했다. 이는 공개 경로의 미로그인 차단까지 통과한 근거다. 로그인·인증 헤더·query·multipart·Worker 계산까지 성공한 것으로 확대하지 않는다.

다음 단계용 `api-https-check.py`를 기존 Git 제외 폴더에0600으로 준비했다. 검증한 기존 direct helper bytes를 SHA 확인 후 재사용하며 AWS CLI 분기는 실행하지 않는다. 고정 host443/dev·TLS 인증서/호스트 검증·TLS 키 기록 환경변수 차단·debug0·redirect/자동 재시도 없음·응답1MiB 상한·토큰 메모리 보관으로 로그인/세션/과정15개/query/noslash404를 검사한다. 별도 AI의 실파일 검토는 차단 문제 없음이었고, 다른 AI의 실제 Python 실행+HTTPS/TLS 대역 실패 주입41/41개가 통과했다. helper 누락/변조, 키 기록/TLS 설정, HTTP429/redirect, TLS/timeout/불완전 응답, header/본문/세션/과정/페이지 오류에서 후속 호출과 ALL_PASS가 차단됐다. 토큰/원문/예외 출력 없음, 고정 host/port/stage/query/header 및 연결 close도 확인했다. 검증 SHA256은 `9c4f6bc592f8499cbc6ff94123ca14076be9705744a3b7ed839bf22b49321b80`이다. 주 검토자는 구문·helper SHA·파일0600/폴더0700·Git 제외·Markdown 코드블록/참조·diff 공백 검사를 통과했다. 에이전트의 실제 AWS/네트워크 호출은0회이며 runtime/ZIP은 변경하지 않았다. 이 도구의 실제 HTTPS 로그인·조회 실행은 사용자 후속 단계다. APP_API에는 stage를 보존하는 API 경로·pagination URL 연결 규칙을 명시했다.

사용자는 D99로 실제 앱 파일이 아직 없어 기존 저장소 시험 자료를 먼저 사용하도록 선택했다. 별도 AI가 `tests/dataset/cco_1.bin`의31,380bytes와 SHA256 `3e8b7dc3b72b70886e0c9cdb9bd9bfbcb6401713be0dbffc3c27e20de1b5da91`을 확인하고, 현재 계산기/독립 oracle을 쓰기·네트워크·비공개 설정 읽기 차단 아래 메모리 실행했다. 성인 compression_only/ARC2025에서 압박101회/환기0회/전체 점수100, cycle_count1/elapsed_seconds56.308, 기존 tester Pass 및 차트101점이 일치했다. manifest의 recorded 분류를 확인했지만 수집자·기기·수집일을 독립 확인한 것은 아니다. Dev 매핑은 과정910004/등록920004/훈련940041/최종평가940042이며 실제 요청의 definitionHash/condition/attemptId는 AWS 응답을 사용한다. 기존 Dev 통합시험은 합성60회 파일을 쓰므로 이 저장소 파일의 실물 검증 증거와 혼동하지 않는다. 이 계산은 로컬 예상값이며 아직 AWS 파일 업로드·계산을 실행하지 않았다.

후속 사용자 `api-https-check.py` 실행 출력은 without_login401/SESSION_REQUIRED, login201/ready/토큰 있음, session200/동일 세션, courses200/count15/returned15/AllDummyCourses, pagination200/정확한 다음 페이지, missing_trailing_slash404/NOT_FOUND 모두 PASS였다. 여섯 응답 모두 HeadersValid=true이며 마지막 ALL_PASS/https_login_session_courses_query_path로 **실제 공개 HTTPS의 로그인·세션·과정·query/경로 전달을 확인**했다. 아직 multipart 계산·S3 원본 hash·차트 다운로드를 확인한 기록은 아니다.

후속 `api-calculation-check.py`와 원본과 동일한 `cco_1.bin` 사본을 기존 Git 제외 폴더에0600으로 준비했다. 일반 훈련940041 한 건만 생성하며 실제 시작 응답의 조건과hash를 사용하고, POST200/202 모두 허용한다. 기존 완료·예상 밖 상태는 초기화하지 않고 중단한다. 사용자 CloudShell의 새0700 폴더/0600 체크포인트에 세션·시작요청·시도/복구 증표를 보관하되 원문·비밀·서명URL을 출력하지 않는다. 일반 재실행은 기존 폴더에서 중단하고 --status는 같은 시도의GET만 수행한다. 업로드 시작부터30초를 기준으로 조회를 제한하되 socket timeout의 실제 전체 경과시간 보장과 구별한다. 최종평가·로그아웃·초기화·취소는 수행하지 않는다.

별도 AI의 실파일 계약/안전 검토에서 차단 문제를 찾지 못했다. 다른 AI는 쓰기·네트워크·비공개 설정 읽기를 차단한 메모리 시험으로 새multipart 인코더→실제API Gateway base64 봉투/기존parse_measurement→CPR31380bytes/hash/condition6개·타입 보존을 확인했다. multipart31885bytes/base6442516bytes였다. 실제JourneyStorage+MemoryS3→InternalCalculator→차트 생성/발행→finalize_legacy_response/evaluate→실제응답serializer→새check_result가 PASS였다. 이 경계의 차트 서명 및 공유 진도 결과는 대역이다. 실제DummyDevCourseProvider/CourseService/DynamoCourseRepository+InMemoryCourseStore로 과정15개/대상훈련 시작/반환condition/새detail(false)검사 및 훈련 전 최종평가 거절도 확인했다. 실제DB/HTTPS/SQS/Relay/AWS권한·완료후진도저장 검증으로 확대하지 않는다.

전송/보관 경계 담당 AI의 실제Python+HTTPS/TLS/시계 대역·격리된 파일 저장 시험49/49개가 통과했다. 202→200/즉시200, 업로드timeout 후 실제 체크포인트를 재사용한 --status GET 전용 재개, 폴더0700/파일·교체용 임시파일0600/원자 교체/UTC 기록, 쓰기 실패 시 이전 기록 보존과 후속 요청 차단을 확인했다. 중복 실행·변조 fixture·권한/symlink/설정 오류, start/upload 불확실성, redirect/TLS/429/본문/계산/공유 진도 오류에서 중단했으며 pending은 조회 예산/횟수에 따라 WAITING·종료2였다. 취소/재업로드·토큰/복구증표/서명URL/원문/예외 출력은 없었다. 검증 스크립트 SHA256은 `396e9bef6f8f56cbd4a3c95c6ba44bbd02707c7a36a63ff4003a13ed7a60f91b`, fixture SHA는 위 원본과 일치한다. 주 검토자는 구문·파일0600/폴더0700·Git제외·문서 참조/코드블록·diff 공백 검사를 통과했다. runtime/ZIP 변경과 에이전트의 실제AWS/네트워크/개인홈 접근은 없었다. 실제 첫 계산·공유 진도 결과는 사용자 후속 실행으로 확인해야 한다.

후속 사용자 `api-calculation-check.py` 실행은 fixture31380bytes/hash, 로그인201/ready, 미완료 훈련/평가 조회, 시작201/condition 일치, multipart202 접수와 pending 출력2회, succeeded/압박101/환기0/전체100/목표 충족/프로그램 완료/현재 공유 진도 반영/Dummy 제외가 모두 PASS였다. 뒤이어 일반 훈련 완료true/최종평가 완료false와 ALL_PASS/https_recorded_practice_calculation_progress가 확인됐다. **이 범위의 실제 AWS 비동기 계산과 일반 훈련 진도 반영은 통과**다. 출력만으로 Stream 직접 전달과 Scheduler 복구 중 실제 전달 경로, 총 지연시간, S3 원본 bytes의 독립 일치나 차트 다운로드·운용 로그 저장을 단정하지 않는다. 완료된 시도와 체크포인트는 후속 읽기 확인에 재사용한다.

후속 읽기 도구 `api-artifacts-check.py`를 같은 Git 제외 폴더에0600으로 준비했다. 기존 계산 도구의 SHA를 먼저 확인하고 완료 체크포인트·원본 파일을 읽으며 새 훈련/로그인/재계산이나 체크포인트 변경은 하지 않는다. 같은 시도의 API 결과, 정확한 Attempt/Job, S3 manifest/원본/최종 결과를 비교하고 제한된 날짜/시간의 OPS 파티션을 최대12페이지 조회한다. API 접수 및 Worker 시작/완료 로그가 모두 있어야 통과한다. 새 차트 서명으로 실제 파일을 내려받아 저장 결과/독립 기대값과 대조하고, 서명을 제거한 접근은403/404인지 확인한다. 토큰/서명URL/DB·S3 원문/예외 원문은 출력하지 않는다.

별도 AI가 실제 JourneyStorage/MemoryS3 산출물과 실제 로그 검증기를 사용해 저장·로그 부분의 정상/누락/변조/자료형 차이/조회 범위 오류9개 사례를 통과했다. 다른 AI는 배포 ZIP 계산기로 독립 생성한 차트22612bytes/SHA256 `2b2f4e4c27e19c0117778ffd880ffead65deeb2f3ca65644e9aaac99bb98d140`, 정규화 SHA256 `b614d5625eb70355dfc48f23bf884cb478e576214d9ddb66c4b7785713018803`과 SDK의 regional 가상호스트 서명 형식을 확인했다. 새 검증 도구 초안의 차트 경로 검사에서 불필요한 decode/슬래시 제거로 다른 경로를 허용하는 문제를 찾아 정확한 경로 비교로 보완했고 정상 경로 허용·인코딩된 추가 슬래시/중복 슬래시 거절을 재확인했다. 이는 새 검증 도구의 보완이며 배포된 앱의 취약점 발견으로 표현하지 않는다. 주 검토자는 SDK 읽기 요청4종, 구문·helper SHA·파일0600/폴더0700·Git 제외·문서 참조/코드블록을 검사했다. AWS 직접 호출·실제 사용자 체크포인트 읽기·runtime/ZIP 변경은 없었다. 저장 원본·차트 다운로드·운용 로그의 실제 AWS 확인은 사용자 후속 실행 대상이다.

전송·파일 보관 경계 담당 AI의 오프라인 실행/실패 주입60/60개가 통과했다. 저장·로그 부분만 대역으로 두고 실제 본체/AWSReader/HTTPS 검사/체크포인트 읽기/차트 정규화를 실행했다. 정상 경로, helper/fixture/체크포인트 오류, CLI history/계정/endpoint, TLS/HTTP/응답 오류, S3 크기/소유자/range 전체 읽기, 서명URL host/경로/query 및 인코딩된 추가 슬래시 회귀, 비밀·원문 비노출, 기존 체크포인트 bytes/권한 보존과 자체 임시파일 정리를 확인했다. 최종 도구 SHA256은 `cfc7441f1553e4a1da130051a832d520bf0e95ac898f963b7e13afc9e1f1c281`이다. 실제 AWS CLI 프로세스/네트워크 호출은0회이며 실제 권한이나 서비스 응답 인수로 확대하지 않는다. 최종 문서 diff 공백 검사도 통과했다.

후속 사용자 출력에서 계산 도구 일반 재실행은 `CHECKPOINT_EXISTS_NO_NEW_ATTEMPT`로 중단해 새 시도를 만들지 않았다. 이어 실제 `api-artifacts-check.py`는 계정·기존 결과, S3 원본31380bytes/hash·manifest/최종 결과 hash·시도/작업 참조·입력 digest·API 결과 일치를 모두 PASS로 보고했다. 운용 로그는1페이지/16행 전체 검증과 접수/계산 시작/완료 각1건이 PASS였다. **해당 실제 AWS 저장 원본·최종 결과 및 운용 로그 확인은 통과**이며 차트는 `CHART_URL_OUTSIDE_DEV`에서 중단됐다. 이는 도구의 URL 검사에서 멈춘 것이며 차트 GET의 HTTP 오류, AWS 권한 거절, 실제 외부 주소 발급을 입증하는 출력은 아니다. 서버가 발급한 전체 URL은 받거나 출력하지 않았다.

점검 도구가 정확한 버킷의 regional host 한 개만 허용하던 가정을 보완해 새0600 파일 `api-artifacts-check-v2.py`를 준비했다. 동일 버킷의 정확한 regional/global host 두 개만 허용하며 Ohio SigV4 범위·정확한 객체 경로·300초·TLS·redirect 금지·본문 hash 검사는 그대로다. 발급된 host를 그대로 사용해 서명을 훼손하지 않으며 Bearer는 API에만 전송한다. 주소 원문 대신 고정 형식 이름과 HTTPS/포트/userinfo/fragment의 boolean만 INFO로 출력한다. [AWS의 global endpoint 설명](https://docs.aws.amazon.com/AmazonS3/latest/userguide/VirtualHosting.html#VirtualHostingBackwardsCompatibility)은 지원되는 형식과 DNS/redirect 가능성을 구별한다. 실제 사용자 주소의 형식과 다운로드 성공은 다음 실행으로 확인해야 한다.

별도 AI가 기존60개와 두 host 전송/실패13개, 총73/73개 오프라인 시험을 통과했다. regional/global 실제 host 사용·S3 Bearer 미전달, 잘못된 bucket/region/suffix/경로·TLS/redirect 거절, 진단의 원문·비밀 비노출, 기존 체크포인트 bytes/권한과 자체 임시파일 정리를 확인했다. v2 SHA256은 `8b21af3a71979b4d4e36b28864ed35611a71d4bbae89a921601db2e6e7069b16`이며 v1 원본은 보존했다. 저장·로그 부분은 앞선 독립 시험과 이번 사용자 AWS 결과를 유지하며 해당 전송 시험에서는 대역이었다. runtime/ZIP 변경·실제 AWS CLI/네트워크 호출·사용자 체크포인트 읽기는 없었다.

독립 담당자가 배포 ZIP의 boto3 1.34.140/botocore 1.34.162와 실제 `us-east-2`/s3v4/주소 방식 미지정 설정을8개 조합으로 재현했다. 내장 모델로 인식시킨 실제 SDK는 정확한 Dev 버킷의 global host를 발급했고 Ohio 서명 범위/300초/host/쿼리7개는 유지했다. 이전 regional-only 재현은 모델을 외부 Loader로 연결해 SDK가 regional endpoint를 강제한 차이가 있어 결론을 정정한다. 이는 점검 도구의 제한을 보완할 재현 근거이며 실제 Lambda가 반환한 URL 형식은 v2의 안전한 진단으로 확인한다. 재현은 네트워크·비밀 파일·파일 쓰기를 차단했고 서버 수정·배포는 하지 않았다.

후속 사용자 `api-artifacts-check-v2.py` 출력은 원본/manifest/최종 결과 hash·입력 digest·참조·API 일치, 운용 로그1페이지/16행 및 접수/시작/완료 각1건, global 주소 형식과 HTTPS/기본 포트/userinfo·fragment 없음, 차트HTTP200/101점/저장 hash·독립 차트 일치, 서명 없는HTTP403 및 마지막 ALL_PASS였다. **사용자 제공 출력 범위의 실제 AWS 일반 훈련 원본·결과·운용 로그·서명 차트 다운로드·익명 접근 차단을 통과**했다. 실제 주소 형식이 global임도 확인돼 이전 regional-only 검사 중단 원인을 확정했다. 전체 URL/토큰/원문은 공유되지 않았다. 300초는 서명 설정 검사이며 실제 시간 경과 후 만료 거절·최종평가·다른 시도 소유권·중복 전달·장애·실물 앱·한도/비용 인수로 확대하지 않는다.

다음 사용자 실행용 `api-assessment-check.py`를 기존 Git 제외 폴더에0600으로 준비했다. 완료된 일반 훈련 체크포인트의 세션을 읽기만 하고 새 평가 체크포인트를 다른0700 폴더/0600 파일에 저장한다. 같은 과정/등록/정의를 확인한 뒤 마지막 평가940042 한 건만 시작·업로드하고, 일반 훈련/최종평가의 완료·통과와 과정 FINISHED를 실제 응답으로 확인한다. 반복 일반 실행은 중단하고 --status는 같은 시도의 GET만 한다. 기존 mode=training, 실제 원본/계산, Dummy 제출 제외·외부 송신 비활성은 유지한다.

독립 계약 검토에서 초안의 응답 필드명·최종평가 미실시 값·목록 경로·기존 체크포인트 키가 실제 API/기존 도구와 다름을 찾아 반려했다. 기존 도구와 실제 serializer 기준으로 모두 교정했다. 새 도구 기대값에 맞춘 가짜 응답만으로 계약 통과를 주장하지 않는다. 배포 ZIP의 실제 Dummy 정의·InternalCalculator·worker.evaluate와 과정 완료 정책을 격리 메모리에서 대조해 final_assessment 역할/기존 training mode, 기록 파일31380bytes/hash, 압박101/환기0/전체100/56.308초, 목표60충족/평가pass, 과정 IN_PROGRESS→FINISHED 및 합격 뒤 재시작 거절을 확인했다. 별도 담당은 실제 CourseCompletionPlan의 변경 집합을 메모리 저장소에 적용해 final_phase=passed·진도APPLIED·Dummy 제외·두 항목 완료/통과를 확인했다. 이는 실제 DB 거래/HTTPS/SQS/AWS 상태전이 검증이 아니다. 이전 일반 훈련 및 배포 runtime/ZIP은 변경하지 않았다.

수정 평가 도구의 SHA256 `32bdd43a4c5dba638df1cf04db110db26f088f1494d4f6c4d021c628cb6aed9e`에 대해 전송·파일 보관 경계 담당이67/67개 오프라인 시험을 통과했다. 기존 일반 훈련 도구가 실제 save 호출로 생성한 complete 체크포인트를 재사용해202→200/즉시200, 업로드 응답 유실 뒤 upload_requested 기록의 --status GET 전용 재개, 일반 훈련 원본 bytes/권한 보존, 평가 폴더0700/파일0600/원자 교체 및 쓰기 실패 정리를 확인했다. 중복·선행조건·활성 평가/복구·FINISHED 불일치·TLS/redirect/429·pending 한도에서 후속 변경과 ALL_PASS를 차단하고 토큰·복구 증표·서명URL·원문·예외를 노출하지 않았다. 별도 계약 담당도 원본 도구 생성 체크포인트의 재사용·별도 평가 보관·원본 동일·--status GET만·중복 실행 거절을 확인했다. 주 검토자는 기준 도구와 수정 도구의 AST 필드/체크포인트 키 포함 관계, 구문·권한·Markdown 코드블록·diff 공백을 검사했다. 실제 네트워크/AWS CLI/개인 홈 접근은 없으며 실제 최종평가 실행은 사용자 후속 단계다.

최종 같은 SHA에서 실제 Dummy provider/CourseService/AuthBound bridge/CourseCompletionPlan과 serializer가 생성한 평가 전후 DTO를 변환 없이 새 Probe.detail(completed=False/True)에 넣어 통과했고 실제 시작 응답의 Probe.validate_start도 통과했다. 미응시 isPassed=null→합격 true, final passed, 과정 FINISHED와 합격 후 재시작 거절을 확인했다. 이 재검증은 앞서 확인한 압박 통과 평가를 사용해 메모리 상태만 재구성했으며 계산을 다시 실행하거나 실제 DB/AWS에 접근하지 않았다.

후속 사용자 `api-assessment-check.py` 실행 출력에서 기존 원본31380bytes/hash, 일반 훈련 세션 재사용·체크포인트 보존, 훈련 완료/최종평가 미완료·미판정 및 과정 IN_PROGRESS, final_assessment 시작201/조건 일치·체크포인트 저장, 업로드202/pending 출력2회 뒤 실제101회/0회/전체100/목표 충족/프로그램 완료/진도 반영/Dummy 제외, 두 항목 완료/최종평가 통과·과정 FINISHED 및 마지막 ALL_PASS/https_recorded_final_assessment_course_completion을 확인했다. **이 성인 압박 Only 과정의 일반 훈련→최종평가→과정 완료 기본 흐름은 실제 AWS 사용자 출력 범위에서 통과**다. pending 출력 횟수로 지연시간이나 전달 경로를 단정하지 않는다. 일반 훈련의 저장/차트/운용 로그 확인을 최종평가의 별도 객체 재조회로 확대하지 않으며 최종평가 재응시 거절·소유권·중복·실제 만료·장애·앱/기기·용량/비용은 남아 있다. 완료된 두 체크포인트·공유 진도·원본을 보존하고 후속 큐/DLQ 읽기 안내와 APP_API의 실제 Dev 연결 정보를 준비했다.

후속 안내는 정확한 Dev jobs/DLQ 두 큐의 GetQueueAttributes로 ARN과 세 근사 메시지 수만 읽도록 제한했다. 별도 AI가 현재 기본 흐름 통과 판정과 남은 실제 인수 경계, 근삿값의 해석 및 메시지 수신/삭제 없는 범위를 독립 검토했다. 주 검토자는 배포 ZIP SDK 모델로 읽기 요청2개·Bash 구문2개·JMESPath2개(0/양수 대역)·계정/리전/큐 URL·네 핵심 문서의 코드블록과 diff 공백을 검사해 통과했다. 실제 큐 상태는 사용자 후속 조회 대상이며 에이전트의 AWS 호출·runtime/ZIP 변경·전체 회귀 재실행은 없었다. APP_API에는 실제 Dev stage 주소/로그인·초기 한도·검증 범위와 현재 완료 과정의 새 시작 제한을 반영했다.

후속 사용자 출력에서 정확한 jobs/DLQ 두 ARN과 Waiting/Processing/Delayed 각0을 확인했다. **일반 훈련·최종평가 뒤 두 큐에서 현재 적체가 관측되지 않았다.** 근사 조회이며 과거 실패 없음·무손실·중복 전달/복구·경보 수신 완료로 확대하지 않는다. 별도 AI는 앱팀의 로그인·과정 조회·완료 상태 표시 연결 시험을 시작할 수 있고, 기존 완료 진도와 세션별 시도 소유권을 보존해야 한다고 검토했다. 앱 연결과 병행할 CloudWatch 기본 로그 그룹·프로젝트 접두사 수치 경보의 읽기 안내를 준비했다. 두 명령의 Bash 구문·배포 ZIP SDK 요청 형식·대역 응답 JMESPath 및 문서 diff 공백 검사가 통과했다. AWS 호출이나 runtime/ZIP 변경은 없었다. 실제 로그 내용·오류 지표·경보 수신과 FitCloud 비용 설정은 아직 미확인이다.

후속 사용자 CloudWatch 출력에서 API/Relay/Worker 세 기본 로그 그룹의 존재, StoredBytes 각0, RetentionDays 각null 및 `arc-calc-dev-` 접두사 MetricAlarm 빈 목록을 확인했다. StoredBytes0으로 로그 부재를 확정하지 않았으며 다른 이름/유형의 경보까지 없다고 확대하지 않았다. 사용자는 새 SNS 주제를 통한 이메일 장애 알림을 선택했다(D100). 독립 검토와 AWS 공식 Standard/email/구독 확인 명세를 바탕으로 Ohio의 `arc-calc-dev-alerts` 생성·지정 태그·콘솔 이메일 직접 입력·확인 메일 승인·주소 비노출 구독 조회 안내를 준비했다. 실제 주제/구독/확인·경보 정책/기준/권한·실제 알림 수신과 CloudWatch 로그 수집은 후속 확인이다. 앱팀 기본 연결 시험과 별개이며 기존 공유 진도·원본·보관기간은 유지한다.

후속 사용자 SNS 출력에서 정확한 Ohio/account150612770165 `arc-calc-dev-alerts` 주제, protocol=email, 확인 대기가 아닌 실제 구독 ARN을 확인했다. 이메일 구독 준비는 통과했으나 CloudWatch 발행·실제 경보 수신 증거는 아니다. 다음 주제/구독 속성 읽기 안내는 Endpoint를 제외하고 기존 정책·암호화·필터를 보존해 필요한 변경을 준비하도록 했다. 독립 코드 검토에서 API의 정상 Lambda 반환/HTTP503, Worker·Relay의 정상 반환 부분 실패 및 Worker의 확정 실패 ACK가 Lambda Errors와 같지 않음을 확인했다. 초기 구성은 Lambda Errors/Throttles6개+API5XX1개+DLQVisible1개의8개이며 사용자는 오류/호출 제한5분1회, DLQ1분1건의 민감한 기준을 선택했다(D101). 모든 업무 실패/미전달·지연을 포괄하는 경보로 해석하지 않는다. AWS 속성 조회·정책 변경·경보 생성/알림 시험은 아직 사용자 후속 단계다.

후속 사용자 주제/구독 속성에서 정확한 topic/owner, confirmed1/pending0, Fifo/KMS 없음 및 email 확인 완료·필터/범위/Redrive 없음이 일치했다. 실제 기본 정책의 조건은 `AWS:SourceAccount=150612770165`이며 SourceOwner가 아니었다. 독립 검토와 공식 Principal/SourceAccount 지원 문서를 근거로 현재 SNS:Publish 허용이 같은 계정 CloudWatch 발행을 이미 포괄한다고 판단했다. 최종 준비 범위는 SNS 정책을 그대로 검증·보존하고 경보8개만 생성하는 사용자 실행 도구다. 현재 설정 출력·정책 추론은 실제 경보 발행/수신 성공을 대신하지 않는다.

사용자 실행용 `create-dev-alarms.py` 최종 SHA256 `43f69a7730603d52a3fbc4e6d08c3ca66a897cfa82a11b08c86e57312547f96e`(16,354bytes/305줄)에 대해 별도 담당의 실제 main 대역 시험40개와 독립8개 payload·SDK 모델 검증이 통과했다. 정상 최대8개의 PutMetricAlarm 외 쓰기는 없고 SNS 정책을 보존했다. 잘못된 계정/history/SNS/필터/KMS·세 유형 이름 충돌·구형 CLI 모델에서 생성 전 중단, 일부 생성 뒤 실패/응답 불명에서 생성물 보존·자동 재시도/삭제 없음, --status의 완성/부분/누락/설정·태그 불일치 모두 쓰기0, 비밀 출력 차단·고정 endpoint/TLS·요청 임시파일0700/0600을 확인했다. API ID/이름/REGIONAL/dev stage를 대조하되 같은 stage의 정상 deployment 갱신은 허용한다. 주 검토자의 구문·8개 SDK 요청·업로드 안내 shell2개·Markdown·권한/Git 제외·diff 공백 검사도 통과했다. 실제 AWS/네트워크·사용자 홈/설정/키/체크포인트 접근이나 경보 생성은 하지 않았고 런타임/ZIP 변경·전체 앱 회귀 반복은 없다. 실제 생성·설정 재조회·수신 시험은 사용자의 CloudShell 실행 후 확인한다.

후속 사용자 실제 실행은 `AWS_COMMAND_FAILED`·`AWSMutationMayHaveOccurred=false`로 중단됐다. 이 실행에서는 경보 생성 요청에 도달하지 않았으며, 기존 오류 출력에 실패 명령이 없어 자격 증명·CLI 호환성·서비스 오류 중 어느 원인인지 아직 특정할 수 없다. 이메일 구독 재조회는 정확한 topic/owner/email 및 확인 완료·필터/범위/Redrive 미설정이었다. 기존 40개 오프라인 시험은 실제 CloudShell 호환성이나 경보 설치 성공을 입증하지 않는다. 설치를 반복하지 않고 진단 정보를 보완한 읽기 전용 조회로 실패 지점부터 확인한다.

원본을 보존하고 `create-dev-alarms-v2.py`(SHA256 `f9e75d11289d337a8fc256f74aa4da4dc892c43104b0aaa42e2f2f99fa104793`, 20,658bytes/0600/Git 제외)를 준비했다. 고정된 실패 명령 식별자·CLI 종료 코드·허용 오류 코드·오류 분류·CLI 버전 숫자만 출력하고 오류 원문/인자/환경은 숨긴다. CLI 오류 형식은 subprocess 환경의 legacy로 맞추며 로컬 모델 확인은 no-sign-request로 자격 증명 조회를 피한다. 별도 담당의 합성 CLI 실제 main 집중 시험15개가 통과했다: 생성자/STS/모델 오류의 위치 보존·비밀 출력 차단, --status 완성/부분 구성의 쓰기0, 구형 모델 차단·부분 생성 보존·자동 재시도/SNS 쓰기 없음, 원본8개 요청/태그와 원본 파일 SHA 불변을 확인했다. 주 검토자는 변경 diff와 사용자 안내 shell2개/Markdown을 확인했다. 실제 AWS/네트워크·키/체크포인트 접근이나 런타임/ZIP 변경은 없으며, 사용자에게는 먼저 v2의 --status만 실행하도록 안내한다. 실제 실패 원인과 생성/메일 수신은 아직 미확인이다.

후속 v2 사용자 출력은 CLI2.36.47·`CLOUDWATCH_MODEL_SKELETON`·종료252·쓰기 시도false였다. STS 계정 확인을 지난 로컬 출력 견본 검사에서 중단됐다. 주 검토자는 실제 로컬 AWS CLI2.36.44를 임시 HOME·빈 설정/자격 증명·metadata 비활성·no-sign-request로 실행해 같은 명령의 종료252를 재현했다. `Period`·`EvaluationPeriods` 등의 견본0값과 `EvaluationWindow` 선택 항목이 응답 검증에 실패했다. 공식 AWS CLI의 견본 생성→Stubber 응답 검증 구현과도 일치한다. 사용자2.36.47의 원문 stderr를 직접 본 것은 아니며, 로컬 재현과 실제 실패 위치를 구별한다. 기존 대역 시험은 이 실제 CLI 견본 생성 실패를 놓쳤다.

`create-dev-alarms-v3.py`(SHA256 `61ce4e576e221cb078c514b127d0819092ece08c03550083b3caa013bba476c2`)는 skeleton 의존을 제거하고 정확한 경보 ARN의 ListTagsForResource로 존재를 확인한다. 빈 Tags도 존재이며 해당 조회의 ResourceNotFoundException만 부재로 인정한다. 독립 검토가 공식 공통 ARN/태그/부재 의미와 LogAlarm 지원을 대조했다. 첫 생성 전8개와 각 생성 바로 전에 재확인하며 비원자적 동시 편집 한계는 유지한다. 별도 담당의 실제 main 합성 CLI 집중 시험16개가 통과했다: 정상8개 생성·기존 payload/태그 불변, 빈 태그/파서에서 숨겨진 LogAlarm/다른 오류/생성 중 이름 충돌 방어, 부분 생성 보존, --status의 READY_TO_CREATE/설치 완료/부분 구성 모두 쓰기0, 원본v1/v2 보존을 확인했다. 주 검토자는 실제 로컬 CLI의 ListTags 요청 파싱·응답 형태를 서비스 요청 없는 견본으로 확인하고, 핵심5함수 AST 불변·구문을 검사했다. 실제 AWS 서비스 호출·경보 생성·키/체크포인트 접근이나 런타임/ZIP 변경은 없다. 사용자는 v3 --status의 READY_TO_CREATE 뒤 최초 생성을 진행할 수 있으며 실제 설치/메일 수신은 아직 미확인이다.

후속 사용자 실제 CloudShell 출력에서 v3 STATUS의 READY_TO_CREATE/Count0과 CREATE의8개 생성PASS, SNS 정책 보존·확인 구독1개, 각 경보 설정PASS, 마지막 ALARMS_CONFIGURED/Count8을 확인했다. **초기 경보8개 설치와 저장 설정 검증은 통과**다. 조회 당시 상태는 모두 INSUFFICIENT_DATA였고 ActualEmailDeliveryVerified=false였다. 앞서 함께 붙인 v1/v2 STOP은 쓰기 시도false였으며 v3 설치 성공과 구별했다. 사용자 출력에 근거한 사실이고 에이전트의 AWS 직접 조회는 아니다.

다음 수신 시험은 기존 DLQ MetricAlarm 한 개의 현재 상태·SNS 전용 동작을 읽고, OK/INSUFFICIENT_DATA일 때만 사용자가 SetAlarmState(ALARM/TEST ONLY)를 한 번 실행한 뒤 Action 이력과 이메일 도착을 대조하는 안내로 준비했다. 독립 검토와 공식 AWS 시험/자동 재평가/이력 명세를 대조했고 SDK 요청3개·Bash 안내3개·출력 범위를 제한한 JMESPath2개 검사가 통과했다. 이미 ALARM이면 실제 상황부터 확인하며 강제 OK 복원·오류 주입·별도 자원 생성·즉시 재발송은 하지 않는다. 시험은 CloudWatch→SNS→이메일 경로를 확인하며 실제 오류 탐지·전체8개 경보 개별 수신을 대신하지 않는다. 실제 수신 시험은 아직 실행하지 않았고 도구로 AWS 상태 변경이나 메시지 발송을 수행하지 않았다.

## 2026-09-23 실행 역할 권한 준비

사용자가 DynamoDB/S3 설정 완료와 오하이오 작업 큐·실패 큐 URL, 최대 수신 횟수 5를 전달했다. 이는 사용자 보고이며 도구로 실제 AWS 설정을 조회한 결과가 아니다. 주 검토자와 별도 AI 검토자가 API/Worker/Relay 소스와 AWS 공식 권한 문서를 대조해 역할별 인라인 정책 세 개를 Git에서 제외하는 `var/deployment/dev-iam-20260923-teaab_e4/`에 준비했다. 실제 계정 연결 자료와 Lambda 신뢰 정책을 함께 두었다.

초안 검토에서 없는 S3 객체의 404 확인에 필요한 `ListBucket`, Dummy 완료 시 로컬 제외 기록을 저장하는 Worker의 `SUBMISSION#*` 권한, 혼합 트랜잭션의 `ForAllValues` 키 목록을 보완했다. 역할별 서비스·로그 리소스 분리, API/Worker의 Relay 진행 행 접근 제외, `environment=dev`의 정확한 진행 PK를 확인했다. 최종 세 JSON의 파싱·지원 Action/리소스 범위·혼합 Worker 키·진행 PK 해시·인라인 정책 크기 및 문서 diff 공백 검사를 통과했다. 코드 변경은 없으며 전체 회귀를 다시 실행하지 않았다. **AWS IAM 정책 검사기/시뮬레이터·실제 역할 생성·Lambda 접근 시험은 미실행**이다. 실제 테이블/버킷 이름 대조, AWS 정책 저장 결과와 전체 호출 인수는 후속 단계다.

사용자는 이어서 역할 세 개와 인라인 정책 연결 완료를 보고했다. Lambda 생성 안내 전에 별도 AI 검토자가 기존 ZIP의 실제 존재·13,837,535 bytes·CRC·2,225개 항목·manifest SHA-256을 다시 확인했고, 포함 소스/리소스 110개가 현재 작업트리와 모두 일치했다. 새 코드 변경이나 재빌드는 없으며, Python 3.12/x86_64·역할별 Handler 안내를 준비했다. 이는 업로드할 파일의 오프라인 무결성 확인이며 AWS 함수 생성·업로드·실행을 수행한 기록은 아니다.

## 2026-09-22 AWS Dev 배포 적대적 검토

초기 판정은 **반려**였다. 로컬 과정 API의 통과와 달리 AWS 실행은 새 경로를 제공하지 않았고, 기존 배포 workflow는 push만으로 배포할 수 있었다. 아래 결함 수정과 재현 회귀 후 요청된 **Dummy Dev의 코드·설정 준비 범위**를 통과로 판단했다. 실제 계정/리전/함수/DB/S3/큐·권한·제한값을 조회하지 않았으므로 지금 실환경 배포를 승인했다는 의미는 아니다.

| 확인된 문제 | 보완·검증 |
|---|---|
| AWS에서 `/api/v2` 조립 불가 | 명시적 Dev 전용 `course_v2_dummy` 구성과 기존 5프로그램×3연령의 임시 과정 공급자 추가. API/Worker 동일 버전 검사. 실제 ARC 배정·가짜 점수·fixture 배포 없음 |
| 개별 역할 설정이 통과해도 서로 다른 DB/저장/버전으로 연결 가능 | `validate_aws_dev_bundle.py`가 세 역할의 scope·DB·storage·execution·course 일치를 검사 |
| Relay timeout이 짧아 계속 0건 처리하지만 검사 통과 | 실제 acquire+한 항목 처리+반환/로그 예산으로 거절. Queue visibility/Worker timeout 관계도 검사 |
| 과정 snapshot보다 저장 한도가 작아 로그인 bootstrap 실패 | 실제 15개 bundle의 저장 직렬화 길이를 설정 검사에서 비교. 경계 바로 아래 거절, 정확한 경계 저장/재조회 통과 |
| 큰 query 정수가 Python 변환 제한에서 503, 공개 제어 본문이 크기 검사 전 decode | 계약 상한 자릿수·base64 크기 선검사. 정제 400/413 회귀 |
| push 자동 배포·수동 Dev 임의 branch·배포 전 회귀 공백 | 수동 지정 branch만, 동일 revision 회귀 후 자격증명 설정, GitHub Environment 연결. 실제 reviewer/OIDC trust 설정은 별도 |
| 기존 Lambda 코드 교체 때 실행 형식 미확인·legacy ARN partition 고정 | Python3.12/ZIP 사전 확인, 기존 도구의 China/GovCloud 사전 거절. 전체 세 역할 설치로 과장하지 않음 |

사용자 D94에 따라 이번 Dev의 개인별 접근 장치 신설을 필수 조건으로 요구하지 않았다. 공용 Dummy 자격증명은 공개되어 있어 인터넷 전체 공개 시 외부인도 접속할 수 있다. 기존 인증·소유권·비공개 파일·로그 정제는 유지하며 Beta/Prod까지 같은 접근 정책을 승인한 것으로 취급하지 않는다.

| 이번 실행 | 실제 결과와 범위 |
|---|---|
| 전체 로컬 회귀 | **3312 passed, 9 subtests passed**, 251.56초, exit 0. 13642건은 기존 botocore `datetime.utcnow` deprecation 경고 |
| CI용 오프라인 gate | **403 passed**, 53.92초. 위 전체와 중복되므로 합산하지 않음. 실제 GitHub hosted 실행은 아님 |
| 실제 DB·AWS 진입점 통합 | 신규 `integration_tests/test_aws_dev_course_journey.py`: 실제 임시 DynamoDB + S3 대역 + `lambda_handler.run`/Worker 진입점 + 실제 계산. 로그인/15과정/선행조건/훈련/최종평가/중복 SQS/서버 재조립/다른 세션 거절/로그아웃 reset/원래 결과 보존 통과 |
| 실제 배포 ZIP | 새 고정 pure wheel, builder RECORD/hash 검증, CRC 검사. 2225 files, 압축 13,837,535 bytes, 압축해제 21,085,906 bytes. 테스트·DB·키·로컬 실행기 제외 |
| ZIP 독립 실행 확인 | 추출본만 사용하는 별도 `python -S -B`에서 진입점5개 import, boto3/sentry 로딩, fixture 없이 Dummy 과정15개 구성. Python network audit 차단. 실제 Linux/Lambda 실행 검증은 아님 |
| 배포 정적 검사 | actionlint 1.7.12 두 workflow, shell 문법, diff 공백 검사 통과 |
| 보존 | 계산기·골든 fixture·사용자 기존 dirty 변경·서버·DB·키 보존. commit/push/AWS 접근/배포 없음 |

ZIP SHA-256: `d47fb7a7c7ce6a09c2c51ad2588ac0b0cca6cbd229b095225d4e789b4378fbf0`. runtime 소스 110개의 hash를 최종 작업트리와 대조했다. 빌드 ZIP/manifest는 Git에서 제외하는 `var/deployment/` 아래 이번 작업의 새 폴더에 보관했다. 최종 수정 후 다시 빌드하면 hash가 바뀔 수 있으므로 전달 파일과 manifest를 함께 확인한다.

이번에는 주 검토자와 AWS runtime·배포 도구·보안 담당 AI 3개가 병렬 작업했다. 보안 담당이 신규 runtime/설정 검사와 배포 변경을 교차 공격해 Relay·저장 한도 두 반례를 제출했고, 수정 후 동일 경계를 재심했다. 9월18일의 “새 AI 5인” 검토를 이번에도 수행했다고 주장하지 않는다. AI 판정은 사람의 운영 승인이나 보안 인증이 아니다.

재현 명령(준비된 검증 환경 경로는 해당 PC에 맞춘다):

```sh
STAGE=test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /tmp/vcc-w5-python/bin/python \
  scripts/validate_local_integration.py --dynamodb-home var/dynamodb-local-3.3.1 --suite all
```

남은 실환경 인수: 실제 IAM, Gateway binary/경로/제한, S3 권한/서명, SQS/Stream/예약/DLQ/Relay 최초 행, 로그 전달, Lambda 처리 시간·용량·비용·복구, 실물 앱·마네킨. 영상/문서 콘텐츠와 공식 ARC 계약은 이번 임시 카탈로그 범위 밖이다. 초보자 실행 순서와 실제 자원 확인표는 [DEPLOY_GUIDE](DEPLOY_GUIDE.md#beginner-dev)에 모았다.

<a id="14-vcc-고도화와-신규-독립-재심--2026-09-18"></a>

## 1. 2026-09-18 전체 실행

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
