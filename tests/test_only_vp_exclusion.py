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
    action_data = {"action_type": action_type, "compression_count": 15, "ventilation_count": 2,
                   "handsoff_ms": 0, "total_action_ms": 400}
    score = {k: GOOD for k in ("comp_depth", "comp_rate", "recoil", "hand_position",
                               "vent_vol", "vent_speed", "vent_rate")}
    return ActionWithScore(action_type, action_data, score, actor)


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
        cfg = Config({"mode": "training", "target": "adult", "training_type": "cpr",
                      "guideline": "AHA2020", "cpr_cycle_type": "302", "is_2rescuers": True})
        sw = ScoreWeightFactory.create("adult", "cpr", "AHA2020")

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
