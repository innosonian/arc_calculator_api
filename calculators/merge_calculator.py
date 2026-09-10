from calculators.cycle_evaluator import CycleWithScore, NullPolicy
from config.borders import rise_linear_get_point
from config.calculation_config import BaseCalculationConfig
from config.constants import (
    CALC_CASE_NOT_CALC,
    CALC_CASE_CPR,
    CALC_CASE_ONLY_COMP,
    CALC_CASE_ONLY_VENT,
    CALC_CASE_RESCUE_VENT,
)
from config.enums import ActorType
from config.score_weight import ScoreWeightFactory
from services.config import Config
from util.custom_math import custom_round


def calculate_total_score(
    cycle_with_score_list: list[CycleWithScore],
    config: Config,
    null_policy: NullPolicy | None = None,
) -> dict:
    null_policy = null_policy or NullPolicy.inactive()
    sum_score_ccf = 0
    sum_score_comp_depth = 0
    sum_score_comp_no = 0
    sum_score_comp_rate = 0
    sum_score_hand_position = 0
    sum_score_recoil = 0
    sum_score_vent_rate = 0
    # 코칭 전용: 속도를 잴 수 있었던 사이클만 별도 합산(HSTM용 sum_score_vent_rate와 분리).
    sum_score_vent_rate_measured = 0
    rate_measured_cycle_count = 0
    sum_score_vent_count = 0
    sum_score_vent_speed = 0
    sum_score_vent_vol = 0
    sum_overall = 0
    sum_total_ms = 0
    sum_total_handsoff = 0
    score_rescue_vent = 0
    score_rescue_vent_vol = 0
    score_rescue_vent_count = 0
    score_rescue_vent_speed = 0
    rescue_overall = 0

    score_weight = ScoreWeightFactory.create(
        config.condition["target"],
        config.condition["training_type"],
        config.condition["guideline"],
    )

    calculation_config = config.calculation_config
    comp_cycle_count = len([1 for c in cycle_with_score_list if c.can_calc_comp()])
    vent_cycle_count = len([1 for c in cycle_with_score_list if c.can_calc_vent()])

    if not comp_cycle_count and not vent_cycle_count:
        result = {
            "score_comp_depth": 0,
            "score_comp_rate": 0,
            "score_comp_no": 0,
            "score_comp_count": 0,
            "score_recoil": 0,
            "score_hand_position": 0,
            "score_vent_vol": 0,
            "score_vent_rate": 0,
            "score_vent_count": 0,
            "score_vent_speed": 0,
            "score_ccf": 0,
            "score_rescue_vent": 0,
            "judg_result": "N/A",
            "overall": 0,
        }
        return _adj_field_value(calculation_config, result)

    for cycle_with_score in cycle_with_score_list:
        sum_total_ms += cycle_with_score.cycle.total_action_ms()
        sum_total_handsoff += cycle_with_score.buffer_assigned_handsoff

        # rescue vent cycle 이면~
        if cycle_with_score.did_rescue_vent:
            score_rescue_vent_vol = cycle_with_score.score_vent_vol
            score_rescue_vent_count = cycle_with_score.score_vent_count
            score_rescue_vent_speed = cycle_with_score.score_vent_speed
            rescue_overall = cycle_with_score.overall()
            continue

        if cycle_with_score.calc_case == CALC_CASE_NOT_CALC:
            continue

        # 실제 사용자 동작이 없는 사이클(파트너 단독 차례거나 세션 경계에서 잘린 사이클)은 평균에서 제외한다.
        # 정상 교대 구조라면 모든 사이클에서 사용자가 압박 또는 환기를 수행하므로 ONLY_VP는
        # 정상적으로 나올 수 없고, 나왔다면 아티팩트이므로 0점으로 벌하지 않는다.
        if cycle_with_score.cycle.actor_type == ActorType.ONLY_VIRTUAL_PARTNER:
            continue

        if cycle_with_score.score_ccf is not None:
            sum_score_ccf += cycle_with_score.score_ccf

        if cycle_with_score.can_calc_comp():
            if not null_policy.chest_null:
                sum_score_comp_depth += cycle_with_score.score_comp_depth
            if calculation_config.is_cpr():
                if not null_policy.chest_null:
                    sum_score_comp_no += cycle_with_score.score_comp_no
            sum_score_comp_rate += cycle_with_score.score_comp_rate
            if not null_policy.chest_null:
                sum_score_hand_position += cycle_with_score.score_hand_position
            if not null_policy.chest_null:
                sum_score_recoil += cycle_with_score.score_recoil

        if cycle_with_score.can_calc_vent():
            # 환기 0회 사이클(압박만 있고 환기 없는 중간 CPR 사이클)은 vol/rate가 None일 수 있다.
            # None을 그대로 더하면 TypeError(500)이므로 count와 동일하게 가드한다(집계 기준 불변).
            if cycle_with_score.score_vent_vol is not None:
                sum_score_vent_vol += cycle_with_score.score_vent_vol
            if cycle_with_score.score_vent_rate is not None:
                sum_score_vent_rate += cycle_with_score.score_vent_rate
            # 코칭 전용: 속도 측정 가능(환기 2회↑) 사이클만 평균에 포함(아티팩트 제외).
            if cycle_with_score.score_vent_rate_measured is not None:
                sum_score_vent_rate_measured += cycle_with_score.score_vent_rate_measured
                rate_measured_cycle_count += 1
            if cycle_with_score.score_vent_count is not None:
                sum_score_vent_count += cycle_with_score.score_vent_count

        if not null_policy.both_null:
            sum_overall += cycle_with_score.overall()

        if config.calculation_config.is_infant() and cycle_with_score.score_vent_speed is not None:
            sum_score_vent_speed += cycle_with_score.score_vent_speed

        if calculation_config.need_rescue_vent() and cycle_with_score.is_rescue_vent_cycle:
            score_rescue_vent = cycle_with_score.score_rescue_vent or 0

    total_cycle_count = len(
        [
            1
            for c in cycle_with_score_list
            if (
                c.calc_case != CALC_CASE_NOT_CALC
                and c.did_rescue_vent is None
                and c.cycle.actor_type != ActorType.ONLY_VIRTUAL_PARTNER
            )
        ]
    )

    if config.calculation_config.is_infant() and not config.calculation_config.is_cco():
        score_vent_speed = sum_score_vent_speed / vent_cycle_count if vent_cycle_count else None
    else:
        score_vent_speed = None

    if calculation_config.is_cpr():
        score_comp_count = sum_score_comp_no / comp_cycle_count if comp_cycle_count else None
    else:
        score_comp_count = None

    overall = sum_overall / total_cycle_count if total_cycle_count else 0

    score_ccf = None
    if calculation_config.is_cpr():
        score_ccf = sum_score_ccf / total_cycle_count if total_cycle_count else score_ccf
    elif calculation_config.is_cco():
        ccf = int(((sum_total_ms - sum_total_handsoff) / sum_total_ms) * 100)
        score_ccf = rise_linear_get_point(config.border, "ccf_comp", ccf)["grade"]

    # 환기 지표(vol/count/speed)는 환기 사이클 수로 나눈다 — comp(comp_cycle_count)·vent_rate와
    # 같은 기준. total_cycle_count로 나누면 2인구조(total≈2×환기사이클)에서 ½로 깎인다.
    score_vent_count = sum_score_vent_count / vent_cycle_count if vent_cycle_count else None
    result = {
        "score_comp_depth": sum_score_comp_depth / comp_cycle_count if comp_cycle_count else None,
        "score_comp_rate": sum_score_comp_rate / comp_cycle_count if comp_cycle_count else None,
        "score_comp_no": score_comp_count if calculation_config.is_cpr() else None,
        "score_comp_count": score_comp_count if calculation_config.is_cpr() else None,
        "score_recoil": sum_score_recoil / comp_cycle_count if comp_cycle_count else None,
        "score_hand_position": sum_score_hand_position / comp_cycle_count if comp_cycle_count else None,
        "score_vent_vol": (
            sum_score_vent_vol / vent_cycle_count
            if vent_cycle_count and not config.calculation_config.is_cco()
            else None
        ),
        "score_vent_rate": sum_score_vent_rate / vent_cycle_count if vent_cycle_count else None,
        # 코칭 판단 전용(HSTM 미전송): 속도 측정 가능 사이클만의 평균. 없으면 None(속도 코칭 안 함).
        "score_vent_rate_measured": (
            sum_score_vent_rate_measured / rate_measured_cycle_count if rate_measured_cycle_count else None
        ),
        "score_vent_count": score_vent_count if calculation_config.is_cpr() else None,
        "score_vent_speed": score_vent_speed,
        "score_ccf": score_ccf,
        "score_rescue_vent": score_rescue_vent,
        "overall": overall,
        "judg_result": "N/A",
    }

    # merge rescue vent
    if calculation_config.need_rescue_vent():
        result["overall"] = (
            result["overall"] * (1 - score_weight.RESCUE_VENT) + rescue_overall * score_weight.RESCUE_VENT
        )

        if score_rescue_vent_vol is not None:
            if result.get("score_vent_vol"):
                result["score_vent_vol"] = (
                    result["score_vent_vol"] * (1 - score_weight.RESCUE_VENT_VOLUME)
                    + score_rescue_vent_vol * score_weight.RESCUE_VENT_VOLUME
                )
            else:
                result["score_vent_vol"] = score_rescue_vent_vol * score_weight.RESCUE_VENT_VOLUME

        if score_rescue_vent_count is not None:
            if result.get("score_vent_count"):
                result["score_vent_count"] = (
                    result["score_vent_count"] * (1 - score_weight.RESCUE_VENT_COUNT)
                    + score_rescue_vent_count * score_weight.RESCUE_VENT_COUNT
                )
            else:
                result["score_vent_count"] = score_rescue_vent_count * score_weight.RESCUE_VENT_COUNT

        if calculation_config.is_infant():
            score_vent_speed = result.get("score_vent_speed") if result.get("score_vent_speed") else 0
            score_rescue_vent_speed = score_rescue_vent_speed if score_rescue_vent_speed else 0

            result["score_vent_speed"] = (
                score_vent_speed * (1 - score_weight.RESCUE_VENT_SPEED)
                + score_rescue_vent_speed * score_weight.RESCUE_VENT_SPEED
            )

    _adj_custom_round(result)

    return _apply_null_policy(calculation_config, null_policy, result)


