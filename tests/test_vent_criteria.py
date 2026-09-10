# 원본: hstm_v2 tests/test_vent_criteria.py (ARC 각색 이식 — guideline ARC 계열로 교체.
# 문서 조립(_build_result_by_criteria)·레거시 변환(_convert_result_to_legacy) 테스트는 §4 제거
# 대상이라 미이식. ERC2020 child 케이스는 ARC2020 child로 각색(동일 계약: infant 외 None).)
"""환기 판정 분포(VentilationRate·VentilationSpeed) 회귀 테스트.

CPR의 VentilationRate criteria는 V1 계산기와 동일하게 사이클 단위 분당 환기율
판정으로 집계한다. VentilationSpeed는 infant 마네킨만 측정 가능하므로 infant 전
훈련에서만 채우고, 그 외 조건(child CPR 포함)은 None으로 남긴다.
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


def _condition(training_type="cpr", target="adult", guideline="ARC2020"):
    return {
        "mode": "training",
        "target": target,
        "training_type": training_type,
        "guideline": guideline,
        "cpr_cycle_type": "302",
        "is_2rescuers": False,
    }


def _comp_action(actor=Actor.REAL_PERSON):
    action_data = {
        "action_type": ACTION_TYPE_COMP,
        "compression_count": 15,
        "ventilation_count": 0,
        "handsoff_ms": 1000,
        "total_action_ms": 6000,
    }
    score = {
        "comp_depth": dict(GOOD, value=52),
        "comp_rate": dict(GOOD, value=110),
        "recoil": dict(GOOD),
        "hand_position": dict(GOOD),
    }
    return ActionWithScore(ACTION_TYPE_COMP, action_data, score, actor)


def _vent_action(speed_criterion=None, actor=Actor.REAL_PERSON):
    action_data = {
        "action_type": ACTION_TYPE_VENT,
        "compression_count": 0,
        "ventilation_count": 2,
        "ventilation_speed": 800,
        "handsoff_ms": 0,
        "total_action_ms": 4000,
    }
    score = {
        "vent_vol": dict(GOOD, value=550),
        "vent_rate": dict(GOOD),
    }
    if speed_criterion is not None:
        score["vent_speed"] = {"grade": 100, "criterion": speed_criterion}
    return ActionWithScore(ACTION_TYPE_VENT, action_data, score, actor)


def _cycle(actions, config, cycle_num=2, vent_rate_criterion=None):
    # cycle_num 기본 2: 원본 테스트 구조 보존(원본은 ERC rescue vent 첫 사이클 회피 목적).
    sw = ScoreWeightFactory.create(
        config.condition["target"], config.condition["training_type"], config.condition["guideline"]
    )
    cws = CycleWithScore(Cycle(actions, cycle_num), 1, sw, config.calculation_config)
    if vent_rate_criterion is not None:
        cws.evaluate_result_vent_rate = {"grade": 100, "criterion": vent_rate_criterion}
    return cws


def _evaluate(config, actions, cycles):
    evaluator = MetricEvaluator(config.calculation_config)
    return evaluator.evaluate([{"result": {"action_with_score_list": actions, "cycle_with_score_list": cycles}}])


class TestCycleVentRateJudgement(TestCase):
    """사이클 분당 환기율 판정: 60000 x 환기 수 / 사이클 ms를 vent_rate_1p 경계로 판정."""

    def _judge(self, vent_cnt, actions):
        config = Config(_condition())
        cws = _cycle(actions, config)
        cws.vent_cnt = vent_cnt
        return cws._calc_cycle_vent_rate(Config(_condition()).border)

    def test_good_rate(self):
        # 3 comp x 6000ms + 2 vent x 4000ms = 26000ms → 분당 4.6회, 경계 [2, 4, 12, 14]에서 good
        actions = [_comp_action() for _ in range(3)] + [_vent_action(), _vent_action()]
        result = self._judge(2, actions)
        self.assertEqual("good", result["criterion"])

    def test_no_vent_is_low(self):
        actions = [_comp_action() for _ in range(3)]
        result = self._judge(0, actions)
        self.assertEqual("low", result["criterion"])

    def test_zero_duration_is_low(self):
        config = Config(_condition())
        cws = _cycle([], config)
        result = cws._calc_cycle_vent_rate(config.border)
        self.assertEqual("low", result["criterion"])


class TestCprVentRateCriteria(TestCase):
    """CPR의 VentilationRate criteria는 사이클 판정 결과를 버킷으로 집계한다."""

    def test_cycle_judgements_bucketed(self):
        config = Config(_condition())
        actions = [_vent_action()]
        cycles = [
            _cycle([_comp_action()], config, vent_rate_criterion="good"),
            _cycle([_comp_action()], config, vent_rate_criterion="low"),
            _cycle([_comp_action()], config, vent_rate_criterion="high"),
        ]
        metric = _evaluate(config, actions, cycles)

        self.assertEqual(1, metric["VentilationRate"]["n_Good"])
        self.assertEqual(1, metric["VentilationRate"]["n_InFrequently"])
        self.assertEqual(1, metric["VentilationRate"]["n_TooFrequently"])
        self.assertEqual(
            100,
            metric["VentilationRate"]["%_Good"]
            + metric["VentilationRate"]["%_InFrequently"]
            + metric["VentilationRate"]["%_TooFrequently"],
        )

    def test_percent_computed_without_vent_actions(self):
        # 환기를 전혀 하지 않은 CPR도 사이클마다 InFrequently로 집계된다(V1과 동일).
        config = Config(_condition())
        cycles = [_cycle([_comp_action()], config, vent_rate_criterion="low")]
        metric = _evaluate(config, [], cycles)

        self.assertEqual(1, metric["VentilationRate"]["n_InFrequently"])
        self.assertEqual(100, metric["VentilationRate"]["%_InFrequently"])

    def test_unjudged_cycles_not_bucketed(self):
        # 판정이 세팅되지 않은 사이클(VP 등)은 분포에 들어가지 않는다.
        config = Config(_condition())
        cycles = [_cycle([_comp_action()], config)]
        metric = _evaluate(config, [], cycles)

        self.assertEqual(0, metric["VentilationRate"]["n_Good"])
        self.assertEqual(0, metric["VentilationRate"]["n_InFrequently"])
        self.assertEqual(0, metric["VentilationRate"]["n_TooFrequently"])


class TestVentSpeedInfantOnly(TestCase):
    """환기 속도는 infant 마네킨만 측정 가능하므로 infant에서만 분포를 채운다."""

    def test_infant_filled(self):
        config = Config(_condition(target="infant"))
        actions = [
            _vent_action(speed_criterion="good"),
            _vent_action(speed_criterion="low"),
            _vent_action(speed_criterion="high"),
        ]
        metric = _evaluate(config, actions, [])

        # 주의: 값이 지속시간(ms)이라 criterion "low"(짧음)→n_TooFast, "high"(김)→n_TooSlow로 매핑된다.
        self.assertEqual(1, metric["VentilationSpeed"]["n_Good"])
        self.assertEqual(1, metric["VentilationSpeed"]["n_TooFast"])
        self.assertEqual(1, metric["VentilationSpeed"]["n_TooSlow"])

    def test_child_cpr_stays_none(self):
        # child 마네킨은 환기 속도를 보고하지 않으므로 분포를 채우지 않는다.
        config = Config(_condition(target="child"))
        metric = _evaluate(config, [_vent_action()], [])
        self.assertIsNone(metric["VentilationSpeed"])

    def test_adult_cpr_stays_none(self):
        config = Config(_condition())
        metric = _evaluate(config, [_vent_action()], [])
        self.assertIsNone(metric["VentilationSpeed"])
