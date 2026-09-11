# 검증 결과와 유지 방법

2026-09-10 문서 통합 기준. 과거 실제 실행 증거와 이번 문서 정리 검증을 구별한다. 단계별 세부 계획·개인 리뷰·실행 JSON·patch는 로컬 원본에 보존한다. 이 문서의 AI CTO 판정은 사람의 보안 인증이나 AWS·ARC 운영 승인이 아니다.

## 1. 현재 로컬 Journey의 검증 범위

기본 `scripts/serve_local.py`를 사용자 서버와 다른 임시 설치·포트에서 실행했다. 제품 HTTP·DynamoDB Local·별도 Worker·파일 저장·차트 서명기를 사용하고 저장소의 기록된 실측 바이너리를 업로드했다. 실물 앱·마네킨을 조작한 시험은 아니다.

| 실행 | 당시 결과 |
|---|---|
| L4 `scripts/validate_local_integration.py --suite all` | **2343 passed, 4 xfailed, 9 subtests passed, 4947 warnings, 168.48초, exit 0** |
| 15개 조합 최초 집중 실행 | 15 passed, 9.74초. 이후 위 전체 회귀에도 포함 |
| 전체 수집 이후 수정한 보안 fixture의 집중 재실행 | 2 passed, 11.78초. 위 전체 수에 더하지 않음 |
| 당시 독립 AI CTO의 배포물 시험 | 29 passed, 0.54초 |

전체 묶음은 `tests`, `scripts`, `local_server_tests`, `integration_tests`, `http_pipeline_tests`, `transport_integration_tests`를 포함했다. 모든 테스트가 실제 CLI를 띄운다는 뜻은 아니다. 기존 예상 실패 4개를 지우거나 완화하지 않았으며 warning은 고정 botocore의 UTC deprecation으로 기록됐다.

전체 수집 후 보안 quota 시험의 성인 호흡 입력 경로를 `vo_1.bin`에서 `adult_vo_1.bin`으로 정정하여 2개 시험을 재실행했다. 제품이나 바이너리 내용을 바꾸지 않았고, 최초 전체 실행이 이 후속 수정을 이미 검증했다고 주장하지 않는다.

| 경계 | 확인한 내용 |
|---|---|
| 계산·전송 | 5프로그램×3연령, multipart·기존 base64 form, CPR/AED 기록 파일, 자동 Worker |
| 응답 | 현재 core 직접 출력과 값·JSON 자료형·null·코칭 비교. 제품 자식에 계산 대역을 주입하지 않음 |
| 차트 | 실제 HTTP GET의 차트 JSON·자료형·서명된 본문 hash 비교 |
| 완료 분리 | Only는 실제 목표+tester Pass, CPR은 실제 점수와 별도 pending_policy |
| 공유 진도 | 다른 로그인에서 완료 확인, 완료한 슬롯의 새 훈련409, 다음 선택·취소 |
| 재시도·권한 | 같은 입력은 같은 결과, 다른 입력409, 잘못된 인증/소유권으로 기존 상태 변경 없음 |
| 복구 | 소유 Worker를 SIGSTOP한 채 queued 접수, 정상 종료·같은 설치 재시작 후 자동 처리 |
| 로그아웃 | epoch 초기화 전 작업의 결과는 저장하되 새 진도에는 PROGRESS_RESET |
| 파일 보호 | 원본 bytes/hash, 0700/0600·소유 UID·단일 hardlink, 비밀 없는 로그, quota 이후 기존 읽기·replay |
| 차트 권한 | 변조404, 원본/키 경로 비공개, logout 후 남은 TTL 유지 |

첫 업로드는 영속 접수 직후 Worker가 이미 완료했다면200, 처리 중이면202다. 시험을 무조건202로 제한하지 않는다. Worker를 멈춰 queued를 만든 시험은202를 요구하고 최종 GET·확정 결과 replay는200·같은 결과를 검증했다.

L1/L2/L3 시험은 최종 거래 경합·세션 만료/재인가·DB lease 갱신/상실·파일 손상/원자 쓰기·실제 TCP 보호를 함께 확인했다. 정확한300초 경계는 L2의 제어 clock 시험 근거이며 L4에서300초를 다시 기다렸다고 표현하지 않는다.

## 2. 적대적 검토에서 보존할 결론

L3 최초 AI CTO 판정은 **REVISE**였다. 죽은 Worker의 공유 Condition 응답을 부모가 무제한 기다리는 종료 교착을 실제 CLI 시험으로 확인했다. 중단 통지를 one-way pipe로 바꾸고 제한된 종료·회수를 적용한 뒤 실패했던 동일 시험의 재통과로 **PASS**를 받았다. 최초 반려 기록을 삭제하거나 처음부터 통과한 것으로 바꾸지 않는다.

L4 독립 AI CTO는 제품을 변경하지 않고 실제 소스·실행 근거·지문을 확인하여 로컬 인수를 PASS로 판정했다. 그 후 앱 명세와 AWS 후속 안내도 작성·검토됐다. 이번 문서 통합이 별도의 새 CTO 보안 인증을 수행했다는 뜻은 아니다.

당시 `docs/local_journey/L4_MANIFEST.json`의 SHA-256은 `91ee0aa343000c438c3d19c04d7d01e4b7c65f1c42b5eee89b3b22ea6fb1961c`, 대상은 코드·시험31개였다. 이 원본은 로컬 보관 폴더에 그대로 있다. 당시 Git 관측값과 이번 정리 시작 시점의 값이 다르므로 작업 전체에서 index가 불변이었다고 주장하지 않는다.

남는 한계는 실제 iPad/Android·마네킨 현장 인수, CPR 완료 cycle 규칙, AWS 실행 연결·배포·ARC 실제 제출, 운영 retention/규모/권한이다. LAN HTTP는 암호화되지 않고, 부모 SIGKILL 후 Java DB orphan과 모든 OS/SDK 동작의 강제 hard deadline도 해결했다고 주장하지 않는다.

## 3. 참고 HSTM과의 비교 근거

