# 원본: hstm_v2 tests/test_actor_type.py (동일 이식 — 가이드라인 비의존)
from unittest import TestCase

from calculators.cycle_evaluator import Cycle
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from config.enums import Actor, ActorType
from models.action import ActionWithScore


def _act(action_type, actor):
    return ActionWithScore(action_type, {}, {}, actor)


class TestGetActorType(TestCase):
    """사이클 actor_type을 첫·마지막 액션이 아니라 압박·환기를 각각 주로 수행한 행위자로 판정.

    전환 사이클에서 선행 depth=0 VP 압박 등으로 첫·마지막만 VP가 되어 사이클 전체가
    ONLY_VIRTUAL_PARTNER(0점)로 잘못 분류되던 문제를 막는다.
    """

    def test_transition_cycle_is_not_only_vp(self):
        # 첫=VP압박, 마지막=VP환기지만, 압박·환기 모두 실제 사용자가 다수
        actions = [
            _act(ACTION_TYPE_COMP, Actor.VIRTUAL_PARTNER),  # 선행 잔여 VP 압박
            *[_act(ACTION_TYPE_COMP, Actor.REAL_PERSON) for _ in range(14)],
            _act(ACTION_TYPE_VENT, Actor.REAL_PERSON),
            _act(ACTION_TYPE_VENT, Actor.VIRTUAL_PARTNER),  # 끝 VP 환기
        ]
        cycle = Cycle(actions, 1)
        # 과거 로직(첫/마지막)이면 ONLY_VIRTUAL_PARTNER → 0점이었음
        self.assertNotEqual(ActorType.ONLY_VIRTUAL_PARTNER, cycle.actor_type)
        self.assertEqual(ActorType.ONLY_REAL_PERSON, cycle.actor_type)

    def test_vp_comp_cycle(self):
        # VP가 압박(다수), 사용자가 환기 → VIRTUAL_PARTNER_COMP
        actions = [
            *[_act(ACTION_TYPE_COMP, Actor.VIRTUAL_PARTNER) for _ in range(15)],
            *[_act(ACTION_TYPE_VENT, Actor.REAL_PERSON) for _ in range(2)],
        ]
        self.assertEqual(ActorType.VIRTUAL_PARTNER_COMP, Cycle(actions, 1).actor_type)

    def test_vp_vent_cycle(self):
        # 사용자가 압박, VP가 환기(다수) → VIRTUAL_PARTNER_VENT
        actions = [
            *[_act(ACTION_TYPE_COMP, Actor.REAL_PERSON) for _ in range(15)],
            *[_act(ACTION_TYPE_VENT, Actor.VIRTUAL_PARTNER) for _ in range(2)],
        ]
        self.assertEqual(ActorType.VIRTUAL_PARTNER_VENT, Cycle(actions, 1).actor_type)

    def test_genuine_only_vp_still_detected(self):
        # 압박·환기 모두 VP 다수면 여전히 ONLY_VIRTUAL_PARTNER (0점 유지)
        actions = [
            *[_act(ACTION_TYPE_COMP, Actor.VIRTUAL_PARTNER) for _ in range(15)],
            *[_act(ACTION_TYPE_VENT, Actor.VIRTUAL_PARTNER) for _ in range(2)],
        ]
        self.assertEqual(ActorType.ONLY_VIRTUAL_PARTNER, Cycle(actions, 1).actor_type)

    def test_vp_comp_only_without_vent_is_only_vp(self):
        # VP가 압박만 하고 환기가 없으며 실제 사용자 동작이 전혀 없으면 ONLY_VP (0점 유지)
        actions = [_act(ACTION_TYPE_COMP, Actor.VIRTUAL_PARTNER) for _ in range(30)]
        self.assertEqual(ActorType.ONLY_VIRTUAL_PARTNER, Cycle(actions, 1).actor_type)

    def test_only_real_person(self):
        actions = [
            *[_act(ACTION_TYPE_COMP, Actor.REAL_PERSON) for _ in range(15)],
            *[_act(ACTION_TYPE_VENT, Actor.REAL_PERSON) for _ in range(2)],
        ]
        self.assertEqual(ActorType.ONLY_REAL_PERSON, Cycle(actions, 1).actor_type)