def calculate_part_score(
    cycle_with_score_list: list[CycleWithScore],
    part_num: int,
    config: Config,
    null_policy: NullPolicy | None = None,
) -> dict:
    null_policy = null_policy or NullPolicy.inactive()
    sum_score_ccf = 0
    sum_score_comp_depth = 0
    sum_score_comp_no = 0
    sum_score_comp_rate = 0
    sum_score_hand_position = 0
    sum_score_recoil = 0
    sum_score_vent_rate = 0
    sum_score_vent_speed = 0
    sum_score_vent_vol = 0
    sum_score_vent_count = 0
    score_rescue_vent = None
    sum_overall = 0

    comp_cycle_count = len([1 for c in cycle_with_score_list if c.can_calc_comp()])
    vent_cycle_count = len([1 for c in cycle_with_score_list if c.can_calc_vent()])

    if not comp_cycle_count and vent_cycle_count and not null_policy.active:
        return {
            "score_comp_depth": 0,
            "score_comp_rate": 0,
            "score_comp_no": 0,
            "score_recoil": 0,
            "score_hand_position": 0,
            "score_vent_vol": 0,
            "score_vent_rate": 0,
            "score_vent_count": 0,
            "score_vent_speed": 0,
            "score_ccf": 0,
            "score_rescue_vent": 0,
            "overall": 0,
        }

    for cycle_with_score in cycle_with_score_list:
        if cycle_with_score.calc_case == CALC_CASE_NOT_CALC:
            continue
        if cycle_with_score.cycle.actor_type == ActorType.ONLY_VIRTUAL_PARTNER:
            continue
        if cycle_with_score.did_rescue_vent:
            score_rescue_vent = cycle_with_score.score_rescue_vent
            sum_score_vent_rate = cycle_with_score.score_vent_rate
            sum_score_vent_speed = cycle_with_score.score_vent_speed
            sum_score_vent_vol = cycle_with_score.score_vent_vol
            if not null_policy.both_null:
                sum_overall += cycle_with_score.overall()
            continue
        elif cycle_with_score.did_rescue_vent is False:
            score_rescue_vent = 0
            continue

        sum_score_ccf += cycle_with_score.score_ccf
        if (
            cycle_with_score.cycle.actor_type.can_calc_comp()
            and cycle_with_score.calculation_config.is_contain_comp()
            and cycle_with_score.calc_case in [CALC_CASE_CPR, CALC_CASE_ONLY_COMP]
        ):
            if not null_policy.chest_null:
                sum_score_comp_depth += cycle_with_score.score_comp_depth
            if cycle_with_score.calc_case == CALC_CASE_CPR:  # 임시 조치 2025/03/11  Author: David INNO-617
                if not null_policy.chest_null:
                    sum_score_comp_no += cycle_with_score.score_comp_no
            sum_score_comp_rate += cycle_with_score.score_comp_rate
            if not null_policy.chest_null:
                sum_score_hand_position += cycle_with_score.score_hand_position
            if not null_policy.chest_null:
                sum_score_recoil += cycle_with_score.score_recoil

        if cycle_with_score.can_calc_vent() and cycle_with_score.calc_case in [
            CALC_CASE_CPR,
            CALC_CASE_ONLY_VENT,
            CALC_CASE_RESCUE_VENT,
        ]:
            # 환기 0회 사이클은 rate/count/vol이 None일 수 있으므로 가드한다(None 덧셈 500 방지).
            if cycle_with_score.score_vent_rate is not None:
                sum_score_vent_rate += cycle_with_score.score_vent_rate
            if cycle_with_score.score_vent_count is not None:
                sum_score_vent_count += cycle_with_score.score_vent_count
            if cycle_with_score.score_vent_vol is not None:
                sum_score_vent_vol += cycle_with_score.score_vent_vol

        if not null_policy.both_null:
            sum_overall += cycle_with_score.overall()

        if config.calculation_config.is_infant() and cycle_with_score.score_vent_speed is not None:
            sum_score_vent_speed += cycle_with_score.score_vent_speed

    # if config.calculation_config.need_rescue_vent() and part_num == 1:
    #     weight = ScoreWeightInfantWithRescueVent
    #     sum_overall += score_rescue_vent * weight.RESCUE_VENT

    total_cycle_count = len(
        [
            1
            for c in cycle_with_score_list
            if (
                c.calc_case != CALC_CASE_NOT_CALC
                and c.did_rescue_vent is None
                and c.cycle.actor_type != ActorType.ONLY_VIRTUAL_PARTNER
            )
        ]
    )

    score_comp_count = custom_round(sum_score_comp_no / comp_cycle_count) if comp_cycle_count else None
    score_vent_count = custom_round(sum_score_vent_count / vent_cycle_count) if vent_cycle_count else None
    result = {
        "score_comp_depth": custom_round(sum_score_comp_depth / comp_cycle_count) if comp_cycle_count else None,
        "score_comp_rate": custom_round(sum_score_comp_rate / comp_cycle_count) if comp_cycle_count else None,
        "score_comp_no": score_comp_count if config.calculation_config.is_cpr() else None,
        "score_comp_count": score_comp_count if config.calculation_config.is_cpr() else None,
        "score_recoil": custom_round(sum_score_recoil / comp_cycle_count) if comp_cycle_count else None,
        "score_hand_position": custom_round(sum_score_hand_position / comp_cycle_count) if comp_cycle_count else None,
        "score_vent_vol": custom_round(sum_score_vent_vol / vent_cycle_count) if vent_cycle_count else None,
        "score_vent_rate": custom_round(sum_score_vent_rate / vent_cycle_count) if vent_cycle_count else None,
        "score_vent_count": score_vent_count if config.calculation_config.is_cpr() else None,
        "score_vent_speed": (
            (custom_round(sum_score_vent_speed / vent_cycle_count) if vent_cycle_count else None)
            if config.calculation_config.is_infant()
            else None
        ),
        "score_ccf": (
            custom_round(sum_score_ccf / total_cycle_count)
            if (total_cycle_count and not config.calculation_config.is_vent_only())
            else None
        ),
        "score_rescue_vent": score_rescue_vent,
        "overall": custom_round(sum_overall / total_cycle_count) if total_cycle_count else None,
    }

    result = _adj_field_value(config.calculation_config, result)
    return _apply_null_policy(config.calculation_config, null_policy, result)