2026-09-08의 읽기 전용 저장 규격 조사에서 참고 checkout HEAD는 `08b4e8a`, 최신 로컬 `origin/main` ref는 `01d336daa1f2360d87c8b6efb2f4c7ac44d52c30`였고 파일 차이가 없었다. fetch나 실제 운영 버전 조회는 하지 않았다. `5b6e4cd`의 계산 전 원본/메타 저장과 `f1e204c`의 UTC 날짜 경로 분리를 확인했다. HSTM의 과거 제출 body/header 로그를 ARC 보안 예외보다 우선하여 복원하지 않는다.

저장·삭제 요구는 HSTM 기준을 유지한다. 그러나 소스 조사만으로 실제 S3 lifecycle·로그 retention·새 진도/멱등성 DB의 보관기간을 확인하지 못했다. 배포 이력이 없다는 답변을 미배포·영구 보관의 증거로 삼지 않는다.

별도 계산 차등 비교는 다음123개다.

| 비교 | 사례 |
|---|---:|
| 5guideline×3연령×3훈련 | 45 |
| 전체 guideline의 CPR 최소량 경계 | 60 |
| 기록 binary·일부 AED | 8 |
| VP 구간 | 6 |
| ERC 소아/영아 초기 호흡 | 4 |

27개는 승인된 ARC 최소량 예외였고 JSON Pointer 차이1,638개를 모두 기록했다. 나머지96개는 값·자료형이 같았다. 경로 무시 목록이나 수치 허용오차로 통과시키지 않고 참고 가중치의 독립 유리수 산술로 cycle·part·total 기대값을 계산했다. 코칭은 검산한 총점과 기존 실측 신호로 비교했다. **미승인 차이0·실행 오류0·차단된 부작용 시도0**이라는 당시 결과를 보존한다.

이 비교는 `main.run_calculator`와 HTTP legacy 변환 helper 범위다. 실제 네트워크 HTTP·인증·Certification·제출 시험123개라는 뜻은 아니다. 서로 다른 해석기의 참고/현재 worker에서 네트워크·AWS·subprocess·파일 변경을 막고 소스 지문을 전후 대조했다. ARC 최소량 미달과 VP가 함께 있는 추가 예외는 actor-aware 독립 기대값 없이 허용하지 않는다.

회귀에 쓰는 다음 JSON은 **Git에 유지하는 테스트 입력·기대값**이다. 진행 기록이라는 이유로 로컬에만 옮기지 않는다.

| `tests/fixtures/reference_parity/` 파일 | 책임 |
|---|---|
| `guards_manifest.json`, `guards_report.json` | 부작용 guard 입력·검증 기록 |
| `parity_manifest.json` | 소스·입력·binary·VP·검증기 지문 |
| `parity_report.json` | JSON Pointer/자료형/누락 차이·예외 산술 |
| `reference_results.json` | 참고 계산 관찰값 |
| `approved_expectations.json` | 승인된27개 예외의 독립 기대값 |
| `current_results.json` | 당시 현재 출력. 독립 정답을 대체하지 않음 |
| `golden_update_report.json` | 기존8개 dataset 결과의 이전/이후 지문·변경 근거 |

## 4. 이후 검증 명령

Python3.12와 `requirements.txt`, `requirements-dev.txt`, 로컬/통합 시험 의존성을 설치한 별도 개발 환경을 사용한다. 기본 `pytest.ini` 수집 범위는 `tests scripts`이며 그것만 실행하고 모든 로컬 통합 시험을 했다고 보고하지 않는다.

```sh
python -m pytest -q tests/test_mock_route_contract.py tests/test_mock_artifact.py
python -m pytest -q tests/test_reference_parity_regression.py tests/test_result.py
```

전체 실제 로컬 인수는 검증된 DynamoDB 배포 경로를 지정한다. 사용자 실행 서버·DB를 공유하지 않는다.

```text
python scripts/validate_local_integration.py --dynamodb-home <검증된_DynamoDB_Local_경로> --suite all
```

참고 비교를 새로 생성할 때만 아래 도구를 사용한다. 참고 경로의 실제 소스와 실행 환경을 먼저 확인하고 기존 전체 fixture를 덮지 않을 별도 `--output-dir`를 사용한다. `--case`는 부분 비교, `--no-oracle`은 두 전체 출력 파일 생략이다.

```text
python -B scripts/verify_reference_parity.py --guard-check-only --output-dir <별도_검증_폴더>
python -B scripts/verify_reference_parity.py --output-dir <별도_검증_폴더>
```

종료0은 참고/정확한 승인 예외와 일치,1은 미승인 차이,2는 실행 오류·보호 위반·소스 변경이다. 양쪽에서 같은 실행 오류가 난 사례도 성공이 아니다. 현재 출력만으로 정답 파일을 재생성하지 않는다. `analysis/arc-run/run_arc.py`·`analysis/v1-baseline/run_baseline.py`는 과거 도구로 남으며 최신 기대값 갱신 기준으로 사용하지 않는다.

## 5. 이번 문서 정리 검증

원본192개 Markdown과36개 JSON·7개 patch를 로컬에 보관했다. 해시·기존 Git 등록 정보는 `.documentation-backup/2026-09-10-document-consolidation/ORIGINALS.json`, 통합 대응표는 `SOURCE_MAP.json`이다. 코드/시험이 읽는 JSON과 IAM 참고 자료5개는 원본 사본과 별도로 활성 경로에도 유지한다. 다른 fixture·dataset·Python 소스는 정리 대상이 아니다.

문서 정리 결과는 다음과 같다. 제품 기능 변경 이전의 확인이며 새 운용 로그 DB 구현 결과가 아니다.

