"""Reference behavior: more than four breaths use the zero/too_few bucket."""
from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.metric_evaluator import MetricEvaluator
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT, CALC_CASE_CPR
from config.enums import Actor
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config

GOOD = {"grade": 100, "criterion": "good"}

COND = {
    "mode": "training", "target": "adult", "training_type": "cpr",
    "guideline": "ARC2025", "cpr_cycle_type": "302", "is_2rescuers": False,
}


def _cfg_sw():
    return Config(COND), ScoreWeightFactory.create(COND["target"], COND["training_type"], COND["guideline"])


def _comp():
    data = {
        "action_type": ACTION_TYPE_COMP, "compression_count": 1, "ventilation_count": 0,
        "handsoff_ms": 0, "total_action_ms": 400, "compression_depth": [50] * 10,
    }
    return ActionWithScore(
        ACTION_TYPE_COMP, data, {k: GOOD for k in ("comp_depth", "comp_rate", "recoil", "hand_position")},
        Actor.REAL_PERSON,
    )


def _vent():
    data = {
        "action_type": ACTION_TYPE_VENT, "compression_count": 0, "ventilation_count": 2,
        "handsoff_ms": 0, "total_action_ms": 400, "compression_depth": [],
    }
    score = {"vent_vol": GOOD, "vent_speed": GOOD, "vent_rate": GOOD}
    return ActionWithScore(ACTION_TYPE_VENT, data, score, Actor.REAL_PERSON)


def _cpr_cycle(n_comp, n_vent):
    cfg, sw = _cfg_sw()
    actions = [_comp() for _ in range(n_comp)] + [_vent() for _ in range(n_vent)]
    cws = CycleWithScore(Cycle(actions, 1), 1, sw, cfg.calculation_config)
    cws.make_score(cfg.border)
    return cws


class TestVentCountCriterion(TestCase):
    """Reference classifies 5+ as too_few with grade 0; retain even this edge behavior."""

    def _judge(self, vent_cnt):
        cfg, sw = _cfg_sw()
        cws = CycleWithScore(Cycle([_comp()], 1), 1, sw, cfg.calculation_config)
        return cws._calc_vent_cnt_score(cfg.border, vent_cnt)

    def test_five_or_more_vents_use_reference_zero_bucket(self):
        for vent_cnt in (5, 6, 10):
            result = self._judge(vent_cnt)
            self.assertEqual(0, result["grade"], vent_cnt)  # 점수는 불변(0)
            self.assertEqual("too_few", result["criterion"], vent_cnt)

    def test_zero_to_four_vents_unchanged(self):
        # 원본 dart border 그대로: 0→too_few(0), 1→too_few(50), 2→good(100), 3→too_many(50), 4→too_many(0)
        self.assertEqual({"grade": 0, "criterion": "too_few"}, self._judge(0))
        self.assertEqual({"grade": 50, "criterion": "too_few"}, self._judge(1))
        self.assertEqual({"grade": 100, "criterion": "good"}, self._judge(2))
        self.assertEqual({"grade": 50, "criterion": "too_many"}, self._judge(3))
        self.assertEqual({"grade": 0, "criterion": "too_many"}, self._judge(4))


class TestVentCountFullPath(TestCase):
    """Verify reference classification reaches the response metric counters."""

    def test_five_vent_cycle_scores_zero_with_too_few(self):
        cws = _cpr_cycle(30, 5)
        self.assertEqual(CALC_CASE_CPR, cws.calc_case)
        self.assertEqual(0, cws.score_vent_count)
        self.assertEqual("too_few", cws.evaluate_result_vent_cnt["criterion"])

    def test_five_vent_cycle_bucketed_as_too_few(self):
        cfg, _ = _cfg_sw()
        cws = _cpr_cycle(30, 5)
        evaluator = MetricEvaluator(cfg.calculation_config)
        evaluator.collect_cycle_metrics([cws])
        self.assertEqual(0, evaluator.metric["VentilationCount"]["n_TooMany"])
        self.assertEqual(1, evaluator.metric["VentilationCount"]["n_TooFew"])
        self.assertEqual(0, evaluator.metric["VentilationCount"]["n_Good"])

    def test_two_vent_cycle_still_good(self):
        # 무회귀: 정상 2회 환기 사이클은 good(100) 그대로.
        cws = _cpr_cycle(30, 2)
        self.assertEqual(100, cws.score_vent_count)
        self.assertEqual("good", cws.evaluate_result_vent_cnt["criterion"])
