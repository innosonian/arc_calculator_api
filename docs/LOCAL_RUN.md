# 로컬 Journey 서버 실행 안내

2026-09-10 기준. 서버·DB·계산 worker(대기 작업 실행기)·파일 저장소를 Mac에서 함께 실행한다. AWS 계정이나 ARC 계정은 필요하지 않다.

## 1. Mac에서 실행

Terminal(터미널)에서 실행한다.

```sh
cd /Users/mac/arc_calculator_api
var/local-python/bin/python scripts/serve_local.py
```

현재 Mac에 준비된 Python 실행 환경, `requirements-local.txt` 의존성, Java/Javac, 검증된 `var/dynamodb-local-3.3.1` 배포 파일이 필요하다. 명령이 인터넷에서 도구를 자동 설치하지는 않는다.

정상 시작 메시지:

```text
ARC local journey API ready: http://127.0.0.1:8000
```

터미널을 켜 둔 상태에서 `http://127.0.0.1:8000/healthz`를 연다. 정상 상태는 HTTP `200`, `mode: "local_journey"`, `calculator_available: true`다. 로그인 화면이나 Swagger UI가 뜨는 서버는 아니다.

이제 기본 명령으로 로그인·프로그램·훈련 생성·계산·결과·실제 차트 조회가 연결된다. 과거 제어 API만 필요할 때에만 `--control-only`를 붙인다. 그 모드의 `calculator_available: false`는 정상이며 계산 Journey에는 사용하지 않는다.

## 2. 실제 앱의 연결 순서

앱 개발자에게 [간단한 API 명세](APP_API.md)를 전달한다. 명세에는 서버 주소를 넣지 않았으므로 실제 실행 위치는 별도로 전달한다.

1. `test@test.com` / 문자열 `2222`로 로그인한다.
2. 프로그램·연령을 선택하고 훈련 시도를 생성한다.
3. 앱에서 실제 마네킨 데이터를 누적하고, 훈련 종료 후 전체를 업로드한다.
4. 저장된 계산 결과와 차트를 조회한다.
5. 별도 시도 상태에서 완료 판정을 확인하고, 프로그램 목록을 다시 받아 현재 공유 진도를 확인한다.

서버의 자동 계산 실행과 실제 차트 다운로드는 검증했다. **실물 마네킨과 iPad/Android 앱의 현장 연결 시험은 아직 별도다.** 테스트 서버에서 보낸 기록 바이너리의 성공을 실제 앱 인수로 간주하지 않는다.

## 3. iPad에서 접속

`127.0.0.1`은 접속한 기기 자신이다. iPad에서는 Mac의 현재 내부 IP로 접속한다. 두 기기가 같은 내부 네트워크에 있어야 하며 기기 사이의 통신이 허용되어야 한다.

Mac과 iPad의 현재 Wi-Fi IP를 각각 확인한다. 이전에 전달받은 주소가 지금도 같다고 가정하지 않는다. 아래 대괄호 값은 실제 주소로 교체하고 대괄호는 제거한다.

기존 서버 터미널에서 `Ctrl+C`로 종료한 뒤 실행한다.

```sh
var/local-python/bin/python scripts/serve_local.py \
  --host <Mac의_현재_내부_IP> \
  --allow-client <iPad의_현재_내부_IP> \
  --allow-insecure-lan
```

iPad에서 `http://<Mac의_현재_내부_IP>:8000/healthz`를 확인한다. 이 모드는 지정한 Mac IP에만 서버를 열며 `127.0.0.1:8000`을 동시에 열지 않는다. 동일 데이터 폴더로 두 서버를 동시에 실행하지 않는다.

`--allow-insecure-lan`은 HTTP 평문 통신을 명시적으로 허용하는 개발 설정이다. 신뢰할 수 있는 내부 네트워크에서 Dummy 데이터로 시험한다. 실제 ARC 비밀번호·개인정보를 넣거나 공유기 외부 포트를 열지 않는다. IP 허용 목록은 팀원의 개인 인증을 대신하지 않는다. 앱은 직접 HTTP 요청을 사용하며, `Origin`이 붙는 다른 웹사이트의 JavaScript 요청은 허용하지 않는다.