| 확인 | 결과 |
|---|---|
| 활성 Markdown | 192개 원본에서 핵심9개로 통합 |
| 원본 보존 | 235개 모두 원문 bytes·SHA-256 일치; 원래 .gitignore도 추가 보관 |
| Git 추적 해제 | 선택한 원본226개만 제거. 다른247개 index entry의 mode·blob·stage·경로 유지 |
| 코드·데이터 | 정리 대상 외 파일은 .gitignore 변경을 제외하고 불변. L4 제품·시험31개 지문 일치 |
| 문서 링크 | 로컬 링크40개 확인; 앱팀 문서의 실제 서버 주소 없음 |
| 제외 규칙 | 로컬 보관 폴더·과거 baseline JSON 제외. 필수 JSON/fixture와 핵심9개는 제외하지 않음 |
| README 예외 | 이 Mac의 전역 README 무시 규칙에 대응해 저장소 .gitignore에서 루트 README만 추적 가능하게 함 |
| 관련 회귀 | `tests/test_mock_route_contract.py` + `tests/test_mock_artifact.py`: **86 passed, 1.43초, exit 0** |
| Git 작업 | 새 문서/설정 staging·commit·push 없음; 첫 commit은 여전히 없음 |

새 핵심9개와 변경한 .gitignore는 사용자가 검토하여 등록할 작업 파일로 남긴다. 원본 추적 해제를 일반 코드 staging 권한으로 확대하지 않았다. 기계 확인 결과는 로컬 보관 폴더의 `CLEANUP_VERIFICATION.json`에 남긴다.

문서 통합 당시에는 N03~N06 답변과 운용 로그 구현이 남아 있었다. 이후 사용자 답변으로 기존 구조 재사용·주요/상세 진단·로그 장애의 훈련 차단 금지·기간 확정 전 자동 삭제 없음이 확정됐다. 후속 기능 검증은 아래에 별도로 기록한다.

## 7. 운용 로그 DB 추가 — 2026-09-10

기존 상태/결과 참조 DB·비공개 파일 저장 코드를 재사용하고 로컬 API·Worker의 운용 로그 기록과 관리자 읽기 CLI를 추가했다. 실제 AWS·ARC 연결, 배포, 실물 앱/마네킨 인수는 수행하지 않았다. 이번 결과는 구현과 자체 반증 점검이며 과거 독립 AI CTO 승인을 새 변경에 재사용하지 않는다.

| 실행 | 결과 |
|---|---|
| 초기 로그·기존 API/Worker 집중 검사 | 285 passed, 2.26초 |
| 실제 CLI의 로그 저장·재시작·읽기 조회 | 1 passed, 9.56초. 아래 전체 검사에도 포함 |
| 오류 처리 보완 후 집중 검사 | 325 passed, 2.87초 |
| 최종 전체 로컬 검사 | **2363 passed, 4 xfailed, 9 subtests passed**, 329.57초 |
| 문서·보존 확인 | 핵심 Markdown9개, 로컬 문서 링크42개 유효. 기존 계산 기준·fixture·앱 API 문서 등 보호 대상49개 변경 없음 |

집중 검사와 전체 검사 숫자는 서로 더하지 않는다. 기존 예상 실패4개를 삭제/완화하지 않았다. 경고5067건은 고정 botocore의 UTC deprecation이며 이번에 SDK 버전을 바꾸지 않았다. 전체 명령은 기존 실행기를 사용한다.

```sh
/private/tmp/arc-implementation-py312/bin/python scripts/validate_local_integration.py \
  --dynamodb-home /Users/mac/arc_calculator_api/var/dynamodb-local-3.3.1 --suite all
```

해당 Python 경로는 이 Mac의 검증용 환경이며 다른 PC에 존재한다고 가정하지 않는다. 실행기는 새 임시 DB·loopback 포트를 소유하고 기존 사용자 설치를 사용하지 않는다.

| 반증 시나리오 | 확인한 동작 |
|---|---|
| 로그 저장 예외·client 생성 실패 | 업무용 client와 분리, 저장 미확정 집계. 정상 DB 연결·정리 유지 |
| 로그 DB 쓰기 멈춤·대기열 포화 | 실제 내부 계산 성공, 결과 재조회·재전송 동일, 목표 충족 Only의 완료/진도 반영 유지 |
| stderr 멈춤/오류·기록 스레드 시작 실패 | 로그 접수/상태 확인이 출력 완료를 기다리지 않음. 훈련 오류로 변환하지 않음 |
| 여러 요청 동시 기록 | 요청 문맥과 시도 식별자가 다른 요청에 섞이지 않음. 예외 뒤 문맥 해제 |
| 비밀값·상세 진단 | 비밀번호·토큰·복구 증표·raw body·서명 URL 제외. 허용된 개수·시간·정제된 코드 위치 유지 |
| 실제 API+spawn Worker | 로그인·생성·재전송·계산·취소·진도 이력과 진단이 실제 DB에 저장됨 |
| 종료·재시작·logout 재전송 | 기존 계산 응답과 DB 로그 유지. logout 응답 재전송이 추가 초기화를 주장하지 않음 |
| 읽기 경계 | 날짜/건수/cursor 제한, 다른 환경·변조 행 거절, 원본/로그 덮어쓰기·TTL·새 공개 경로 없음 |
| 기존 회귀 | 계산·자료형·null·코칭·파일/차트·인증·epoch·lease/fence·HTTP 보호 검사 유지 |

중간 실패도 보존한다. 최초 집중 검사5개는 Worker 연결 인자를 엄격히 검사하던 fixture의 새 로그 역할 반영 누락이었으며 정확한 인자 검사를 갱신했다. 최초 전체 검사에서는2356개 통과·7개 실패가 발생했다. 그중4개는 로그용 Job 식별자 접근이 기존 lease/오류 처리보다 먼저 실패하는 문제여서 제품의 선택적 문맥 처리를 보완했다. 나머지3개는 종료 검증 fixture가 새 연결 인자를 받지 않아 본래 종료 경로에 도달하지 못했으므로 명시적인 역할 검사를 유지한 채 수정했다. 기존 실패/종료 assertion을 제거하지 않았다.

실제 CLI 시험의 최초 실행은 sandbox의 loopback bind 제한으로 제품 실행 전 차단됐다. 허용된 임시 포트 재실행 중 한 번 DB 연결 준비 오류가 있었으나, 오류 종류/코드 위치만 추적한 별도 재현과 이후 단독·전체 실행에서 재현되지 않았다. 원인을 확정했다고 보고하거나 이를 근거로 timeout·보호 설정을 완화하지 않았다.

