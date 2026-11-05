"""압박 지표(rate/hand, 압박 액션 수, 평균 압박 속도) 집계 범위 테스트.

압박 지표는 압박을 채점한 사이클(can_calc_comp)의 실제 사용자(RP) 압박만 센다. 압박 비채점
사이클(NOT_CALC, VP:COMP, ONLY:VP)의 RP 압박은 점수에 포함되지 않으므로 지표에서도 제외한다.
depth/recoil 지표가 채점 사이클의 파형 이벤트만 세는 것과 같은 기준이다.
"""

from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.metric_evaluator import MetricEvaluator
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT, CALC_CASE_NOT_CALC
from config.enums import Actor, ActorType
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config

SW = ScoreWeightFactory.create("adult", "cpr", "AHA2020")
CONFIG = Config(
    {
        "mode": "training",
        "target": "adult",
        "training_type": "cpr",
        "guideline": "AHA2020",
        "cpr_cycle_type": "302",
        "is_2rescuers": True,
    }
)


def _comp(actor=Actor.REAL_PERSON, rate=("good", 110), hand="good"):
    action_data = {
        "action_type": ACTION_TYPE_COMP,
        "compression_count": 15,
        "ventilation_count": 0,
        "handsoff_ms": 0,
        "total_action_ms": 400,
    }
    score = {
        "comp_depth": {"grade": 100, "criterion": "good", "value": 52},
        "recoil": {"grade": 100, "criterion": "good"},
        "comp_rate": {"grade": 100, "criterion": rate[0], "value": rate[1]},
        "hand_position": {"grade": 100, "criterion": hand},
    }
    return ActionWithScore(ACTION_TYPE_COMP, action_data, score, actor)


def _vent(actor=Actor.REAL_PERSON):
    action_data = {
        "action_type": ACTION_TYPE_VENT,
        "compression_count": 0,
        "ventilation_count": 2,
        "ventilation_speed": 800,
        "handsoff_ms": 0,
        "total_action_ms": 2000,
    }
    score = {"vent_vol": {"grade": 100, "criterion": "good", "value": 550}}
    return ActionWithScore(ACTION_TYPE_VENT, action_data, score, actor)


def _cycle(actions, cycle_num=1, is_last_cycle=False):
    return CycleWithScore(Cycle(actions, cycle_num, is_last_cycle), 1, SW, CONFIG.calculation_config)


def _scored_cycle():
    # 사용자가 압박 15회·환기 2회를 수행한 채점 사이클(ONLY:RP).
    return _cycle([_comp() for _ in range(15)] + [_vent(), _vent()], cycle_num=1)


def _vp_comp_cycle(cycle_num):
    # 압박은 VP가 주로 수행하고 환기는 사용자가 수행한 사이클(VP:COMP). 소수 RP 압박은 채점되지 않는다.
    return _cycle(
        [_comp(actor=Actor.VIRTUAL_PARTNER) for _ in range(13)]
        + [_comp(rate=("high", 150), hand="down") for _ in range(2)]
        + [_vent(), _vent()],
        cycle_num=cycle_num,
    )


def _evaluate(cycles):
    actions = [a for c in cycles for a in c.cycle.actions]
    evaluator = MetricEvaluator(CONFIG.calculation_config)
    return evaluator.evaluate([{"result": {"action_with_score_list": actions, "cycle_with_score_list": cycles}}])


class TestCompMetricScope(TestCase):
    def test_not_calc_cycle_rp_comps_excluded(self):
        # 마지막 사이클이 압박 10회 미만·환기 없음이면 NOT_CALC다.
        not_calc = _cycle([_comp(rate=("low", 80), hand="side") for _ in range(3)], cycle_num=2, is_last_cycle=True)
        self.assertEqual(CALC_CASE_NOT_CALC, not_calc.calc_case)
        m = _evaluate([_scored_cycle(), not_calc])
        self.assertEqual(15, m["CompressionActionNumber"])
        self.assertEqual(15, m["CompressionRate"]["n_Good"])
        self.assertEqual(0, m["CompressionRate"]["n_TooSlow"])
        self.assertEqual(0, m["HandPosition"]["n_IncorrectLR"])
        self.assertEqual(110, m["AvgCompressionRate"])

    def test_vp_comp_cycle_rp_comps_excluded(self):
        vp_comp = _vp_comp_cycle(cycle_num=2)
        self.assertEqual(ActorType.VIRTUAL_PARTNER_COMP, vp_comp.cycle.actor_type)
        self.assertFalse(vp_comp.can_calc_comp())
        m = _evaluate([_scored_cycle(), vp_comp])
        self.assertEqual(15, m["CompressionActionNumber"])
        self.assertEqual(0, m["CompressionRate"]["n_TooFast"])
        self.assertEqual(0, m["HandPosition"]["n_IncorrectStomach"])
        self.assertEqual(110, m["AvgCompressionRate"])

    def test_only_vp_cycle_rp_comps_excluded(self):
        # 압박·환기 모두 VP가 주로 수행한 사이클(ONLY:VP)의 소수 RP 압박도 제외한다.
        only_vp = _cycle(
            [_comp(actor=Actor.VIRTUAL_PARTNER) for _ in range(13)]
            + [_comp(rate=("high", 150), hand="down") for _ in range(2)]
            + [_vent(actor=Actor.VIRTUAL_PARTNER), _vent(actor=Actor.VIRTUAL_PARTNER)],
            cycle_num=2,
        )
        self.assertEqual(ActorType.ONLY_VIRTUAL_PARTNER, only_vp.cycle.actor_type)
        m = _evaluate([_scored_cycle(), only_vp])
        self.assertEqual(15, m["CompressionActionNumber"])
        self.assertEqual(0, m["CompressionRate"]["n_TooFast"])
        self.assertEqual(0, m["HandPosition"]["n_IncorrectStomach"])

    def test_scored_cycle_counts_rp_comps_only(self):
        # 채점 사이클에 섞인 VP 압박은 이전과 같이 제외하고 RP 압박은 모두 센다.
        mixed = _cycle(
            [_comp(actor=Actor.VIRTUAL_PARTNER, rate=("high", 150), hand="down") for _ in range(2)]
            + [_comp() for _ in range(13)]
            + [_vent(), _vent()]
        )
        self.assertTrue(mixed.can_calc_comp())
        m = _evaluate([mixed])
        self.assertEqual(13, m["CompressionActionNumber"])
        self.assertEqual(13, m["CompressionRate"]["n_Good"])
        self.assertEqual(0, m["HandPosition"]["n_IncorrectStomach"])
        self.assertEqual(110, m["AvgCompressionRate"])

    def test_session_without_scored_comp_cycle_has_zero_comp_metrics(self):
        m = _evaluate([_vp_comp_cycle(cycle_num=1)])
        self.assertEqual(0, m["CompressionActionNumber"])
        self.assertEqual(0, m["CompressionRate"]["n_TooFast"])
        self.assertEqual(0, m["HandPosition"]["n_IncorrectStomach"])
        self.assertEqual(0, m["AvgCompressionRate"])
