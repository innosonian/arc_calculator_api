# 원본: hstm_v2 tests/test_vent_aggregation.py (ARC 각색 이식 — guideline만 ARC2020으로 교체)
from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.merge_calculator import calculate_total_score
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT, CALC_CASE_CPR
from config.enums import Actor, ActorType
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config

GOOD = {"grade": 100, "criterion": "good"}


def _act(action_type, actor):
    action_data = {
        "action_type": action_type,
        "compression_count": 15,
        "ventilation_count": 2,
        "handsoff_ms": 0,
        "total_action_ms": 400,
    }
    score = {k: GOOD for k in ("comp_depth", "comp_rate", "recoil", "hand_position", "vent_vol", "vent_speed", "vent_rate")}
    return ActionWithScore(action_type, action_data, score, actor)


class TestVentAggregationDenominator(TestCase):
    """환기 지표(vol/count/speed)는 환기 사이클 수(vent_cycle_count)로 나눠야 한다.

    total_cycle_count로 나누면 환기가 없는 사이클(2인구조의 압박 차례, 1인구조의 마지막
    comp 전용 사이클 등)이 분모에 섞여 환기점수가 깎인다. comp(comp_cycle_count)·
    vent_rate(vent_cycle_count)와 분모를 일치시킨다.
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

    def test_vent_metrics_use_vent_cycle_count_not_total(self):
        cfg = Config(
            {
                "mode": "training",
                "target": "adult",
                "training_type": "cpr",
                "guideline": "ARC2020",
                "cpr_cycle_type": "302",
                "is_2rescuers": True,
            }
        )
        sw = ScoreWeightFactory.create("adult", "cpr", "ARC2020")

        # 사용자 압박 차례(VP_VENT): 환기는 VP가 해 채점 안 됨
        def comp_cycle():
            return self._cws(
                [_act(ACTION_TYPE_COMP, Actor.REAL_PERSON)] * 15 + [_act(ACTION_TYPE_VENT, Actor.VIRTUAL_PARTNER)] * 2,
                sw,
                cfg,
            )

        # 사용자 환기 차례(VP_COMP): 환기 100점
        def vent_cycle():
            return self._cws(
                [_act(ACTION_TYPE_COMP, Actor.VIRTUAL_PARTNER)] * 15 + [_act(ACTION_TYPE_VENT, Actor.REAL_PERSON)] * 2,
                sw,
                cfg,
            )

        cycles = [comp_cycle(), vent_cycle(), comp_cycle(), vent_cycle()]  # 환기 사이클 2, 전체 4
        self.assertEqual(ActorType.VIRTUAL_PARTNER_VENT, cycles[0].cycle.actor_type)
        self.assertEqual(ActorType.VIRTUAL_PARTNER_COMP, cycles[1].cycle.actor_type)

        total = calculate_total_score(cycles, cfg)
        # 환기 사이클 2개 평균 = 100. total 4로 나눈 50(버그)이 아니어야 한다.
        self.assertEqual(100, total["score_vent_vol"])
        self.assertEqual(100, total["score_vent_count"])
