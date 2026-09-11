# ARC Calculator API

앱이 누적한 CPR/AED 바이너리를 기존 내부 계산기로 계산하고 점수·통계·코칭·차트를 반환하는 American Red Cross 전용 백엔드다. 로그인·프로그램·시도·공유 진도를 제공하며, 계산 결과와 프로그램 완료 및 ARC 제출 상태를 구별한다.

**현재:** 기본 로컬 서버·DynamoDB Local·별도 Worker·파일/차트 연결을 검증했다. 5개 프로그램 × 3개 연령의 계산 결과를 제공한다. Only는 실제 목표 횟수+기존 tester Pass로 완료하고, CPR의 완전한 cycle 완료 규칙은 미정이라 `pending_policy`로 남긴다. 실제 iPad/Android·마네킨 현장 인수와 AWS·ARC 연동은 별도다. `submit_arc={"status":"disabled","ok":false,"error":"arc_contract_pending"}`이며 실제 ARC 제출은 수행하지 않는다.

## 핵심 문서 9개

새 작업은 아래 문서를 기준으로 진행한다. 최신 사용자 답변이 우선하며 과거 리뷰나 설계 제안을 사용자 결정으로 바꾸지 않는다.

| 문서 | 역할 |
|---|---|
| [README.md](README.md) | 현재 상태·전체 문서 안내 |
| [AGENTS.md](AGENTS.md) | 이후 작업의 보존·검증·질문·Git 지침 |
| [결정과 미정 사항](docs/DECISIONS.md) | 확정 정책·과거 정정·남은 질문 |
| [구조와 보안](docs/ARCHITECTURE.md) | 내부 계산·상태/작업·저장·보호 경계 |
| [상세 API 계약](docs/ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md) | 전체 경로·오류·입력 파서·계산/문서·자료형 계약 |
| [앱팀용 API](docs/APP_API.md) | 바로 연결할 Method·Endpoint·필수 필드. 서버 주소 제외 |
| [로컬 실행](docs/LOCAL_RUN.md) | Mac 실행·iPad 연결·DB/파일 보관·문제 확인 |
| [AWS·Dev 후속 안내](docs/DEPLOY_GUIDE.md) | 개인 접근·실자원 확인·미완성 연결·배포/복구·환경파일 |
| [검증·리팩터링](docs/VALIDATION.md) | 실제 실행 증거·발견한 문제·변경 전후 코드·성능 측정·남은 제약 |

2026-09-11 코드 품질 점검에서는 배포 ZIP 압축·스트리밍 해시, AWS 설정 한도 검사, 기존 로그 정책 유지와 경로가 있는 IAM 역할 지정, 공용 훈련 정의 분리를 반영했다. [환경변수 예시](.env.example)·[자원 연결표 예시](deploy/bindings.example.json)·[배포 간접 의존성 제약](constraints-lambda.txt)을 제공한다. **로컬 검증 완료와 AWS 전체 Journey 배포 준비 완료는 다르다.** 미완성 실행 연결은 배포 안내의 완료 조건을 따른다.

기존 핵심 문서 9개를 활용한다. 요청한 리팩터링 보고서는 `docs/VALIDATION.md`의 8절, AWS 배포 문서는 `docs/DEPLOY_GUIDE.md`에 통합하며 별도 중복 문서를 만들지 않는다.

## 로컬 시작

현재 이 Mac에 준비된 실행 환경을 사용한다.

```sh
cd /Users/mac/arc_calculator_api
var/local-python/bin/python scripts/serve_local.py
```

터미널을 켜 둔 채 `http://127.0.0.1:8000/healthz`를 확인한다. Dummy 로그인은 `test@test.com` / 문자열 `2222`다. iPad에서는 Mac의 현재 내부 IP를 사용한다. 정확한 클라이언트 IP 허용과 평문 LAN 시험의 조건은 로컬 안내를 따른다. 명령 자체가 의존성을 설치하거나 AWS에 연결하지 않는다.

AWS에서는 실제 자원 정보뿐 아니라 API·Worker·Relay의 실행 연결 개발과 전체 경로 인수가 남아 있다. 기존 배포 스크립트만 실행해 전체 Journey가 완성된다고 가정하지 않는다.

## 원본·진행 기록의 로컬 보관

첫 commit 이전의 원본 문서·진행 JSON·patch는 아래 폴더에 기존 경로와 내용 그대로 보관한다.

```text
.documentation-backup/2026-09-10-document-consolidation/
  INDEX.txt          로컬 열람 안내
  ORIGINALS.json     파일별 원래 경로·크기·SHA-256·기존 Git 등록 정보
  SOURCE_MAP.json    통합 문서 또는 역사 자료로의 대응표
  originals/        원본 파일
```

Finder에서 저장소를 열고 `Command+Shift+.`로 숨김 폴더를 표시하거나 `Command+Shift+G`에 `/Users/mac/arc_calculator_api/.documentation-backup/2026-09-10-document-consolidation`을 입력하면 된다. 로컬 원본은 현재 지침이 아닌 역사 자료이며 Git과 배포물에서 제외한다. 이 PC의 사본이므로 다른 개발자의 checkout이나 별도 백업까지 자동으로 만들어지는 것은 아니다.

실행에 필요한 DB 배포 manifest, API 계약 테스트의 route manifest, 회귀 fixture와 실측 dataset JSON은 진행 기록과 구분하여 유지한다. 새 진행 기록은 로컬 보관 폴더에 모으고 핵심 문서를 계속 늘리지 않는다.

DB 저장 대상은 **앱의 훈련 진도·계산 결과·운용 로그**다. 기존 상태/결과 파일 참조 DB와 비공개 파일 저장 코드를 재사용한다. 로컬 API·Worker의 주요 이력과 정제된 상세 진단을 DB에 기록하도록 추가했다. 로그 저장 장애·대기열 포화는 정상 훈련을 차단하지 않으며, 누락 가능성을 별도로 집계한다. 보관기간 확정 전 자동 삭제는 하지 않는다. [조회 명령과 한계](docs/LOCAL_RUN.md), [검증 결과](docs/VALIDATION.md)를 확인한다. 개발 기록 JSON·patch는 계속 로컬에만 보관하며 실제 AWS 로그 연결은 후속 작업이다.

