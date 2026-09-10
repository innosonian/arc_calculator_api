"""V1 기준값(baseline) 생성 러너 — 원본 저장소 코드를 무수정 그대로 실행한다.

스펙 근거: analysis/50-PORTING-SPEC.md §7.1
- 원본 저장소(hstm_v2_calcuator_api)를 sys.path에 추가해 원본 main.run_calculator를 호출.
- 원본 scripts/viewer_common.py:43-45 방식 그대로 몽키패치(원본 파일 무수정):
    * main._save_raw_input        → no-op   (원본 main.py:28,64-79 — raw input S3 업로드 차단)
    * services.calculate_cpr.add_chart_data → passthrough
                                  (원본 services/calculate_cpr.py:5,63-70 — chart S3 업로드/서명URL 차단.
                                   따라서 결과에 chart_dataset_url 키가 없다 — 원본 data_handlers/chart_data.py:39-47)
    * services.calculate_cpr._log → no-op   (원본 services/calculate_cpr.py:91-97 — JSON 로그 억제)
  → S3/네트워크 부작용 0.
- guideline은 ARC2025로 고정(스펙 §7.1). 나머지 조건은 원본 tests/test_result.py에서 역추적:
    * cpr_1..5  : test_result.py:22-48  (adult / cpr / 302 / 1인, aed_i.bin 페어링, stage="test")
    * cco_1     : test_result.py:102-123 (adult / compression_only / 302 / 1인, aed=b"")
    * vo_1      : test_result.py:144-166 (infant / ventilation_only / 302 / 1인, aed=b"")
    * adult_vo_1: 스펙 §7.1(R3-7) — vo_1 조건에서 target만 adult로. 입력은 원본 tests/adult_vo_1.bin.
- 각 결과(run_calculator 반환 dict 전체)를 analysis/v1-baseline/{bin이름}.json 으로 저장.

실행:
    /Users/mac/arc_calculator_api/.venv/bin/python \
        /Users/mac/arc_calculator_api/analysis/v1-baseline/run_baseline.py
"""

import hashlib
import json
import os
import sys
from pathlib import Path

# 원본 저장소에 bytecode 캐시(__pycache__)를 쓰지 않는다 — 원본 읽기 전용 원칙.
sys.dont_write_bytecode = True

ORIGINAL_REPO = "/Users/mac/Library/Mobile Documents/com~apple~CloudDocs/Innosonian/hstm_v2_calcuator_api"
OUT_DIR = Path(__file__).resolve().parent

sys.path.insert(0, ORIGINAL_REPO)

import main as calc_main  # noqa: E402  (원본 main.py)
import services.calculate_cpr as cc  # noqa: E402  (원본 services/calculate_cpr.py)
from services.http.schemas import ConditionType  # noqa: E402  (원본 services/http/schemas.py:4-10)

# ── 몽키패치 (원본 scripts/viewer_common.py:43-45 방식) ──────────────────────
calc_main._save_raw_input = lambda *a, **k: None
cc.add_chart_data = lambda response, *a, **k: response
cc._log = lambda *a, **k: None


def _condition(target: str, training_type: str) -> ConditionType:
    # mode/cpr_cycle_type/is_2rescuers는 원본 tests/test_result.py 공통값,
    # guideline만 ARC2025 고정(스펙 §7.1). 원본 config/borders.py에서
    # ARC2025 블록은 ARC2020과 동일 값(스펙 §2와 부합)임을 확인함.
    return ConditionType(
        mode="training",
        target=target,
        training_type=training_type,
        guideline="ARC2025",
        cpr_cycle_type="302",
        is_2rescuers=False,
    )


# (출력 이름, cpr bin 경로, aed bin 경로 또는 None, condition)
RUNS = [
    *[
        (
            f"cpr_{i}",
            f"{ORIGINAL_REPO}/tests/dataset/cpr_{i}.bin",
            f"{ORIGINAL_REPO}/tests/dataset/aed_{i}.bin",
            _condition("adult", "cpr"),
        )
        for i in range(1, 6)
    ],
    (f"cco_1", f"{ORIGINAL_REPO}/tests/dataset/cco_1.bin", None, _condition("adult", "compression_only")),
    (f"vo_1", f"{ORIGINAL_REPO}/tests/dataset/vo_1.bin", None, _condition("infant", "ventilation_only")),
    (f"adult_vo_1", f"{ORIGINAL_REPO}/tests/adult_vo_1.bin", None, _condition("adult", "ventilation_only")),
]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = []
    for name, cpr_path, aed_path, condition in RUNS:
        with open(cpr_path, "rb") as f:
            cpr_bytes = f.read()
        if aed_path:
            with open(aed_path, "rb") as f:
                aed_bytes = f.read()
        else:
            aed_bytes = b""  # 원본 tests/test_result.py:121,163 — cco/vo는 AED 없음

        try:
            # 호출 형태는 원본 tests/test_result.py:43-48과 동일(stage="test",
            # vp_event_list/usage/organization 기본값).
            result = calc_main.run_calculator(
                cpr_bytes,
                aed_bytes,
                condition,
                stage="test",
            )
        except Exception as e:  # 실패 bin은 스킵하지 않고 보고(작업 지침 5)
            import traceback

            failures.append((name, repr(e)))
            print(f"[FAIL] {name}: {e!r}")
            traceback.print_exc()
            continue

        out_path = OUT_DIR / f"{name}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")

        total = result["cpr_score"]["total_score"]
        print(
            f"[OK] {name}: overall={total.get('overall')} judg={total.get('judg_result')!r} "
            f"aed_overall={(result.get('aed_score') or {}).get('overall')} "
            f"cpr_sha256={hashlib.sha256(cpr_bytes).hexdigest()[:12]} -> {out_path.name}"
        )

    if failures:
        print(f"\n{len(failures)} run(s) failed: {[n for n, _ in failures]}")
        return 1
    print(f"\nAll {len(RUNS)} baseline results written to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
