# 원본: hstm_v2 tests/test_cycle.py (동일 이식 — 가이드라인 비의존)
from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleMaker
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from config.enums import Actor
from models.action import ActionWithScore


class TestCycle(TestCase):
    def test_get_calc_case(self):
        cases = (
            (5, 1, "cpr"),
            (10, 0, "only_comp"),
            (10, 1, "cpr"),
            (30, 2, "cpr"),
        )
        for comp_count, vent_count, expected in cases:
            with self.subTest(comp_count=comp_count, vent_count=vent_count, expected=expected):
                actions = [
                    *[ActionWithScore(ACTION_TYPE_COMP, {}, {}, Actor.REAL_PERSON.value) for _ in range(comp_count)],
                    *[ActionWithScore(ACTION_TYPE_VENT, {}, {}, Actor.REAL_PERSON.value) for _ in range(vent_count)],
                ]

                self.assertEqual(expected, Cycle(actions, 1, True).get_calc_case())

    def test_make_cycles(self):
        action_with_scores = [
            ActionWithScore(ACTION_TYPE_COMP, {"cycle_cnt": 1}, {}, Actor.REAL_PERSON.value),
            ActionWithScore(ACTION_TYPE_VENT, {"cycle_cnt": 1}, {}, Actor.REAL_PERSON.value),
            ActionWithScore(ACTION_TYPE_COMP, {"cycle_cnt": 2}, {}, Actor.REAL_PERSON.value),
            ActionWithScore(ACTION_TYPE_VENT, {"cycle_cnt": 2}, {}, Actor.REAL_PERSON.value),
            ActionWithScore(ACTION_TYPE_COMP, {"cycle_cnt": 3}, {}, Actor.REAL_PERSON.value),
        ]

        cycles = CycleMaker.make_cycles(action_with_scores)

        self.assertFalse(cycles[0].is_last_cycle)
        self.assertFalse(cycles[1].is_last_cycle)
        self.assertTrue(cycles[2].is_last_cycle)