## 4. 결과와 완료의 차이

| 항목 | 현재 동작 |
|---|---|
| 모든 5개 프로그램 × 3개 연령 | 기존 내부 계산기로 점수·통계·코칭·차트를 생성 |
| Compression Only / Ventilation Only | 실제 횟수와 기존 tester Pass를 모두 충족하면 완료 |
| CPR 계열 | 결과를 제공하되 완전한 cycle 완료 규칙이 미정이므로 `pending_policy`. 완료를 임의로 만들지 않음 |
| ARC 제출 | `submit_arc.status=disabled`, `ok=false`, `error=arc_contract_pending` |
| 대기 | 앱은 최대30초 대기 후에도 같은 시도의 결과를 조회 가능. 작업을 자동 취소하는 시간이 아님 |
| 차트 | 실제 로컬 JSON 파일을 서명 URL로 제공. 발급부터300초; 만료 시 인증된 API로 새 링크 발급 |

계산 성공이 프로그램 완료나 ARC 제출 성공을 뜻하지 않는다. 로그인한 다른 기기들은 공유 진도를 볼 수 있지만 서로의 훈련 결과를 자동으로 조회할 권한은 없다.

## 5. 저장·종료·재시작

- 기본 `var/local-server`에 로컬 DB, 설치·서명 키, 비공개 측정·결과·차트 파일을 보관한다. 이 폴더를 Git, 배포 ZIP, 공유 드라이브에 넣지 않는다.
- `Ctrl+C`로 정상 종료한다. 재시작은 기존 세션·접수 작업·결과를 보존하고 대기 작업을 다시 처리한다. 사용자가 직접 로그아웃하면 해당 세션이 폐기되고 Dummy 계정의 공유 진도가 초기화된다.
- 초기화 전의 늦은 결과는 보관하되 새 진도에는 반영하지 않는다. 로그아웃으로 이미 발급된 차트 링크의 남은300초 수명은 사라지지 않는다.
- 기존 차트 링크의 재시작 후 접근에는 같은 설치·주소·포트와 남은 유효시간이 필요하다. 계산 응답 안의 오래된 URL은 바뀌지 않으므로 앱은 `chart-link` API로 갱신한다.
- 자동 파일 삭제는 추가하지 않았다. 키 일부 삭제, DB 교체, `chmod 777`로 오류를 우회하지 않는다. 서버를 끈 뒤 설치 전체 단위로 보관·폐기를 결정한다.
- 강제 종료나 Mac 종료는 정상 종료와 다르다. 부모 프로세스를 강제 종료하면 DB Java 프로세스가 남을 수 있다. 재시작이 포트 충돌로 거부되면 실제 소유 프로세스를 확인하며, 알 수 없는 프로세스를 일괄 종료하지 않는다.

## 6. 기본 한도와 문제 확인

| 설정 | 기본값 | 의미 |
|---|---|---|
| `--calculation-body-bytes` | `1000000` bytes | 파일·폼 포장을 포함한 계산 HTTP 본문 한도 |
| `--artifact-bytes` | `8000000` bytes | 저장 객체 본문 한도 |
| `--storage-quota-bytes` | `1073741824` bytes | 로컬 객체 저장소의 파일 헤더·임시 파일을 포함한 총량 한도. DB·설치 키까지 포함한 폴더 전체 한도는 아님 |
| `--worker-lease-seconds` | `60`초 | 실행 중 작업 소유권 유지 설정. 앱의30초 대기와 다름 |

이 수치는 로컬 기본값이며 AWS 한도나 ARC 공식 계약값이 아니다. 실제 앱의 누적 파일이 HTTP 한도를 넘으면 `413`이다. 파일을 조각내거나 버리지 말고 실제 요청 크기를 확인해 한도 변경과 재검증을 진행한다.