범위의 한계: 로그만 실패하는 상황에서 훈련을 유지한다. 전체 DB 장애·디스크 고갈까지 훈련 저장이 성공한다는 뜻은 아니다. RAM 대기열의 과부하/강제 종료·저장 응답 미확정으로 기록이 누락될 수 있으며 로그의 무손실 전달을 보장하지 않는다. `/healthz`의 기록 카운터는 API 프로세스만 나타내고 재시작하면 초기화된다. 영속 로그 행은 자동 삭제하지 않는다. 원격 환경의 전달 수명·권한·용량 격리·알림은 후속 검증 대상이다.

실행 로그·변경 전 지문·최종 변경 patch·확인 JSON은 `.documentation-backup/2026-09-10-operational-logs/`에 보관한다. Git/배포물에서 제외한다. 이번 구현 작업자는 staging/unstaging·commit·push를 실행하지 않았으며 작업 중 다른 경로에서 바뀐 index도 되돌리지 않았다.


## 8. 코드 품질과 배포 준비 — 2026-09-11

새로 합류한 엔지니어 관점에서 소스·핵심 문서·배포 도구를 조사하고 작은 범위의 실제 개선을 반영했다. 작업 시작의 `git status --short`는 비어 있었다. 기존 사용자 변경을 덮거나 Git index를 조작하지 않았다. 이 절이 별도 `REFACTORING.md`를 대신한다. AWS 절차는 기존 [DEPLOY_GUIDE.md](DEPLOY_GUIDE.md)에 통합하여 핵심 Markdown 9개를 유지한다.

사용자는 AWS 운영값·배치가 아직 미정이며 **기존 구성 기반 제안과 안전한 준비까지만** 요청 범위를 확정했다([D37](DECISIONS.md)). 실제 AWS runtime 전체 연결을 완료했다고 보고하지 않는다. 별도 AI 검토자·CTO 또는 사람의 운영 승인을 수행하지 않았다.

### 발견 사항과 수정 범위

P1은 전체 AWS 훈련 실행 전 해결할 항목, P2는 작게 수정할 수 있는 배포 신뢰성·구조 문제, P3는 측정이나 후속 검토가 필요한 후보를 뜻한다. 줄 번호는 이번 수정 후 기준이다.

| 우선순위·위치 | 구체 근거, 드러나는 조건과 영향 | 처리·예상 범위·동작 보존 |
|---|---|---|
| P1 `mock_journey/runtime.py:16 get_application`, `worker_runtime.py:6 get_worker / :12 get_relay` | 제어 서비스에 CalculationService가 없고 Worker·Relay는 항상 오류. AWS 설정값만 넣어 전체 Journey를 배포할 수 없음 | **사용자 답변에 따라 별도 연결 작업으로 보류.** 여러 런타임·IAM·트리거·로그 수명 범위. 기존 공용 조립을 재사용하고 API·저장·lease/epoch를 바꾸지 않는 구성 제안 작성 |
| P2 `scripts/deployment_preflight.py:161 validate_configuration` | 변경 전 memory=1/10241, timeout=901, retention=2가 실제로 통과. 늦은 AWS 오류 때문에 코드 갱신 뒤 설정만 실패할 수 있음 | 서비스 한도·허용 보관 일수·환경변수 key/UTF-8 총량 검사 추가. 두 shell이 AWS 호출 전에 공유 검사. 유효한 기존 설정값은 유지 |
| P2 `scripts/deploy_arc_lambda.sh:110` 로그 보관 | 이전에는 양의 보관 일수 필수·항상 정책 변경. 미정 정책을 유지한 코드 배포가 불가능 | `log_retention_days=null`이면 보관 명령을 실행하지 않음. 숫자를 명시한 기존 동작·실패 처리는 유지. 기존 만료 정책 제거 기능을 만들지 않음 |
| P2 `scripts/deploy_arc_lambda.sh:84` 역할 비교 | 경로 없는 ARN을 조합해 비교하므로 `role/service-role/Name`인 정상 기존 역할도 거절 | optional `role_arn`을 추가. 계정·마지막 역할명·AWS 조회 ARN이 모두 일치해야 함. 역할 생성·권한 확대 없음. 변경 범위는 설정 검사·shell·회귀 |
| P2 `scripts/build_mock_artifact.py:247 build_artifact` | ZIP_STORED와 완성 ZIP의 `read_bytes()` 때문에 전송량·최고 메모리 증가. 기존 ZIP은 50MiB 미만이었으므로 기존 배포 실패로 단정하지 않음 | DEFLATE 압축·스트리밍 SHA-256, 로컬 패키지 한도 초과 거절. 입력 allowlist·RECORD hash·권한·출력 충돌 보호 유지. 아래 실측 |
| P2 `local_server/runtime.py` → `mock_journey/execution_definitions.py:10 execution_catalog` | 제품의 15개 정의가 로컬 프로세스 감독 모듈에 있었고 그 모듈은 Lambda ZIP 제외. AWS 조립 시 코드 복사·로컬 감독기 의존 위험 | 정의만 공용 파일로 이동. 기존 import 위치에서 재노출하며 문자열 버전·15개 조건·pending 정책 그대로. 키·운영 한도·client 생성은 이동하지 않음 |
| P2 `scripts/deploy_arc_lambda.sh` 의존성 설치 | 직접 패키지 2개만 고정되어 같은 소스도 설치 날짜에 따라 간접 의존성 버전이 달라질 수 있음 | 이번 기준 환경과 같은 7개 간접 버전을 `constraints-lambda.txt`로 고정하고 기존 pip에 `-c` 추가. 새 패키지·SDK 업그레이드 없음. wheel hash lock·취약점 감사 완료를 뜻하지 않음 |
| P3 `mock_journey/jobs.py:528 _due_page` | 한 페이지의 GSI Query 뒤 행마다 최신 GetItem. N개면 Query+N회 조회인 **네트워크 병목 후보** | AWS 지연·호출량 미측정. 다음에 페이지 크기별 p95·읽기 비용 측정. GSI 지연·lease 확인을 위해 최신 행 재확인이 필요하므로 이를 삭제하는 최적화는 보류. batch로 바꾸면 미처리 키/일관성 시험 필요 |
| P3 `services/guide_prompts.py:73 _load_prompt_book_file` | 계산마다 코칭 JSON 파일 읽기·파싱. **반복 I/O 후보**이며 현재 부하 병목이라는 측정 없음 | 실제 계산 프로파일에서 비중 측정 후 결정. 즉시 캐시하면 파일 교체·실패 후 복구·mutable 반환값 의미가 달라질 수 있어 미변경 |
| P3 `mock_journey/worker.py:74 _process`, `calculators/merge_calculator.py:17 calculate_total_score` | 각각 132줄·211줄. Worker는 복구·소유권·차트·평가·최종 거래를 한 흐름에서 처리하여 부분 이동 시 순서 오류 위험 | 길이만으로 결함으로 단정하지 않음. 상태별 실패 시험을 먼저 유지하며 단계 추출 검토. 이번에는 계산식·트랜잭션 흐름에 손대지 않음 |
| P3 `scripts/deploy_arc_lambda.sh:50` / `deploy_arc_api_gateway.sh:40`의 `aws_value`, `aws_quiet` | 같은 AWS 오류 정제 wrapper가 두 곳에 존재해 향후 수정 불일치 가능 | 현재 불일치 없음. 공통 수정 수요가 생기면 작은 shell helper로 통합 가능. 지금은 별도 helper 의존을 추가하지 않고 두 스크립트의 대역 오류 회귀로 보호 |

