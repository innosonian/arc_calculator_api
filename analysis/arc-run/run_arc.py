"""arc 골든 생성 러너 — arc 저장소 코드로 13개 입력(8개 결과)을 실행한다.

스펙 근거: analysis/50-PORTING-SPEC.md §7.2
- analysis/v1-baseline/run_baseline.py 와 동일한 8개 케이스/조건(guideline=ARC2025 고정).
- stage="test" 실행: util/uploader.upload_raw_input·upload_json_file 이 stage=="test"에서
  즉시 return 하므로(원본 동작 보존) S3/네트워크 부작용 0.
  tests/test_result.py 의 골든 소비 경로(run_calculator(..., stage="test"), 몽키패치 없음)와
  완전히 동일한 호출 형태로 골든을 생성한다 — 이 경우 결과에 chart_dataset_url=None 이 포함된다.
  (v1-baseline은 add_chart_data 자체를 몽키패치해 키가 없음 — CONDITIONS.md 참조.)
- 결과(run_calculator 반환 dict 전체)를 tests/dataset/result_*.json 으로 저장
  (파일명 규칙은 tests/test_result.py _CASES 와 동일).

실행:
    /Users/mac/arc_calculator_api/.venv/bin/python \
        /Users/mac/arc_calculator_api/analysis/arc-run/run_arc.py
"""

import hashlib
import json
import sys
from pathlib import Path

ARC_REPO = Path("/Users/mac/arc_calculator_api")
DATASET_DIR = ARC_REPO / "tests" / "dataset"

sys.path.insert(0, str(ARC_REPO))

from main import run_calculator  # noqa: E402
from services.http.schemas import ConditionType  # noqa: E402


def _condition(target: str, training_type: str) -> ConditionType:
    return ConditionType(
        mode="training",
        target=target,
        training_type=training_type,
        guideline="ARC2025",
        cpr_cycle_type="302",
        is_2rescuers=False,
    )


# (골든 이름, cpr bin, aed bin 또는 None, condition) — tests/test_result.py _CASES 와 동일
RUNS = [
    *[
        (f"result_{i}", f"cpr_{i}.bin", f"aed_{i}.bin", _condition("adult", "cpr"))
        for i in range(1, 6)
    ],
    ("result_cco_1", "cco_1.bin", None, _condition("adult", "compression_only")),
    ("result_vo_1", "vo_1.bin", None, _condition("infant", "ventilation_only")),
    ("result_adult_vo_1", "adult_vo_1.bin", None, _condition("adult", "ventilation_only")),
]


def main() -> int:
    failures = []
    for name, cpr_bin, aed_bin, condition in RUNS:
        cpr_bytes = (DATASET_DIR / cpr_bin).read_bytes()
        aed_bytes = (DATASET_DIR / aed_bin).read_bytes() if aed_bin else b""

        try:
            result = run_calculator(cpr_bytes, aed_bytes, condition, stage="test")
        except Exception as e:
            import traceback

            failures.append((name, repr(e)))
            print(f"[FAIL] {name}: {e!r}")
            traceback.print_exc()
            continue

        out_path = DATASET_DIR / f"{name}.json"
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
    print(f"\nAll {len(RUNS)} arc golden results written to {DATASET_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