| 증상 | 확인할 것 |
|---|---|
| 연결 거부 | 서버 터미널, 현재 IP·포트, 실행 모드 |
| Mac만 연결됨 | iPad IP 허용 목록, 같은 네트워크, 기기 간 통신 제한 |
| `401` / `403` | 세션 헤더·만료·로그아웃; 필요한 경우 새 로그인 뒤 같은 시도 재인가 |
| `409` | 완료한 훈련 재시작, 변경된 입력, 시도 상태 확인 |
| `503` | 저장 한도·파일/DB 상태·계산 실행기 오류. 기존 파일을 지우기 전에 오류 확인 |
| worker/DB 고장으로 서버 종료 | 장애를 숨기며 계속 접수하지 않는 동작. 원인 확인 뒤 같은 설치로 재시작 |

저장 한도에 도달해도 기존 결과 조회·동일 입력 재전송을 유지하도록 검증했다. 오류 공유 시 상태 코드와 정제된 메시지만 전달하고 세션 토큰, 복구 증표, 차트 서명 URL, 키 파일 내용은 공유하지 않는다.

검증 근거: [검증 결과와 승인 범위](VALIDATION.md).

### 운용 로그 확인

기존 시작 명령을 사용하면 API와 Worker가 주요 이력과 상세 진단을 같은 로컬 DB의 별도 기록 영역에 저장한다. 새 서버나 DB를 만들 필요가 없다. 변경된 코드를 사용하려면 기존 서버를 `Ctrl+C`로 정상 종료한 뒤 같은 명령·데이터 폴더로 다시 실행한다. 이전 stdout 기록을 소급해서 DB에 넣지는 않는다.

서버를 켜 둔 상태에서 **다른 터미널**로 조회한다. 기본 데이터 폴더·DB 포트를 쓰는 경우의 명령이다.

```sh
cd /Users/mac/arc_calculator_api
var/local-python/bin/python scripts/read_local_logs.py --limit 100
```

기본값은 UTC 기준 오늘이다. 예를 들어 한국 시간 오전9시 이전 기록은 UTC 날짜가 전날일 수 있다. 다른 날짜를 볼 때는 다음과 같이 지정한다.

```sh
var/local-python/bin/python scripts/read_local_logs.py --date 2026-09-10 --limit 100
```

`records`에 최대100개가 나오며 `next_cursor`가 문자열이면 같은 날짜의 다음 페이지가 있다. 그 값을 아래 자리로 교체한다. 한 번에 지정할 수 있는 건수는1~500개다.

```text
var/local-python/bin/python scripts/read_local_logs.py --date 2026-09-10 --limit 100 --after '<next_cursor 값>'
```

서버를 사용자 지정 설정으로 실행했다면 조회에도 동일한 `--data-dir <데이터_폴더>`와 `--db-port <DB_포트>`를 추가한다. HTTP 포트8000과 DB 포트8001은 다르다. 이 도구는 기존 로컬 설치를 읽기만 하며 AWS에 연결하거나 DB를 시작·초기화하지 않는다. 앱용 로그 조회 Endpoint는 추가하지 않았다.

| 기록 | 확인할 내용 |
|---|---|
| 주요 이력(`category=operation`) | 로그인 성공/거절, 로그아웃/진도 초기화, 훈련 생성/취소/재인가, 계산 접수/재전송/처리 결과, 완료의 공유 진도 반영 여부 |
| 상세 진단(`category=diagnostic`) | 기존 계산 단계, 바이너리 크기·관측 개수·처리 시간, 정제된 오류 종류·코드 위치 |
| 식별 정보 | `attempt_id`·`job_id`로 같은 훈련의 기록을 연결. `session_id`는 세션 식별자이며 로그인에 사용하는 `session_token`과 다름 |

