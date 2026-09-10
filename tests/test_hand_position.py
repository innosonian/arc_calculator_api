# 원본: hstm_v2 tests/test_hand_position.py (동일 이식 — 가이드라인 비의존)
from unittest import TestCase

from calculators.action_evaluator import ActionEvaluator


class TestHandPosition(TestCase):
    def test_get_hand_position_spot(self):
        service = ActionEvaluator(None)
        for hand_position_list, expect in (
            ([1, 1, 1, 0, 0, 0, 0, 0, 0, 2, 1, 1, 1, 1, 1], "down"),
            ([1, 1, 1, 0, 0, 0, 0, 0, 0, 4, 1, 1, 1, 1, 1], "left"),
            ([1, 1, 1, 0, 0, 0, 0, 0, 0, 4, 8, 1, 1, 1, 1], "right"),
            ([1, 1, 1, 0, 0, 0, 0, 0, 0, 8, 1, 1, 1, 1, 1], "right"),
            ([1, 1, 1, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1], "center"),
        ):
            with self.subTest(case=hand_position_list, expect=expect):
                result = service.get_hand_position_spot(hand_position_list)
                self.assertEqual(expect, result)
