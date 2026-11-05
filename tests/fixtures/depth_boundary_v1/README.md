# DEPTH_V1 개정 3 공통 벡터

사양: 사용자 제공 **DEPTH_V1r3 · 2026-10-28**, 진폭 **30 raw**, Record 50ms·샘플 5ms.
같은 날 사용자가 정정한 action 창 **[직전 count_record, 이번 count_record)**를 적용했다.
확정 Record는 다음 버퍼의 선두이며 `action_end`는 제외 인덱스다. 종료 확정도 마지막 Record를
LAST 버퍼에 남긴다. 최초 사양의 경계 Record 포함 문구는 이 후속 정정으로 대체되었다.
합성 **13종 × byte 12 세 변형 = 39건**, 제공 실세션 **15건**, 총 **54건**이다.
초기 ZIP 11건에 사용자가 추가 제공한 CPR·AED 세션 4건을 더해 요청된 15건을 모두 재생했다.

각 디렉터리에는 원본 `raw.bin`, 조건·VP 이벤트와 선택적 AED 파일 참조만 보존한 `input.json`, 사양 §8의 모든 필드를 담은
`expected.json`, 생성 출처와 파일 SHA-256을 담은 `manifest.json`이 있다. 실세션의 이름·조직 등
메타데이터는 복사하지 않았다. CPR·AED 4건에는 `aed.bin`도 있으며 manifest 해시에 포함한다.
모든 실세션 바이너리는 제공 ZIP·파일의 바이트를 그대로 보존했다.

## 생성과 재생

생성 기준 HEAD: `9a36a9818399bbd17777b036dc938007f60705bf` (`develop`). 아직 커밋하지 않은
DEPTH_V1 구현으로 생성했으므로 이 HEAD만으로 생성기를 재현할 수는 없다. 생성기 소스의 SHA-256과
벡터 파일 해시는 각 manifest에 별도로 기록되어 있다. 로컬 커밋·push·배포는 별도 승인 대상이다.
추가 4건 생성 시 기존 50건의 raw·입력·expected·manifest **200파일은 바이트 단위로 보존**했다.
따라서 기존 manifest의 생성기 해시는 당시 버전을, 새 4건의 해시는 AED 입력을 지원하는 버전을 가리킨다.

```bash
AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 STAGE=test SENTRY_DSN= PYTHONPATH=. .venv/bin/python scripts/generate_depth_boundary_golden.py --real-zip /Users/mun/Downloads/hstm.zip --real-dir /Users/mun/Innosonian/depth_boundary_data/real

AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 STAGE=test SENTRY_DSN= PYTHONPATH=. .venv/bin/python scripts/generate_depth_boundary_golden.py --check

AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 STAGE=test SENTRY_DSN= .venv/bin/python -m pytest tests/test_depth_boundary_golden.py -q
```

최초 가져오기 뒤에는 외부 ZIP·디렉터리 없이 저장된 입력으로 `--check`할 수 있다. 생성기는 합성 39건을 코드 안의
**수기 전체 필드 오라클**과 대조한 뒤 실세션 수를 대조하고, 전부 통과한 뒤에만 파일을 쓴다.
기존 raw·입력·expected 중 하나라도 달라지면 쓰기 전에 실패한다. 구현을 통과시키기 위한 벡터
재생성을 하지 않는다. 일반 실행도 검증된 기존 파일·manifest를 다시 쓰지 않고 새 케이스만 추가한다.
`--check`는 어떤 파일도 쓰지 않는다.

앱은 `raw.bin`을 그대로 재생하고 `expected.json`의 모든 필드를 비교해야 한다. 첫 압박 속도는
`null`, 정점 동률은 앞 Record·앞 샘플, action 창은 `[action_start, action_end)`다. infant HP window는 원본
코드를 보존하고 판정할 때만 `& 0x0F`를 적용한다.

경계 정정 이전에 생성한 벡터는 `/tmp/depth_v1_before_boundary_correction`에 보존한 뒤,
정정된 수기 합성 오라클과 기존 CONFIRM 좌표·raw 위치 코드를 사용한 독립 HP 창 계산을 먼저
검증하고 expected를 한 번 이행했다. 변경 가능 필드는 `action_start`·`action_end`·`hp_window`·`hp`로
제한했다. **raw·횟수·확정/정점 좌표·깊이·recoil·속도는 변경하지 않았다.** `two_rescuer_vp`는 바뀐
action 창에서도 RP→VP→RP 다수결을 유지하도록 VP 시작 이벤트를 Record 2 직전으로 옮겼다.
각 manifest의 `boundary_amendment`가 이 사용자 정정을 명시한다. 일반 생성기의 덮어쓰기 금지는 유지한다.

