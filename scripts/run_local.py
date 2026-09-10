"""로컬에서 계산 로직만 실행하는 CLI

사용 예:
    .venv/bin/python scripts/run_local.py \\
        --cpr cprTraining_1756103663657.bin \\
        --target adult --guideline ARC2025 --cycle-type 302

Lambda handler, multipart 파싱, 제출 invoke 전부 우회하고 main.run_calculator만 호출.
"""
# 원본: hstm_v2 scripts/run_local.py (arc 이식 — 동작 동일. guideline 선택지는
# 이 저장소가 지원하는 ARC2020/ARC2025 2종으로 축소, 기본값 ARC2025는 원본 그대로.
# --stage 기본 "prod"도 원본 그대로 — H-6 현행 유지 승인).

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import run_calculator
from services.http.schemas import ConditionType


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpr", required=True, help="CPR 바이너리 파일 경로")
    parser.add_argument("--mode", default="training", choices=["training", "assessment", "dry_run"])
    parser.add_argument("--target", default="adult", choices=["adult", "child", "infant"])
    parser.add_argument(
        "--training_type",
        default="cpr",
        choices=["cpr", "compression_only", "ventilation_only"],
    )
    parser.add_argument(
        "--guideline",
        default="ARC2025",
        choices=["ARC2020", "ARC2025"],
    )
    parser.add_argument("--cpr_cycle_type", default="302", choices=["302", "152"])
    parser.add_argument("--is_2rescuers", action="store_true", help="2인구조 모드")
    parser.add_argument(
        "--vp_event",
        help="vp_event_list JSON 파일 경로 (2인구조). 미지정 시 --cpr 옆 vp_event_list_<ts>.json 자동 탐색",
    )
    parser.add_argument("--stage", default="prod")
    parser.add_argument("--out", help="결과 저장 경로 (미지정 시 stdout)")
    args = parser.parse_args()

    with open(args.cpr, "rb") as f:
        cpr_bytes = f.read()

    vp_event_list = []
    vp_event_path = args.vp_event
    if vp_event_path is None and args.is_2rescuers:
        # --cpr 파일명의 타임스탬프로 같은 디렉토리의 vp_event_list_<ts>.json 자동 탐색
        ts_match = re.search(r"_(\d+)\.bin$", os.path.basename(args.cpr))
        if ts_match:
            candidate = os.path.join(os.path.dirname(args.cpr), f"vp_event_list_{ts_match.group(1)}.json")
            if os.path.exists(candidate):
                vp_event_path = candidate
    if vp_event_path:
        with open(vp_event_path, "r", encoding="utf-8") as f:
            vp_event_list = json.load(f)
        print(f"vp_event_list: {vp_event_path} ({len(vp_event_list)} events)", file=sys.stderr)

    condition = ConditionType(
        mode=args.mode,
        target=args.target,
        training_type=args.training_type,
        guideline=args.guideline,
        cpr_cycle_type=args.cpr_cycle_type,
        is_2rescuers=args.is_2rescuers,
    )

    result = run_calculator(
        cpr_byte_data=cpr_bytes,
        aed_byte_data=b"",
        condition=condition,
        vp_event_list=vp_event_list,
        stage=args.stage,
    )

    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"saved: {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
