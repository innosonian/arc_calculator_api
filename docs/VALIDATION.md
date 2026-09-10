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