**정정 2: aed_part_first 추가, 이유: 앱은 AED 이벤트 없이 두 필드를 재현할 수 없음.**
사양 §4에 따라 `_split_aed_tail`이 `action_start`를 정했거나 AED 새 파트 첫 압박으로
`prev_peak_sample`이 초기화된 행에만 `"aed_part_first": true`를 추가한다. 두 필드는
`action_start`와 `prev_peak_sample`에 따른 `rate`다. 코드 재생으로 확인한 대상은
`real/19148` id 10, `real/19986` id 8, `real/19994` id 27, `real/20016` id 21뿐이다.
19994는 앞의 AED 제외 압박 26회 때문에 첫 채점 파트 번호가 1이어도 대상에 포함된다.
이 네 expected에는 플래그만 추가하고 manifest의 `sha256.expected.json`만 갱신했다.
기존 생성 출처와 나머지 50건의 모든 파일은 바이트 단위로 보존한다.

이 정정은 생성기에 `--amend-aed-flag`를 명시할 때만 허용한다. 기존 expected가 새 결과에서
플래그만 뺀 바이트와 정확히 같고 기존 manifest SHA도 일치해야 하며, 다른 필드·입력의 변경은
옵션을 지정해도 거부한다. `--check`는 이 옵션과 함께 사용해도 변경을 거부하고 쓰지 않는다.
이미 정정한 뒤 다시 실행하면 파일을 쓰지 않는다.

## 합성 케이스

모든 케이스를 `zero`(Ccnt 0), `every_record`(매 Record 증가), `hold_jitter`(유지 중 반복 증가를
모사한 카운터)로 복제했다. 변형 간에는 byte 12만 다르고 expected는 동일하다. CRate·Cycle·시퀀스
번호도 의도적으로 불규칙하게 넣었으며, 이 필드들을 다시 바꾼 재생 결과도 검증한다.

| 케이스 | 압박 수 | 고정한 규칙 |
|---|---:|---|
| hold_jitter | 1 | 50mm 부근 ±1 raw 유지 중 추가 압박 없음; 동률 정점은 앞 Record |
| hold_flat | 1 | 하강 없는 유지의 정점은 finish에서 확정 |
| partial_recoil_x3 | 3 | 0→50→15→50→15→50→0mm; recoil 30·30·0 raw |
| shallow_pump | 0 | 5↔15mm 다섯 번, 진폭 20 raw는 미달 |
| shallow_30_15 | 5 | 15↔30mm 다섯 번, 정확히 30 raw 진폭은 포함 |
| start_pressed | 1 | 첫 Record부터 40mm인 상태의 30 raw 하강 포함 |
| end_rising_30 | 1 | 마지막 Record에서 골 대비 30 raw 상승이면 finish 확정 |
| end_rising_29 | 0 | 골 대비 29 raw 상승은 finish에서도 미집계 |
| spike | 1 | 한 샘플 120 raw도 pmax 기준으로 집계; peak_offset 5 |
| amp_boundary_fall_29 | 0 | 시작 정점에서 29 raw 하강만으로는 미확정 |
| amp_boundary_fall_30 | 1 | 시작 정점에서 30 raw 하강하면 확정 |
| two_rescuer_vp | 3 | RP→VP→RP 전체 분절; RP 채점 이벤트 2, 첫 recoil 50 raw |
| infant_mask | 1 | HP window는 0x44 유지, 판정은 4로 마스킹해 left |

## 실세션과 대조 근거

초기 11건 출처는 사용자가 제공한 `hstm.zip`이다. 아래 Chart 수는 같은 ZIP의 `.json`에서 압박 막대를 직접
세어 대조했으며, Chart 원본 해시는 manifest에 남겼다. 기존 진폭 25의 분절 결과는 11건 모두
이 Chart 수와 일치했다. 진폭 30에서는 얕은 세션 한 건만 달라진다.

| 세션 ID | Record 수 | 제공 Chart 수(25 raw) | DEPTH_V1r3 수(30 raw) |
|---|---:|---:|---:|
| 1793164406 | 1321 | 4 | 4 |
| 1793164539 | 1003 | 8 | 8 |
| 1793164827 | 512 | 52 | 48 |
| 1793164870 | 440 | 60 | 60 |
| 1793165252 | 394 | 61 | 61 |
| 1793165300 | 516 | 60 | 60 |
| 1793165346 | 474 | 61 | 61 |
| 1793165416 | 518 | 60 | 60 |
| 1793165465 | 479 | 59 | 59 |
| 1793165506 | 443 | 62 | 62 |
| 1793165797 | 779 | 69 | 69 |

