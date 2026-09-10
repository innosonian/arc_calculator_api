# Reference excludes ONLY_VP from total score but includes it in CCF metrics.
from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.merge_calculator import calculate_total_score
from calculators.metric_evaluator import MetricEvaluator
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT, CALC_CASE_CPR
from config.enums import Actor, ActorType
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config

GOOD = {"grade": 100, "criterion": "good"}


def _act(action_type, actor):
    action_data = {"action_type": action_type, "compression_count": 15, "ventilation_count": 2,
                   "handsoff_ms": 0, "total_action_ms": 400}
    score = {k: GOOD for k in ("comp_depth", "comp_rate", "recoil", "hand_position",
                               "vent_vol", "vent_speed", "vent_rate")}
    return ActionWithScore(action_type, action_data, score, actor)


def _cfg():
    return Config({"mode": "training", "target": "adult", "training_type": "cpr",
                   "guideline": "ARC2020", "cpr_cycle_type": "302", "is_2rescuers": True})


def _sw():
    return ScoreWeightFactory.create("adult", "cpr", "ARC2020")


class TestOnlyVpExclusion(TestCase):
    """실제 사용자 동작이 없는 ONLY_VP 사이클은 0점으로 평균을 끌어내리지 않고 제외되어야 한다.

    정상 교대 구조에서는 모든 사이클에서 사용자가 압박 또는 환기를 수행하므로 ONLY_VP는
    정상적으로 나올 수 없고(나왔다면 파트너 단독 차례거나 세션 경계에서 잘린 사이클),
    아티팩트이므로 0점 벌점 대신 평균에서 제외한다.
    """

    def _cws(self, actions, sw, cfg):
        cws = CycleWithScore(Cycle(actions, 1), 1, sw, cfg.calculation_config)
        cws.calc_case = CALC_CASE_CPR
        cws.score_ccf = 100
        if cws.can_calc_comp():
            cws.score_comp_depth = cws.score_comp_rate = cws.score_recoil = 100
            cws.score_hand_position = cws.score_comp_no = cws.score_comp_count = 100
        if cws.can_calc_vent():
            cws.score_vent_vol = cws.score_vent_count = cws.score_vent_rate = 100
        return cws

    def test_only_vp_cycle_excluded_from_total(self):
        cfg = _cfg()
        sw = _sw()

        # 사용자가 압박을 잘 수행한 사이클(=VP_VENT) → overall 100
        real_cycle = self._cws(
            [_act(ACTION_TYPE_COMP, Actor.REAL_PERSON)] * 15 + [_act(ACTION_TYPE_VENT, Actor.VIRTUAL_PARTNER)] * 2,
            sw, cfg)
        # 사용자가 아무것도 안 한 사이클(=ONLY_VP)
        only_vp = self._cws(
            [_act(ACTION_TYPE_COMP, Actor.VIRTUAL_PARTNER)] * 15 + [_act(ACTION_TYPE_VENT, Actor.VIRTUAL_PARTNER)] * 2,
            sw, cfg)

        self.assertEqual(ActorType.ONLY_VIRTUAL_PARTNER, only_vp.cycle.actor_type)

        total = calculate_total_score([real_cycle, only_vp], cfg)
        # ONLY_VP가 0점으로 평균을 깎지 않고 제외되어 100이 되어야 한다(2개 평균 50이 아님).
        self.assertEqual(100, total["overall"])


class TestOnlyVpIncludedInCcfMetric(TestCase):
    """Preserve the reference distinction between total and metric aggregation."""

    def _ccf_cycle(self, actions, ccf, score_ccf):
        cfg = _cfg()
        cws = CycleWithScore(Cycle(actions, 1), 1, _sw(), cfg.calculation_config)
        cws.calc_case = CALC_CASE_CPR
        cws.ccf = ccf
        cws.score_ccf = score_ccf
        return cws

    def test_only_vp_cycle_aggregated_into_ccf(self):
        cfg = _cfg()
        real_cycle = self._ccf_cycle(
            [_act(ACTION_TYPE_COMP, Actor.REAL_PERSON)] * 15 + [_act(ACTION_TYPE_VENT, Actor.VIRTUAL_PARTNER)] * 2,
            ccf=80, score_ccf=90)
        only_vp = self._ccf_cycle(
            [_act(ACTION_TYPE_COMP, Actor.VIRTUAL_PARTNER)] * 15 + [_act(ACTION_TYPE_VENT, Actor.VIRTUAL_PARTNER)] * 2,
            ccf=0, score_ccf=0)
        self.assertEqual(ActorType.ONLY_VIRTUAL_PARTNER, only_vp.cycle.actor_type)

        evaluator = MetricEvaluator(cfg.calculation_config)
        metric = evaluator.evaluate(
            [{"result": {"action_with_score_list": [], "cycle_with_score_list": [real_cycle, only_vp]}}]
        )

        # Reference means: (80 + 0) / 2 = 40 and (90 + 0) / 2 = 45.
        self.assertEqual(40, metric["CCF"]["%_CCF"])
        self.assertEqual(45, metric["ScoreOfCCF"])
        self.assertEqual(2, metric["CCF"]["ccf_count"])
