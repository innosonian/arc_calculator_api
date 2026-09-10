# 원본: hstm_v2 tests/test_action_actor.py (ARC 각색 이식 — guideline만 ARC2025로 교체)
from unittest import TestCase

from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from config.enums import Actor
from data_handlers.action_data import ActionDataPrepare
from services.config import Config

VP = Actor.VIRTUAL_PARTNER
RP = Actor.REAL_PERSON


class TestActionActor(TestCase):
    """액션 actor를 '실제 압박/환기가 일어난(신호 있는) 패킷'의 VP 멤버십으로 확정.

    VP 마킹이 시간범위 단위라 액션 버퍼가 VP 경계에 걸쳐 idle/전환 패킷이 섞여도,
    실제 동작이 일어난 패킷의 주체로 귀속되어야 한다.
    packet_signal 항목 = (actor, max_compression_depth, max_ventilation_volume)
    """

    def setUp(self):
        self.prep = ActionDataPrepare(Config({
            "mode": "training", "target": "adult", "training_type": "cpr",
            "guideline": "ARC2025", "cpr_cycle_type": "302", "is_2rescuers": True,
        }))

    def test_comp_actor_is_signal_owner_not_idle(self):
        # 실제 압박(depth 큼)은 사용자, 앞뒤 idle(depth 0)은 VP → 사용자
        sig = [(VP, 0, 0), (RP, 80, 0), (RP, 75, 0), (VP, 0, 0)]
        self.assertEqual(RP, self.prep._primary_actor(sig, ACTION_TYPE_COMP))

    def test_comp_actor_vp_when_signal_is_vp(self):
        sig = [(VP, 80, 0), (VP, 75, 0), (RP, 0, 0)]
        self.assertEqual(VP, self.prep._primary_actor(sig, ACTION_TYPE_COMP))

    def test_vent_actor_is_signal_owner(self):
        sig = [(VP, 0, 0), (RP, 0, 40), (RP, 0, 35)]
        self.assertEqual(RP, self.prep._primary_actor(sig, ACTION_TYPE_VENT))

    def test_comp_ignores_ventilation_signal(self):
        # 압박 액션은 환기 신호(vol)가 아니라 압박 신호(depth)만 본다
        sig = [(VP, 0, 40), (RP, 80, 0)]
        self.assertEqual(RP, self.prep._primary_actor(sig, ACTION_TYPE_COMP))

    def test_fallback_to_all_packets_when_no_signal(self):
        # 신호 없는 액션(idle 등)은 전체 패킷 다수로 폴백
        sig = [(VP, 0, 0), (VP, 0, 0), (RP, 0, 0)]
        self.assertEqual(VP, self.prep._primary_actor(sig, ACTION_TYPE_COMP))
