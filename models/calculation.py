# 원본 hstm_v2 models/calculation.py (V1 패리티 보존)
from typing import TypedDict


class CalculationResult(TypedDict):
    cpr_total_scores: dict
    part_with_scores: list[dict]
    cpr_metrics: dict
    aed_score: dict
    comp_count: int
    vent_count: int