유지 세션은 4·8회이며, 불완전 이완의 지정 기대값 61·62·59회도 보존한다.
`1793164827`의 **52→48**은 사용자 요청서에 지정된 결과다. 진폭을 30 raw로 올리면서 정점
30mm 이하의 얕은 재압박 4건을 세지 않는 것이 의도된 동작이다.

추가 4건은 `/Users/mun/Innosonian/depth_boundary_data/real/training_log_id_<id>.bin`과
같은 디렉터리의 `training_log_id_<id>_aedt.bin`을 짝지었다. 조건은 기본값
`adult / cpr / AHA2020`, VP 없음이다. 기대 횟수 23·29·63·44는 사용자 제공 README와 후속 지시의
고정값으로 먼저 대조했다. 이 4건에는 원본 Chart JSON이 없으므로 기존 Chart 대조를 했다고 주장하지 않는다.

| 세션 ID | CPR Record 수 | AED 바이트 | 전체 action / Chart | 사이클 압박 수 | 제외(AED / 기타) | CPR 파트별 압박 수 |
|---|---:|---:|---:|---:|---:|---|
| 19148 | 1462 | 188 | 23 / 23 | 23 | 0 / 0 | 9 + 14 |
| 19986 | 1291 | 168 | 29 / 29 | 29 | 0 / 0 | 7 + 22 |
| 19994 | 1667 | 198 | 63 / 63 | 37 | 26 / 0 | 37 |
| 20016 | 1439 | 178 | 44 / 44 | 44 | 0 / 0 | 20 + 24 |

19148·19986·20016은 AED 창 전후로 CPR 파트가 두 개이며 파트별 압박 수 합이 전체와 같다.
19994는 사용자 후속 확인대로 기존 AED 제외 정책을 유지한다. `action_under_aed` 압박 26회를
사이클 채점에 넣지 않아 **전체 63 = 사이클 37 + 제외 26**이다. 제외 집합은 AED 겹침과 계산 사이클에
속하지 않는 압박의 합집합이며 정점 Record로 중복을 제거했다. AED 외 추가 제외는 0이다.
`CompressionNo`와 압박 횟수 게이트 입력은 전체 63을 유지한다.

입력 말미의 0바이트도 제거하지 않았다. CPR 파일의 마지막 28바이트와 AED 파일의 마지막 8바이트는
모두 0이며 원본 그대로 저장했다. 표의 Record 수는 현행 CPR 파서가 읽은 28바이트 묶음 수다.
AED는 기존 10바이트 파서로 처리하며 timestamp가 0인 묶음과 남은 8바이트를 기존대로 제외한다.
별도 헤더 수정·패딩 제거·타임스탬프 정규화는 하지 않았다.

실세션의 정점·recoil·속도·HP 전체 필드는 합성 오라클 검증을 통과한 구현의 결과를 고정한 것이다.
초기 11건의 원본 Chart 수 자동 대조, 전체 15건의 사용자 횟수 기대값 및 계산 결과·Chart 재생 대조는 완료했다. 요청서가 요구한
**사람의 실세션 횟수·Chart 막대 수 대조 확인 기록은 아직 없다.** manifest의
`human_chart_count_review: pending`은 이 미확인 항목을 뜻한다. 전체 필드에 대한 별도 사람 승인
절차를 추가하지 않는다. 이 사람 확인 기록과 로컬 15세션 자동 검증 완료는 구분한다.
실제 보드 종류는 원본으로 구분되지 않아 추정해 채우지 않았다.

## 사용자 확인으로 확정한 범위

- 전체 action·Chart 수는 전체 CONFIRM끼리 비교한다. 사이클 채점·`CompressionActionNumber`는
  기존 RP·채점 가능 사이클 범위를 유지하고, 같은 대상 범위 안에서만 동일성을 요구한다.
- AED 제외도 기존대로 유지하며, 전체 횟수는 사이클 압박 수와 실제 제외 집합의 합으로 검증한다.
- recoil은 actor와 무관하게 **전체 스트림의 바로 다음 압박 정점** 전까지다. VP 정점을 건너뛰어
  다음 RP 정점까지 확장하지 않는다. `two_rescuer_vp`의 첫 recoil 50 raw(10 raw 아님)가 이를 고정한다.
- 60회 게이트는 기존 `< 60`, Custom 횟수 조건은 기존 목표의 95% 기준을 유지한다.
- R01은 대상 밖이며 기본 DISABLED를 유지한다. `scoring/ventilation.py::app_counted_compressions`의
  구 Ccnt 규칙 교체는 R01 활성화 전 후속 작업이다.