비밀번호, 로그인 토큰, 복구 증표, 전체 요청·응답, 원본 바이너리, 차트 서명 URL과 임의 오류 메시지는 기록하지 않는다. 정상 조회마다 접근 이력을 전부 남기는 기능은 아니다. 계산 결과 전체는 기존 결과 조회 경로로 확인한다.

`/healthz`의 `operational_logs`에서 **현재 API 프로세스**의 기록 상태를 확인할 수 있다.

| 값 | 쉬운 설명 |
|---|---|
| `accepted` | 메모리 대기열에 받은 건수. 아직 DB 저장을 보장하지 않음 |
| `stored` | DB 저장 확인을 받은 건수 |
| `unconfirmed` | 저장 시도가 실패했거나 응답을 받지 못해 저장 여부를 확정할 수 없는 건수 |
| `dropped` | 대기열 포화 등으로 저장 대기열에 넣지 못한 건수 |
| `pending` | 아직 저장 확인 또는 실패 처리가 끝나지 않은 건수 |
| `running` | 현재 기록기가 새 로그를 받을 상태인지 여부 |

이 카운터는 Worker 합계가 아니며 재시작하면 초기화된다. 이미 저장된 DB 기록은 유지된다. 로그 장애만으로 정상 훈련이나 health 응답을 실패로 바꾸지 않는다. 터미널에 `operational_log_write_unconfirmed` 또는 `operational_logs_dropped`가 보이면 로그 기록이 완전하지 않을 수 있다. DB 상태와 디스크 여유를 확인하되 훈련을 다시 수행해서 로그를 복구하려고 하지 않는다.

기록은 자동 삭제하지 않는다. 로그 저장 실패·메모리 대기 중 강제 종료에는 일부 기록이 남지 않을 수 있다. DB 전체 고장이나 디스크 고갈은 훈련 데이터 저장에도 영향을 준다. 객체 파일 quota와 별개로 DB 용량·디스크 여유를 확인해야 한다. 보관기간과 삭제 기능은 후속 확정 사항이다.

## 7. 다른 Mac의 실행 준비·계산 도구

기존 환경을 덮어쓰지 않고 Python 3.12 가상환경을 준비하는 명령 형식이다. 최초 의존성 설치에는 다운로드가 필요하며 서버 실행 명령 자체는 설치하지 않는다.

```sh
python3.12 -m venv var/local-python
var/local-python/bin/python -m pip install -r requirements-local.txt
```

Java와 Javac, DynamoDB Local 3.3.1 배포 파일도 별도로 필요하다. 서버는 [고정 배포 파일 manifest](local_server/DYNAMODB_DISTRIBUTION_MANIFEST.json)의 전체 파일 지문을 검사한다. 버전명만 같은 미검증 파일을 허용하도록 manifest를 바꾸지 않는다. 기존 배포 폴더 경로는 `--dynamodb-home`으로 지정할 수 있다. 설치 방법·다운로드 출처는 실제 준비 대상에 맞게 검증하고, 저장된 사용자 DB를 새 DB로 대체하지 않는다.

선택 가능한 전체 CLI 설정은 다음으로 확인한다. 이 명령은 서버를 시작하지 않는다.

```sh
var/local-python/bin/python scripts/serve_local.py --help
```

HTTP Journey와 구별되는 기존 계산 도구도 유지한다.

```text
python scripts/run_local.py --cpr <파일.bin> --target adult --guideline ARC2025 --stage test
python scripts/console_viewer.py <bin과meta가있는폴더> --passing 80
```

`run_local.py`의 기본 stage는 `prod`이고 기존 원본 업로더에 연결되므로 부작용 없는 코어 확인에는 위처럼 `--stage test`를 명시한다. 이것은 서버의 로그인·영속 접수·결과 조회를 검증하는 명령이 아니다. `STAGE=test`를 실제 Journey의 저장 환경으로 무작정 지정하면 저장 확인이 실패할 수 있다. `console_viewer.py`는 기존 저장 자료를 보는 도구다.
