"""결과 뷰어(콘솔) 공용 로직 — 계산 실행과 화면 데이터 매핑

개발 전용 모듈(scripts/)이라 Lambda 배포에 포함되지 않는다.
"""
# 원본: hstm_v2 scripts/viewer_common.py (arc 이식 — 동작 동일, 몽키패치 구조 유지).

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KST = timezone(timedelta(hours=9))

# ── 타깃별 차트 임계 (iOS ManikinType.depthMinMax / volumeMinMax)
DEPTH_BAND_MM = {"infant": (30, 40, 70), "default": (50, 60, 80)}  # (양호min, 양호max, 축최대)
VOLUME_BAND_ML = {"infant": (20, 40, 50), "default": (400, 600, 1000)}


def band(condition, table):
    return table["infant" if condition.get("target") == "infant" else "default"]


def run_training(cpr_path: str, condition: dict, vp_event_list: list) -> tuple[dict, dict]:
    """계산기 실행(업로드·로그 무력화) → (result, chart_data)"""
    import main as calc_main
    import services.calculate_cpr as cc
    import data_handlers.chart_data as cd
    from services.http.schemas import ConditionType

    captured = {}

    def capture_chart(response, prepared_data, calculation_result, stage="prod", **kwargs):
        captured["chart"] = cd.make_chart_data(
            prepared_data["whole_cpr_action_list"],
            prepared_data["prepared_aed_data"],
            calculation_result["part_with_scores"],
        )
        return response

    calc_main._save_raw_input = lambda *a, **k: None
    cc.add_chart_data = capture_chart
    cc._log = lambda *a, **k: None

    with open(cpr_path, "rb") as f:
        cpr_bytes = f.read()
    result = calc_main.run_calculator(
        cpr_byte_data=cpr_bytes,
        aed_byte_data=b"",
        condition=ConditionType(**condition),
        vp_event_list=vp_event_list or [],
        stage="prod",
    )
    return result, captured.get("chart") or {}


def criteria_rows(condition) -> list[tuple[str, str]]:
    """요약 바·사이클 표의 (라벨, total_score/사이클 점수 키) — iOS 화면 순서"""
    comp = [
        ("Compression Depth", "score_comp_depth"),
        ("Compression Release", "score_recoil"),
        ("Compression Rate", "score_comp_rate"),
        ("Hand Position", "score_hand_position"),
        ("Compression Fraction", "score_ccf"),
    ]
    vent = [("Ventilation Volume", "score_vent_vol")]
    training_type = condition.get("training_type")
    if training_type == "compression_only":
        return comp
    if training_type == "ventilation_only":
        rows = vent + [("Ventilation Rate", "score_vent_rate")]
    else:
        rows = comp + [("Compression Count", "score_comp_count")] + vent + [("Ventilation Count", "score_vent_count")]
    if condition.get("target") == "infant":
        rows.append(("Ventilation Pressure", "score_vent_speed"))
    return rows


def metric_rows(metrics, condition) -> list[tuple[str, list[tuple[str, str]]]]:
    """CPR Metric Criteria Chart 행 — (지표명, [(세그먼트 라벨, 값문자열)]) (iOS 매핑·라벨)"""
    m = metrics

    def seg(key, *pairs):
        return [(label, f"{(m.get(key) or {}).get(f'%_{field}', 0)}%") for label, field in pairs]

    comp = [
        ("Compression Depth", seg("CompressionDepth", ("Too Shallow", "TooShallow"), ("Good", "Good"), ("Too Deep", "TooDeep"))),
        ("Compression Release", seg("Recoil", ("Incomplete", "Incomplete"), ("Good", "Good"))),
        ("Compression Rate", seg("CompressionRate", ("Too Slow", "TooSlow"), ("Good", "Good"), ("Too Fast", "TooFast"))),
        ("Hand Position", seg("HandPosition", ("Incorrect (Abdomen)", "IncorrectStomach"), ("Good", "Good"), ("Incorrect (L/R)", "IncorrectLR"))),
        ("Compression Fraction", [("Score", str(m.get("ScoreOfCCF", 0)))]),
    ]
    vent_vol = ("Ventilation Volume", seg("VentilationVolume", ("More Air", "TooLittle"), ("Good", "Good"), ("Less Air", "TooMuch")))
    training_type = condition.get("training_type")
    if training_type == "compression_only":
        rows = comp
    elif training_type == "ventilation_only":
        rows = [vent_vol, ("Ventilation Rate", seg("VentilationRate", ("Infrequently", "InFrequently"), ("Good", "Good"), ("Too Frequently", "TooFrequently")))]
    else:
        rows = comp + [
            ("Compression Count", seg("CompressionCount", ("Too Few", "TooFew"), ("Good", "Good"), ("Too Many", "TooMany"))),
            vent_vol,
            ("Ventilation Count", seg("VentilationCount", ("Too Few", "TooFew"), ("Good", "Good"), ("Too Many", "TooMany"))),
        ]
    if condition.get("target") == "infant" and m.get("VentilationSpeed"):
        rows.append(("Ventilation Pressure", seg("VentilationSpeed", ("Too Slow", "TooSlow"), ("Good", "Good"), ("Too Fast", "TooFast"))))
    return rows


def fmt_score(value) -> str:
    return "-" if value is None else f"{value:.0f}" if isinstance(value, float) else str(value)


def verdict(total_score: dict, passing: int) -> tuple[str, bool, str]:
    """(표시 문구, 합격 여부, 부가 설명).

    계산기 판정은 assessment 모드에서만 나오고 training 모드는 항상 N/A다.
    N/A면 앱과 동일하게 점수 ≥ 기준점수 비교로 판정해 보여준다.
    """
    judg = total_score.get("judg_result")
    overall = total_score.get("overall") or 0
    if judg and str(judg).upper() != "N/A":
        return str(judg), str(judg).lower() in ("pass", "passed", "success"), "계산기 판정"
    ok = overall >= passing
    return ("PASS" if ok else "FAIL"), ok, f"training 모드(계산기 판정 N/A) — 기준점수 {passing} 비교"


def scan(directory: Path) -> list[dict]:
    """디렉토리의 *.bin 전체 → 목록 항목(최신순). 메타(.meta.json)가 있으면 함께 싣는다."""
    entries = []
    for bin_path in sorted(Path(directory).glob("*.bin")):
        base = bin_path.name.removesuffix(".bin")
        meta_path = bin_path.parent / f"{base}.meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
        cond = (meta or {}).get("condition") or {}
        ts_match = re.search(r"-(\d{10})-", f"-{base}-")
        kst = datetime.fromtimestamp(int(ts_match.group(1)), KST).strftime("%Y-%m-%d %H:%M:%S") if ts_match else "?"
        label = ""
        if meta:
            label = (f"{cond.get('target', '?'):6s} {cond.get('cpr_cycle_type', '?'):3s} "
                     f"{'2인' if cond.get('is_2rescuers') else '1인'} {cond.get('training_type', '?'):16s} "
                     f"{cond.get('guideline', '?')}")
        entries.append({
            "base": base, "bin": bin_path, "meta_path": meta_path if meta else None,
            "meta": meta, "has_meta": meta is not None, "kst": kst, "label": label,
        })
    entries.sort(key=lambda e: e["base"], reverse=True)  # 최신(타임스탬프 큰 것) 먼저
    return entries