`auth.extract_bearer`와 `handler._single_header`도 duplicate header 판독 모양은 비슷하지만 누락·잘못된 값의 오류와 허용 규칙이 다르다. 이를 단순 중복으로 합치지 않았다. `legacy_bridge.py:3`의 handler 파서 helper 역참조는 계층 유지보수 후보다. 현재 공개 handler가 router를 호출 시점에 import하고 모든 배포 entrypoint import가 성공했으므로, 즉시 발생하는 순환 import 장애로 단정하지 않는다.

### 핵심 변경 전후 코드

배포 ZIP의 내용은 유지하고 포장·해시 메모리만 바꿨다.

```python
# 이전
archive.writestr(info, data)  # ZIP_STORED
zip_bytes = staged_zip.read_bytes()
manifest["zip"] = {"sha256": _sha(zip_bytes), "size": len(zip_bytes)}

# 이후
archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
with staged_zip.open("rb") as stream:
    zip_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
manifest["zip"] = {"sha256": zip_sha256, "size": zip_size}
```

실제 코드에는 압축 해제 내용 250MiB·직접 업로드 ZIP 50MiB 검사도 있다. 출력 게시 전에 실패하며 기존 파일을 덮지 않는다. 추가 원격 layer·계정 제한까지 검증한 것은 아니다. [Lambda ZIP 한도](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)

```python
# 이전: 양수만 검사하므로 지원하지 않는 AWS 값도 통과
for key in ("memory_mb", "timeout_seconds", "log_retention_days"):
    _integer(calc[key])

# 이후: 일반 Lambda 한도, 보관 일수의 별도 의미를 검사
for key in ("memory_mb", "timeout_seconds"):
    _integer(calc[key])
if not 128 <= calc["memory_mb"] <= 10240 or calc["timeout_seconds"] > 900:
    _fail("LAMBDA_LIMIT_INVALID")
retention = calc["log_retention_days"]
if retention is not None and (type(retention) is not int or retention not in _LOG_RETENTION_DAYS):
    _fail("LOG_RETENTION_INVALID")
```

로컬 실행 정의는 함수 본문 그대로 이동했다. 저장된 Job과 연결되는 `arc-local-projection-v1`·adapter/profile 버전도 유지했다.

```python
# 이전: local_server/runtime.py 안에 15개 정의 생성 함수가 존재
# 이후: 기존 호출부가 같은 이름을 계속 사용
from mock_journey.execution_definitions import PROJECTION_VERSION, execution_catalog
```

### 실측과 회귀 결과

변경 전 전체 검사를 먼저 끝낸 후 코드를 수정했다. 최초 실행은 sandbox의 loopback bind 금지로 제품 테스트 시작 전에 실패했고, 허용을 받아 별도 임시 DB에서 재실행했다. 사용자의 기존 서버·DB·저장 파일을 시험 대상으로 사용하지 않았다.

| 검증 | 결과 |
|---|---|
| 변경 전 전체 `--suite all` | **2363 passed, 4 xfailed, 9 subtests passed**, 5063 warnings, 191.88초, exit 0 |
| 변경 후 배포·구성 집중 검사 | **234 passed**, 26.24초, exit 0 |
| 변경 후 전체 `--suite all` | **2384 passed, 4 xfailed, 9 subtests passed**, 5065 warnings, 197.34초, exit 0 |
| 실제 의존성 ZIP | 2198개 파일, 13,738,339 bytes. CRC 검사·모든 entry SHA-256 확인. 이전 ZIP의 2197개 파일 내용 동일; 실행 정의 파일만 추가 |
| ZIP 독립 import | 저장소 경로 없는 `python -I -B` + 추출 폴더에서 entrypoint 5개·실행 정의 15개 확인. SDK client 생성 0, network guard 적용. `local_server`를 import하지 않음 |
| 설치 의존성 확인 | 검증 Python 환경에서 `pip check`: 깨진 요구사항 없음. 실제 ZIP 의존성 9개와 requirements/constraints 일치. 새 패키지를 설치하거나 AWS 자격 증명을 사용하지 않음 |
| 정적·문서 검사 | Python 198개 파일 compile, 두 shell `bash -n`, `git diff --check` 통과. 핵심 Markdown 9개·로컬 링크 51개 유효 |

전체 묶음은 계산·입출력/자료형/null·15개 조합·인증/소유권·중복/epoch·lease/fence·로컬 실제 HTTP/DB/Worker/차트·로그 실패를 포함한다. 변경 후 새 회귀 21개는 배포 한도·역할 binding·로그 보관 불변·ZIP 게시 경계를 겨냥했다. 기존 기대값이나 4개 xfail은 바꾸지 않았다. 경고는 같은 botocore UTC deprecation이며 DB retry 횟수에 따라 수가 다를 수 있다. 전체 실행 시간 차이를 제품 성능 변화로 해석하지 않는다.

