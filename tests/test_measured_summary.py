# 원본: hstm_v2 tests/test_measured_summary.py (ARC 각색 이식 — guideline ARC2020, 문서
# 제출용 맥락 서술만 제거. 실측 평균/%CCF 집계 계약과 기대값은 원본 그대로.)
"""실측 요약(MetricEvaluator)과 %CCF 집계 회귀 테스트.

metrics의 실측 요약은 채점 점수가 아니라 실측 평균값(압박 속도 cpm, 깊이 mm,
환기량 ml, 환기 속도 ms, hands-off 초)과 실측 CCF 비율을 담는다.
sum_ccf 미누적으로 CCF.%_CCF가 항상 0이던 문제의 회귀도 함께 고정한다.
"""

from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.metric_evaluator import MetricEvaluator
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from config.enums import Actor
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config

GOOD = {"grade": 100, "criterion": "good"}


def _condition(training_type="cpr", target="adult"):
    return {
        "mode": "training",
        "target": target,
        "training_type": training_type,
        "guideline": "ARC2020",
        "cpr_cycle_type": "302",
        "is_2rescuers": False,
    }


def _comp_action(rate_value=110, handsoff_ms=1000, actor=Actor.REAL_PERSON):
    action_data = {
        "action_type": ACTION_TYPE_COMP,
        "compression_count": 15,
        "ventilation_count": 0,
        "handsoff_ms": handsoff_ms,
        "total_action_ms": 6000,
    }
    score = {
        "comp_depth": dict(GOOD, value=52),
        "comp_rate": dict(GOOD, value=rate_value),
        "recoil": dict(GOOD),
        "hand_position": dict(GOOD),
    }
    return ActionWithScore(ACTION_TYPE_COMP, action_data, score, actor)


def _vent_action(vol_value=550, speed_ms=800, actor=Actor.REAL_PERSON):
    action_data = {
        "action_type": ACTION_TYPE_VENT,
        "compression_count": 0,
        "ventilation_count": 2,
        "ventilation_speed": speed_ms,
        "handsoff_ms": 0,
        "total_action_ms": 4000,
    }
    score = {
        "vent_vol": dict(GOOD, value=vol_value),
        "vent_rate": dict(GOOD),
    }
    return ActionWithScore(ACTION_TYPE_VENT, action_data, score, actor)


def _cycle(actions, config, ccf=None, score_ccf=None, depth_values=()):
    sw = ScoreWeightFactory.create(
        config.condition["target"], config.condition["training_type"], config.condition["guideline"]
    )
    cws = CycleWithScore(Cycle(actions, 1), 1, sw, config.calculation_config)
    cws.ccf = ccf
    cws.score_ccf = score_ccf
    cws.comp_event_scores = [{"comp_depth": dict(GOOD, value=v), "recoil": dict(GOOD)} for v in depth_values]
    return cws


def _evaluate(config, actions, cycles):
    evaluator = MetricEvaluator(config.calculation_config)
    return evaluator.evaluate([{"result": {"action_with_score_list": actions, "cycle_with_score_list": cycles}}])


class TestCcfPercentAggregation(TestCase):
    def test_percent_ccf_is_cycle_average(self):
        config = Config(_condition())
        cycles = [
            _cycle([_comp_action()], config, ccf=80, score_ccf=90),
            _cycle([_comp_action()], config, ccf=100, score_ccf=100),
        ]
        metric = _evaluate(config, [], cycles)

        self.assertEqual(90, metric["CCF"]["%_CCF"])
        self.assertEqual(95, metric["ScoreOfCCF"])


class TestMeasuredAverages(TestCase):
    def test_cpr_measured_averages(self):
        config = Config(_condition())
        actions = [
            _comp_action(rate_value=100, handsoff_ms=1000),
            _comp_action(rate_value=120, handsoff_ms=1000),
            _vent_action(vol_value=500, speed_ms=700),
            _vent_action(vol_value=600, speed_ms=900),
        ]
        # 사이클 1: hands-off 2000ms, 사이클 2: hands-off 4000ms → 평균 3초
        cycles = [
            _cycle(actions, config, ccf=90, score_ccf=100, depth_values=(50, 54)),
            _cycle([_comp_action(handsoff_ms=4000)], config, ccf=90, score_ccf=100, depth_values=(52,)),
        ]
        metric = _evaluate(config, actions, cycles)

        self.assertEqual(110, metric["AvgCompressionRate"])
        self.assertEqual(52, metric["AvgCompressionDepth"])
        self.assertEqual(550, metric["AvgVentilationVolume"])
        self.assertEqual(800, metric["AvgVentilationSpeed"])
        self.assertEqual(3, metric["HandsOffTimeSec"])

    def test_vp_actions_excluded_from_averages(self):
        config = Config(_condition())
        actions = [
            _comp_action(rate_value=110),
            _comp_action(rate_value=250, actor=Actor.VIRTUAL_PARTNER),
            _vent_action(vol_value=500, speed_ms=700),
            _vent_action(vol_value=999, speed_ms=9999, actor=Actor.VIRTUAL_PARTNER),
        ]
        metric = _evaluate(config, actions, [_cycle(actions, config)])

        self.assertEqual(110, metric["AvgCompressionRate"])
        self.assertEqual(500, metric["AvgVentilationVolume"])
        self.assertEqual(700, metric["AvgVentilationSpeed"])

    def test_cco_handsoff_is_session_total(self):
        config = Config(_condition(training_type="compression_only"))
        actions = [_comp_action(handsoff_ms=2500), _comp_action(handsoff_ms=1000)]
        metric = _evaluate(config, actions, [_cycle(actions, config)])

        # V1과 동일: compression only는 사이클 평균이 아니라 세션 총합(초, 내림)
        self.assertEqual(3, metric["HandsOffTimeSec"])

    def test_vent_only_handsoff_is_zero(self):
        config = Config(_condition(training_type="ventilation_only"))
        actions = [_vent_action()]
        metric = _evaluate(config, actions, [_cycle(actions, config)])

        self.assertEqual(0, metric["HandsOffTimeSec"])
