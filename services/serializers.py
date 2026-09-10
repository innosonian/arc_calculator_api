# 이식 출처: 원본 hstm_v2 services/serializers.py (66줄).
# 원본 대비 차이: _extract_training_stats 끝의 도달 불가 pass만 미이식(스펙 §4.5).
# score_rescue_vent 키는 merge_calculator가 더 이상 만들지 않으므로 응답에서 자연 소멸(스펙 §4.3).
# score_vent_rate_measured pop(응답 은폐)은 현행 유지. 나머지 구조 동일.
from models.calculation import CalculationResult
from services.guide_prompts import build_guide_prompts


def serialize_result(
    calculation_result: CalculationResult,
    condition: dict | None = None,
    usage: dict | None = None,
) -> dict:
    # 가이드 프롬프트가 코칭 전용 값(score_vent_rate_measured)을 소비한 뒤,
    # 해당 값은 API 응답 스키마에 노출하지 않도록 total_score에서 제거한다.
    guide_prompts = build_guide_prompts(calculation_result, condition, usage)
    total_score = calculation_result["cpr_total_scores"]
    total_score.pop("score_vent_rate_measured", None)
    return {
        "cpr_score": {
            "total_score": total_score,
            "part_scores": serialize_part_scores(calculation_result["part_with_scores"]),
        },
        "metrics": calculation_result["cpr_metrics"],
        "aed_score": calculation_result["aed_score"],
        "training_stats": _extract_training_stats(
            calculation_result["part_with_scores"],
            calculation_result["cpr_metrics"],
        ),
        "action_count": {
            "comp": calculation_result["comp_count"],
            "vent": calculation_result["vent_count"],
        },
        "guide_prompts": guide_prompts,
    }


def _extract_training_stats(part_with_score_list: list[dict], cpr_metrics: dict) -> dict:
    try:
        first_ts = part_with_score_list[0]["result"]["action_with_score_list"][0].action_data["first_timestamp"]
        last_ts = part_with_score_list[-1]["result"]["action_with_score_list"][-1].action_data["last_timestamp"]
        elapsed_seconds = (last_ts - first_ts) / 1000
    except (KeyError, IndexError):
        elapsed_seconds = 0

    return {
        "cycle_count": sum([len(p["result"]["cycle_with_score_list"]) for p in part_with_score_list]),
        "elapsed_seconds": elapsed_seconds,
    }


def serialize_part_scores(part_with_score_list: list[dict]) -> list[dict]:
    result = []
    for part_with_score in part_with_score_list:
        result.append(
            {
                "part_num": part_with_score["part_num"],
                "action_with_score_list": [
                    action_with_score.score for action_with_score in part_with_score["result"]["action_with_score_list"]
                ],
                "cycle_with_score_list": [
                    cycle_with_score.to_dict()
                    for cycle_with_score in part_with_score["result"]["cycle_with_score_list"]
                ],
                "score": part_with_score["result"]["score"],
            }
        )

    return result