패키징 자체는 **같은 수정 후 소스·동일 의존성 2198개 파일**을 이전/이후 builder로 각각 독립 Python 프로세스에서 3회 생성하여 비교했다. 테스트 fixture 패키지가 아니라 기존 설치된 실제 SDK 파일 사본을 사용했다. 최초에는 사용 중인 설치에 생성 bytecode가 있어 builder가 `FORBIDDEN_DEPENDENCY_FILE`로 거절했다. 이 보호를 완화하지 않고 원본 설치를 보존한 새 임시 사본에서 생성 bytecode만 제외하여 측정했다.

| 동일 입력 패키징 측정 | 이전 | 이후 |
|---|---:|---:|
| ZIP 크기 | 21,126,585 bytes | 13,738,339 bytes (**35.0% 감소**) |
| 프로세스 최고 RSS 중앙값(macOS bytes) | 73,007,104 | 54,067,200 (**25.9% 감소**) |
| 빌드 시간 중앙값 | 0.549초 | 0.839초 |

압축으로 이 환경의 빌드 CPU 시간이 약 0.29초 늘었다. 전송량·메모리를 줄이는 교환이며 API 계산 속도 향상 주장은 아니다. 각 방식의 3회 ZIP hash는 동일했다. Python/zlib 버전이 다른 시스템의 압축 bytes까지 동일하다고 보장하지 않는다. 전체 파일 내용을 메모리에 보관하는 기존 검증 구조는 여전히 남아 있으며, 이번에는 완성 ZIP의 추가 사본만 없앴다.

실행한 대표 명령은 다음과 같다. 모든 경로는 이 Mac 기준이며 AWS 명령은 포함하지 않는다.

```sh
cd /Users/mac/arc_calculator_api
/private/tmp/arc-implementation-py312/bin/python scripts/validate_local_integration.py \
  --dynamodb-home /Users/mac/arc_calculator_api/var/dynamodb-local-3.3.1 --suite all
/private/tmp/arc-implementation-py312/bin/python -m pytest -q \
  tests/test_mock_artifact.py tests/test_deployment_preflight.py tests/test_deployment_preflight_security.py \
  local_server_tests/test_runtime.py tests/test_mock_assembly.py
/private/tmp/arc-implementation-py312/bin/python -m pip check
bash -n scripts/deploy_arc_lambda.sh scripts/deploy_arc_api_gateway.sh
git diff --check
```

배포 shell 회귀는 PATH를 명시적인 가짜 AWS/pip 실행기로 치환한 테스트다. 실제 배포 명령으로 실행하지 않았다. 실제 ZIP 빌드·측정·사전 오류 재현 기록은 `.documentation-backup/2026-09-11-quality/`에 있으며 Git/배포물에 포함되지 않는다.

### 남은 검증과 다음 순서

1. AWS 운영값·역할 배치·외부 팀원 접근 체계를 확정한 뒤 공용 API/Worker/Relay와 AWS 로그 전달을 연결한다. DB 스키마·공개 API·인증 변경이 필요하면 별도 검토한다.
2. Linux Python 3.12 Lambda에서 import·실제 응답 크기·인증/파일 권한·중복/복구·전체 훈련을 인수한다. 이번 Mac의 pure Python ZIP 검사는 Linux 실행 성공이 아니다. 새 wheel 다운로드·서명/출처·보안 감사도 수행하지 않았다.
3. 실제 AWS p95·요청량·DynamoDB 호출/비용을 측정한 후 due 조회와 코칭 로드 후보를 판단한다. 계정·리전·도메인·운영 한도는 임의로 입력하지 않았다.
4. 실물 앱/마네킨 시험, CPR cycle 완료 규칙, ARC 계약과 실제 제출은 별도다. 현재 `pending_policy`·`submit_arc: disabled`를 그대로 유지한다.

전용 lint/type-check 설정은 저장소에 없어서 임의 규칙이나 새 도구를 도입하지 않았다. Python 문법 compile, shell 문법, 문서 링크와 Git 공백 검사를 수행하며 정적 타입 검사 통과로 표현하지 않는다. 실제 AWS CLI·배포 shell 실행, 자원 변경·배포, commit·push 및 새 push 배포 자동화 활성화는 하지 않았다. 기존 `develop`/`main` push workflow는 그대로 남아 있어 사용자가 나중에 Secret/권한 연결 전에 확인해야 한다.

## 9. Dependabot 검증 CI — 2026-09-11

