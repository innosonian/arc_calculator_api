"""Reference calculation contracts outside the approved ARC CPR exception."""


import pytest


from calculators.cycle_evaluator import Cycle, CycleWithScore, NullPolicy


from calculators.merge_calculator import calculate_part_score, calculate_total_score


from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT


from config.enums import Actor


from config.score_weight import ScoreWeightFactory


from models.action import ActionWithScore


from services.config import Config

from services.calculators import calculate_cpr
from services.preparers import parse_data, make_pre_action_list, prepare_data
from tests._synth import cpr_session


def _config(training_type="cpr", target="adult", guideline="ARC2025"):
    return Config({
        "mode": "training", "target": target, "training_type": training_type,
        "guideline": guideline, "cpr_cycle_type": "152" if target == "infant" else "302",
        "is_2rescuers": False,
    })


def _action(action_type, rate=100, actor=Actor.REAL_PERSON):
    good = {"grade": 100, "criterion": "good"}
    data = {
        "action_type": action_type, "compression_count": 1, "ventilation_count": 2,
        "handsoff_ms": 0, "total_action_ms": 400, "compression_depth": [50] * 10,
    }
    scores = {key: dict(good) for key in (
        "comp_depth", "comp_rate", "recoil", "hand_position", "vent_vol", "vent_speed",
    )}
    scores["vent_rate"] = {"grade": rate, "criterion": "good"}
    return ActionWithScore(action_type, data, scores, actor)


def _cycle(config, actions, cycle_num=2):
    return CycleWithScore(
        Cycle(actions, cycle_num), 1,
        ScoreWeightFactory.create(
            config.condition["target"], config.condition["training_type"], config.condition["guideline"],
        ), config.calculation_config,
    )


def test_above_minimum_cpr_keeps_reference_weights_and_rescue_field_types():
    config = _config()
    cycle = _cycle(config, [_action(ACTION_TYPE_COMP), _action(ACTION_TYPE_VENT)])
    cycle.score_ccf = 62
    cycle.score_comp_depth = 80
    cycle.score_comp_rate = 70
    cycle.score_recoil = 60
    cycle.score_hand_position = 50
    cycle.score_comp_count = cycle.score_comp_no = 40
    cycle.score_vent_vol = 30
    cycle.score_vent_count = 20
    cycle.score_vent_rate = 11
    policy = NullPolicy.create(config.calculation_config, 90, 6)
    assert not policy.chest_null and not policy.vent_null
    # 62*.25 + 80*.20 + 70*.15 + 60*.15 + 50*.05 + 40*.05 + 30*.10 + 20*.05 = 59.5.
    assert cycle.overall() == 60
    total = calculate_total_score([cycle], config, policy)
    part = calculate_part_score([cycle], 1, config, policy)
    assert total["overall"] == part["overall"] == 60
    assert total["score_rescue_vent"] == 0
    assert type(total["score_rescue_vent"]) is int
    assert part["score_rescue_vent"] is None


def test_reference_ventilation_only_part_keeps_zero_fields_and_missing_alias():
    config = _config("ventilation_only")
    cycle = _cycle(config, [_action(ACTION_TYPE_VENT), _action(ACTION_TYPE_VENT)])
    cycle.make_score(config.border)
    part = calculate_part_score([cycle], 1, config)
    assert set(part) == {
        "score_comp_depth", "score_comp_rate", "score_comp_no", "score_recoil", "score_hand_position",
        "score_vent_vol", "score_vent_rate", "score_vent_count", "score_vent_speed", "score_ccf",
        "score_rescue_vent", "overall",
    }
    assert all(type(value) is int and value == 0 for value in part.values())
    total = calculate_total_score([cycle], config)
    assert total["score_comp_depth"] is None
    assert total["score_comp_count"] is None
    assert total["score_vent_vol"] == 100
    assert total["overall"] is not None


@pytest.mark.parametrize("target", ["adult", "child", "infant"])
@pytest.mark.parametrize("training_type", ["compression_only", "ventilation_only"])
def test_cpr_session_policy_is_inactive_for_reference_single_skill_modes(target, training_type):
    policy = NullPolicy.create(_config(training_type, target).calculation_config, 1, 1)
    assert not policy.chest_null and not policy.vent_null


@pytest.mark.parametrize("target", ["child", "infant"])
def test_erc_initial_rescue_retains_reference_twenty_percent_total_merge(target):
    config = _config(target=target, guideline="ERC2020")
    rescue = _cycle(config, [_action(ACTION_TYPE_VENT) for _ in range(5)], cycle_num=1)
    rescue.make_score(config.border)
    assert rescue.did_rescue_vent is True
    assert rescue.score_rescue_vent == 100
    assert rescue.overall() == 100

    regular = _cycle(config, [_action(ACTION_TYPE_COMP), _action(ACTION_TYPE_VENT)])
    # Reference ERC regular-cycle weights sum to .80; its cycle adjustment divides
    # by .80. Assigning 80 to every scored field therefore yields a cycle score of 80.
    for key in ("score_ccf", "score_comp_depth", "score_comp_rate", "score_recoil", "score_hand_position",
                "score_comp_count", "score_comp_no", "score_vent_vol", "score_vent_count", "score_vent_rate"):
        setattr(regular, key, 80)
    if target == "infant":
        regular.score_vent_speed = 80
    policy = NullPolicy.create(config.calculation_config, 1, 6)
    assert not policy.active
    assert regular.overall() == 80
    total = calculate_total_score([rescue, regular], config, policy)
    # Reference merge: regular 80 * .80 + rescue 100 * .20 = 84.
    assert total["overall"] == 84
    # Preserve the reference part formula too: rescue is added to its numerator,
    # while the denominator counts only the regular cycle: (100 + 80) / 1 = 180.
    part = calculate_part_score([rescue, regular], 1, config, policy)
    assert part["overall"] == 180
    assert part["score_rescue_vent"] == 100


@pytest.mark.parametrize("target,comp_count", [("child", 30), ("infant", 15)])
@pytest.mark.parametrize("initial_rescue", [False, True])
def test_erc_binary_initial_rescue_and_missing_rescue_follow_reference_paths(target, comp_count, initial_rescue):
    config = _config(target=target, guideline="ERC2020")
    session = ([(0, 5)] if initial_rescue else []) + [(comp_count, 2)]
    parsed = parse_data(cpr_session(session), b"", config)
    actions, aed_parts = make_pre_action_list(config, parsed, [])
    prepared = prepare_data(actions, aed_parts, config)
    result = calculate_cpr(prepared, config)
    cycles = [cycle for part in result["part_with_scores"] for cycle in part["result"]["cycle_with_score_list"]]
    first = cycles[0]
    assert first.did_rescue_vent is initial_rescue
    assert first.score_rescue_vent == (100 if initial_rescue else 0)
    assert all(not cycle.null_policy.active for cycle in cycles)
    if initial_rescue:
        assert first.calc_case == "only_vent"
        assert first.cycle.get_vent_count() == 5
    else:
        assert first.calc_case == "did_not_rescue_vent"
        assert first.cycle.actions == []  # The reference inserts an empty penalty cycle.
    assert cycles[1].cycle.cycle_num == 2
    assert cycles[1].score_comp_depth is not None
    assert cycles[1].score_vent_vol is not None
    assert result["cpr_total_scores"]["overall"] is not None