def _adj_field_value(calculation_config: BaseCalculationConfig, data: dict) -> dict:
    training_type = calculation_config.condition["training_type"]

    result = {}
    for k in data.keys():
        result[k] = data[k]
        if k == "score_rescue_vent" and not calculation_config.need_rescue_vent():
            result[k] = None
        elif training_type == "compression_only" and "vent" in k:
            result[k] = None
        elif training_type == "ventilation_only" and (
            "comp" in k or k in ["score_recoil", "score_hand_position", "score_ccf"]
        ):
            result[k] = None

    return result


def _apply_null_policy(calculation_config: BaseCalculationConfig, null_policy: NullPolicy, data: dict) -> dict:
    """Null only the approved CPR groups; reference zero/null branches stay intact."""
    if not calculation_config.is_cpr() or not null_policy.active:
        return data
    result = dict(data)
    if null_policy.chest_null:
        for key in ("score_comp_depth", "score_comp_no", "score_comp_count", "score_recoil", "score_hand_position"):
            if key in result:
                result[key] = None
    if null_policy.vent_null:
        for key in ("score_vent_vol", "score_vent_rate", "score_vent_count", "score_vent_speed"):
            if key in result:
                result[key] = None
    if null_policy.both_null and "overall" in result:
        result["overall"] = None
    return result


def _adj_custom_round(calculation_result: dict) -> None:
    for key, value in calculation_result.items():
        if value is not None and key != "judg_result":
            calculation_result[key] = custom_round(value)