**로컬 구현·검증 완료, GitHub CI 실행 미완료, 세 PR 및 조합은 조건부 가능, AWS 배포 준비 미완료**다. 원격 검사는 이번에 실행하지 않았다. 앞 절의 앱 전체 시험과 이번 CI 검증의 소스·범위는 다르다. 도입 순서와 별도 승인 경계는 [DEPLOY_GUIDE 부록 F](DEPLOY_GUIDE.md#부록-f-dependabot-검증-ci-도입과-승인-후-원격-확인)에 둔다.

### 구현과 보존 범위

| 파일 | 변경 목적 |
|---|---|
| `.github/workflows/validate_actions.yml` | 배포와 분리된 PR 검사. 읽기 권한·실제 Secret 미사용·15분 제한·PR별 중복 취소. 후보 checkout/setup-python을 실제 `uses`에 연결 |
| `requirements-ci.txt`, `scripts/install_actionlint.py` | 소스별 기존 의존성에 고정 검증 도구를 추가. actionlint 공식 asset의 checksum을 확인한 후 설치 |
| `scripts/actions_contracts.json`, `scripts/validate_actions.py` | 정확한 공식 metadata·runtime·입력 검사, 새 참조와 smoke 연결, YAML 중복/구조·Python/shell 문법 및 배포 인증 경계 보존 검사 |
| `scripts/run_actions_regression.py`, `tests/test_actions_regression.py` | `STAGE=test`와 수집 전 SDK/연결 차단을 적용해 기존 preflight 회귀 재사용. 대역·오류 정제·정확한 차단 원인 검사 |
| `tests/test_validate_actions.py` | CI 선도입·개별/전체 변경 허용, 미검증 참조·검사 삭제/우회·권한/인증 변경 거절 등 반례 |
| `docs/VALIDATION.md`, `docs/DEPLOY_GUIDE.md` | 이번 결과와 승인 후 실행 절차만 통합 |

시작 시 로컬은 `develop@a561764909e32f6cd2fd7976a102d890f294958b`, 작업 트리와 index가 깨끗했다. 기존 배포 workflow·Dependabot 설정·앱 코드·기존 테스트/fixture·IAM 예시·결정 문서는 변경하지 않았다. 새 검증 파일 8개와 위 문서 2개만 변경 범위다. 세 Dependabot 변경은 사용자 작업 브랜치에 적용하지 않았다. 상세 JSON·로그·patch는 Git 제외 경로 `.documentation-backup/2026-09-11-dependabot-ci-wenrd4g3/`에 보관하며 새 개발 기록 Markdown을 추가하지 않았다.

### 정확한 소스별 로컬 증거

각 소스를 별도 임시 clone으로 checkout하고 **동일한 최종 검증 파일 8개만 추가**했다. 원본 파일 hash와 추가 파일 hash를 각 `*-evidence.json`에 분리했다. 원격 소스에는 로컬의 미게시 앱 변경이나 `constraints-lambda.txt`를 복사하지 않았다.

| 임시 소스 | 원본 commit SHA | 결과 |
|---|---|---|
| 원격 기본 `master` + CI 선도입 | `6648f058788a02242a1d0d0a5acdbcb4e1e48e03` | 192 passed, 29.44초 |
| [PR #3 checkout](https://github.com/innosonian/arc_calculator_api/pull/3) + CI | `59c0cc2cf3bded2ec8c29d938c30ad24b7214d85` | 192 passed, 30.64초 |
| [PR #2 setup-python](https://github.com/innosonian/arc_calculator_api/pull/2) + CI | `4b3f995679b5a872c85eb60179e5e100202e94bd` | 192 passed, 29.32초 |
| [PR #1 AWS credentials](https://github.com/innosonian/arc_calculator_api/pull/1) + CI | `dc477ad25727505608d9bb2bfadf65b76fd57529` | 192 passed, 29.44초 |
| 위 기본 + 위 세 PR의 정확한 diff + CI | 기본 SHA는 위 `6648f058…`; 새 commit을 만들지 않은 합성 파일 상태 | 192 passed, 29.08초 |
| 현재 로컬 `develop` + CI | `a561764909e32f6cd2fd7976a102d890f294958b` | 210 passed, 33.41초 |

모든 표의 소스에서 actionlint·공식 계약·관련 문법 검사도 exit 0이다. 합성 조합의 배포 workflow SHA-256은 `e4cecf80d2d00f371ad5aab8acbd1fffaf645d88328cfd1a8df70b03e6f9929c`이며 GitHub의 합성 merge commit을 뜻하지 않는다. CI workflow SHA-256은 `b1196eddec651e33294ad096535172f1df66ef3c04d3457eaca0db8e34a0a3d1`, 검사기 SHA-256은 `41650316b83615dc77d2a9752fc09d0e93af358c2105e38e197a3d9ea3ad34fe`다. 나머지 추가 파일은 `code-overlay.json`에 기록했다.

환경은 macOS ARM64, Python 3.12.1의 새 가상환경 2개다. pytest 9.1.1·PyYAML 6.0.3·actionlint 1.7.12를 사용했다. 원격 소스는 그 소스의 requirements, 로컬은 그 소스의 requirements와 constraints를 설치했고 두 환경의 `pip check`가 통과했다. 실제 설치 목록은 `local-dependencies.json`·`remote-dependencies.json`에 보관한다. actionlint는 Mac ARM64 공식 archive checksum과 실행 버전을 확인했다. Linux archive checksum은 공식 배포 자료와 대조했지만 Linux runner 실행은 아직 하지 않았다.

새 회귀는 92개(검사기 73개, 실행 보호 19개)다. 나머지는 해당 소스의 기존 preflight 회귀 100개/118개다. 정상 가상 설정의 region 출력, 누락·불일치 시 외부 명령 차단, OIDC/키 분기와 세션/리전 연결, 오류 비밀정보 정제, conftest 수집 단계 SDK 호출 차단을 확인했다. Python 연결 차단과 기존 자식 shell의 fake CLI를 사용하며 OS 전체 통신 격리라고 주장하지 않는다. 실제 사용자 환경파일·키·DB는 사용하지 않았다.

CI smoke 확인 함수는 로컬에서 정상 checkout/Python 경로를 통과시키고 잘못된 기대 SHA를 `CHECKOUT_SHA_MISMATCH`로 거절했다. 이는 Node 24 액션 실행 증거가 아니다. 공식 metadata는 고정 SHA의 실제 다운로드와 checksum 대조도 통과했다. 최초 샌드박스 네트워크에서는 다운로드 실패가 발생했고, 허용된 외부 네트워크 재실행에서 통과했다. 다운로드 실패를 계약 성공으로 처리하지 않았다.

### 두 차례 상호 검토와 반론 처리

이번 구현에는 총괄 포함 실제 에이전트 4개를 사용했다. 하위 에이전트 3개가 각각 두 역할을 순차 수행했다: 선임1/전문가2, 선임2/전문가1, 선임3/전문가3. 여섯 개의 독립 에이전트나 사람의 운영 승인으로 표현하지 않는다.

| 역할 | 설계 전 상호 검토 | 구현 후 상호 검토·총괄 처리 |
|---|---|---|
| 선임1: CI 설계 | 선임3에 조건부 job/step의 검증 누락 가능성 지적. 단일 무조건 실행 구조 채택 | 선임3의 합성 단위시험만으로 실제 PR SHA를 입증할 수 없다는 반론 수용. 실제 여섯 소스 사본으로 별도 검증 |
| 선임2: 회귀 | 기존 autouse fixture만으로 수집 중 SDK 호출을 막는다는 주장 반박. pytest import 전 보호 채택 | 선임1의 필수 lint/회귀 step 삭제가 false green을 만드는 반례 수용. ID·순서·실제 명령 검사와 실패 회귀 추가 |
| 선임3: 보존·유지보수 | 선임1의 실행 프레임워크 확대와 선임2의 완전 재현성 주장 검토. 정적 `uses`와 소스별 의존성 채택 | 설치기의 임시 시험만으로 회귀가 남지 않는다는 반론 수용. checksum 선검사·archive member·기존 파일 보존 5개 시험 추가. echo만 남긴 preflight도 거절 |
| 전문가1: AWS | 가짜 키를 넣은 정상 AWS 액션 경로도 STS를 호출하므로 오프라인이라는 주장 기각. 기본 범위를 정적 계약·fake CLI로 한정 | `with`만 확인하면 환경변수 `ROLE_CHAINING`이 인증 경로를 바꾸는 반례 수용. workflow/job/AWS step 환경 보존과 세 scope 회귀 추가 |
| 전문가2: GitHub | 전문가3의 owner+40자리 SHA만으로 검토한 릴리스를 보증할 수 없다는 반론 수용. 공식 tag 대조와 metadata hash 등록 채택 | Secret 대괄호 접근, closed 이벤트만 남긴 CI가 통과하는 반례 재현·수정 확인. 도입/기존 PR/조합 실행을 별도 증거로 구분 |
| 전문가3: 권한·공급망 | 전문가1의 정적 검사를 실제 인증 증거로 해석하지 않도록 제한. 실행 전 actionlint checksum 확인 채택 | 전문가1과 환경 입력 변화, 선임2와 Python 보호의 OS 격리 한계를 교차 검토. 정확한 권한·필수 명령·base 전달 반례 보강 |

초기 시험에서 발견한 구조 오류·검사 누락은 수정 후 위 최종 소스로 재검증했다. 최종 검토 범위에 알려진 중대한 코드 결함은 남지 않았으나, 미실행인 hosted runner·AWS 인증을 합의로 통과 처리하지 않았다.

### 액션별 판단과 남은 조건

기존/제안 버전은 PR 주석만 읽지 않고 공식 tag가 가리키는 commit과 대조했다. 세 후보는 Node 24이며 GitHub hosted runner에서 실제 기동 여부를 다음 PR 실행으로 확인해야 한다. 전체 SHA와 metadata hash는 `scripts/actions_contracts.json`에 둔다.

| 대상 | 검증한 기존 → 제안 릴리스 | 현재 판단·남은 조건 |
|---|---|---|
| #3 checkout | [4.4.0](https://github.com/actions/checkout/releases/tag/v4.4.0) → [7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1) | **조건부 가능**. 현재 `persist-credentials: false`, 후속 인증 Git·서브모듈·LFS 미사용의 정적 호환 근거와 로컬 회귀는 확보. 최신 PR의 실제 checkout SHA·Node 24 smoke 및 CI 성공 필요 |
| #2 setup-python | [5.6.0](https://github.com/actions/setup-python/releases/tag/v5.6.0) → [7.0.0](https://github.com/actions/setup-python/releases/tag/v7.0.0) | **조건부 가능**. Python 3.12 명시·캐시 미사용. 최신 PR의 실제 Python 선택·출력/PATH 연결과 CI 성공 필요 |
| #1 AWS credentials | [4.3.1](https://github.com/aws-actions/configure-aws-credentials/releases/tag/v4.3.1) → [6.2.4](https://github.com/aws-actions/configure-aws-credentials/releases/tag/v6.2.4) | **조건부 가능**. 공식 입력·분기·리전·세션·preflight 선행 구조 및 fake CLI 회귀 확보. 최신 PR CI 성공과 실제 인증 미검증 범위의 사용자 판단 필요. 실제 AWS 인증을 모든 의존성 병합의 필수 조건으로 새로 정하지 않음 |
| 세 변경의 조합 | 위 세 후보 | **조건부 가능**. 로컬 조합 통과, 코드상 병합 순서 의존성 없음. 최신 base에 세 변경만 적용한 원격 조합 CI와 개별 증거 확인 필요 |

권장 순서는 CI 선도입 → 개별/조합 검사 → #3/#2 → #1이다. 마지막 순서는 인증 변경을 별도로 살펴보기 위한 검토 편의이며 기술적 선후 의존성은 아니다. GitHub의 `mergeable: true`/`clean`은 호환성·검사 통과·사용자 병합 승인을 뜻하지 않는다. 종료 시점 원격 상태와 검사·보호 규칙 재조회는 상세 `end-remote.json`에 기록한다. 빈 status 목록의 집계 `pending`은 실제 실행 중인 검사로 세지 않는다.

실제 checkout/setup-python 액션 실행, GitHub PR run URL·합성 merge/workflow SHA, AWS 액션 정상 실행·STS·OIDC/키 인증·실제 IAM 신뢰 정책과 자원 연결은 **미검증**이다. 앱 전체·실물 장치·DB 인수시험도 이번 범위에서 재실행하지 않았다. 배포 준비에는 역할 ARN·OIDC provider·aud/sub·허용 branch/environment·자원 권한 및 누락 설정을 별도 확인해야 한다. 현재 GitHub 설정 부재와 IAM 예시/immutable subject 차이는 DEPLOY_GUIDE F3에 구분했다.

staging/unstaging·commit·push·PR 생성/변경/승인/병합·CI dispatch/재실행·AWS 연결/조회/배포·ARC/HSTM 전송은 실행하지 않았다. 로컬 index의 시작/종료 SHA-256 및 기존 파일 보존 결과는 `start-local.json`·`end-local.json`에 기록한다.

종료 재조회에서도 원격 기본 `master`와 위 세 PR의 base/head·diff는 시작과 같았다. 세 PR은 모두 OPEN·non-draft·`mergeable: true`/`clean`이며, 각 최신 head의 check run·commit status·workflow run은 각각 0개다. master는 protected=false이고 적용되는 branch rule 목록은 비어 있었다. 이 조회는 성공했으며 권한 부족을 설정 부재로 처리하지 않았다. 원격 CI 성공이나 사용자 병합 승인은 아직 없다.
