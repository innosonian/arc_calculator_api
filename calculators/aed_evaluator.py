# [provenance] 원본 hstm_v2 calculators/aed_evaluator.py:1-97 동일 이식 (V1 패리티 보존).
# 상수·수식·비교연산·반올림 일체 무변경. 주석만 추가(provenance·B-12).
from config.borders import fall_linear_get_point, BaseBorder
from config.constants import (
    AED_EVENT_ARRIVED,
    AED_EVENT_POWER_ON,
    AED_EVENT_PADS_ON,
    AED_EVENT_NO_SHOCK_ADVISE,
    AED_EVENT_SHOCK_DELIVERED,
)
from util.custom_math import custom_round


def milliToSec(milliseconds):
    return milliseconds / 1000


def calculate_aed_score(aed_data_list: list[dict], border: BaseBorder) -> dict:
    if len(aed_data_list) == 0:
        return {
            "overall": 0,
            "part_scores": [],
        }

    total_score = 0
    part_scores = []

    for aed_part_data in aed_data_list:
        if aed_part_data["part_cnt"] == 1:
            part_score = _evaluate_first_part(aed_part_data, border)
        else:
            part_score = _evaluate_subsequent_part(aed_part_data, border)

        total_score += part_score["score"]
        part_scores.append(
            {
                "overall": part_score["score"],
                "part_cnt": aed_part_data["part_cnt"],
                "total_sec": part_score["total_sec"],
                "intervals": {k: v for k, v in part_score.items() if k not in ["total_sec", "score"]},
                "guide_sec": border.get_aed_guide_sec(aed_part_data["part_cnt"]),
            }
        )

    return {
        "overall": custom_round(total_score / len(aed_data_list)),
        "part_scores": part_scores,
    }


def _evaluate_subsequent_part(aed_part_data: dict, border: BaseBorder) -> dict:
    t3 = 0
    previous_timestamp = 0
    for aed_event_list in aed_part_data["aed_part_data"]:
        if aed_event_list["event"] == AED_EVENT_PADS_ON:
            previous_timestamp = aed_event_list["timestamp"]
        elif (
            aed_event_list["event"] == AED_EVENT_SHOCK_DELIVERED or aed_event_list["event"] == AED_EVENT_NO_SHOCK_ADVISE
        ):
            t3 = aed_event_list["timestamp"] - previous_timestamp

    t3 = milliToSec(t3)
    score = fall_linear_get_point(border, "aed_time_2", t3)["grade"]

    return {"t3": t3, "total_sec": t3, "score": score}


def _evaluate_first_part(aed_part_data: dict, border: BaseBorder) -> dict:
    arrived_timestamp = 0
    power_on_timestamp = 0
    pads_on_timestamp = 0
    shock_timestamp = 0

    for aed_event_list in aed_part_data["aed_part_data"]:
        if aed_event_list["event"] == AED_EVENT_ARRIVED:
            arrived_timestamp = aed_event_list["timestamp"]
        if aed_event_list["event"] == AED_EVENT_POWER_ON:
            power_on_timestamp = aed_event_list["timestamp"]
        elif aed_event_list["event"] == AED_EVENT_PADS_ON:
            pads_on_timestamp = aed_event_list["timestamp"]
        elif (
            aed_event_list["event"] == AED_EVENT_SHOCK_DELIVERED or aed_event_list["event"] == AED_EVENT_NO_SHOCK_ADVISE
        ):
            shock_timestamp = aed_event_list["timestamp"]

    t1 = power_on_timestamp - arrived_timestamp
    t2 = pads_on_timestamp - power_on_timestamp
    t3 = shock_timestamp - pads_on_timestamp

    # [B-12 승인: 현행 유지] t1만 음수/과대(>10,000초) 가드하고 t2/t3는 가드하지 않는다.
    # 이벤트 완전성은 상류 보장으로 간주 (원본 hstm_v2 aed_evaluator.py:88-89, V1 패리티 보존).
    if t1 < 0 or t1 > 10000000:
        t1 = 0

    t_total = milliToSec(t1 + t2 + t3)
    t1 = milliToSec(t1)
    t2 = milliToSec(t2)
    t3 = milliToSec(t3)
    score = fall_linear_get_point(border, "aed_time", t_total)["grade"]

    return {"t1": t1, "t2": t2, "t3": t3, "total_sec": t_total, "score": score}
