"""Reference arithmetic for independently restored B1/B3 and CCF/CCO metrics."""


import pytest


from calculators.cycle_evaluator import Cycle, CycleWithScore


from calculators.metric_evaluator import MetricEvaluator


from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT


from config.enums import Actor


from config.score_weight import ScoreWeightFactory


from models.action import ActionWithScore


from services.config import Config


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


@pytest.mark.parametrize("training_type,cycle_num,rates,expected", [
    ("cpr", 1, [100, 0], 100),
    ("cpr", 2, [100, 100], 200),
    ("cpr", 2, [100, 0, 0], 50),
    ("ventilation_only", 1, [100, 0], 0),
    ("ventilation_only", 2, [100, 0], 100),
])
def test_reference_vent_rate_first_action_and_divisor(training_type, cycle_num, rates, expected):
    config = _config(training_type)
    actions = ([] if training_type == "ventilation_only" else [_action(ACTION_TYPE_COMP)])
    actions += [_action(ACTION_TYPE_VENT, rate) for rate in rates]
    cycle = _cycle(config, actions, cycle_num)
    cycle._embed_vent_score()
    # The reference skips the first action only in the first only-vent cycle;
    # every other numerator includes it while the denominator remains n - 1.
    assert cycle.score_vent_rate == expected


@pytest.mark.parametrize("count,expected", [
    (0, {"grade": 0, "criterion": "too_few"}),
    (2, {"grade": 100, "criterion": "good"}),
    (4, {"grade": 0, "criterion": "too_many"}),
    (5, {"grade": 0, "criterion": "too_few"}),
    (12, {"grade": 0, "criterion": "too_few"}),
])
def test_reference_vent_count_over_four_uses_zero_bucket(count, expected):
    config = _config()
    cycle = _cycle(config, [_action(ACTION_TYPE_COMP)])
    assert cycle._calc_vent_cnt_score(config.border, count) == expected


def test_reference_ccf_metric_includes_only_virtual_partner_cycles():
    config = _config()
    real = _cycle(config, [_action(ACTION_TYPE_COMP), _action(ACTION_TYPE_VENT)])
    partner = _cycle(config, [
        _action(ACTION_TYPE_COMP, actor=Actor.VIRTUAL_PARTNER),
        _action(ACTION_TYPE_VENT, actor=Actor.VIRTUAL_PARTNER),
    ])
    real.ccf, real.score_ccf = 80, 90
    partner.ccf, partner.score_ccf = 0, 0
    evaluator = MetricEvaluator(config.calculation_config)
    metrics = evaluator.evaluate([{"result": {"action_with_score_list": [], "cycle_with_score_list": [real, partner]}}])
    assert metrics["CCF"]["ccf_count"] == 2
    assert metrics["CCF"]["%_CCF"] == 40  # (80 + 0) / 2
    assert metrics["ScoreOfCCF"] == 45  # (90 + 0) / 2


def test_reference_compression_only_ventilation_speed_metric_is_zero_dictionary():
    metrics = MetricEvaluator(_config("compression_only").calculation_config).evaluate([])
    assert isinstance(metrics["VentilationSpeed"], dict)
    assert metrics["VentilationSpeed"]
    assert all(value == 0 for value in metrics["VentilationSpeed"].values())
