# 원본: hstm_v2 tests/test_chart_event_bars.py (동일 이식 — 가이드라인 비의존)
from unittest import TestCase

from config.enums import Actor
from data_handlers.chart_data import make_chart_data

# 깨끗한 1회 압박(상승→80→완전 이완), 3패킷(30샘플).
CLEAN_COMP = [0] * 10 + [0, 20, 40, 60, 80, 80, 60, 40, 20, 10] + [5, 2, 0, 0, 0, 0, 0, 0, 0, 0]
LAST_COMP_RISE = [0] * 10 + [0, 20, 40, 60, 80, 80, 76, 72, 68, 65]
FALL_TAIL = [62, 50, 30, 10, 0, 0, 0, 0, 0, 0]  # 경계로 잘린 하강 꼬리 윈도우
VENT_VOLUME = [0, 100, 400, 600, 600, 400, 100, 0, 0, 0]


class _FakeCycle:
    def __init__(self, cycle_num):
        self.cycle_num = cycle_num


class _FakeCycleWithScore:
    def __init__(self, cycle_num):
        self.cycle = _FakeCycle(cycle_num)


def _scores(cycle_nums):
    return [{"part_num": 1, "result": {"cycle_with_score_list": [_FakeCycleWithScore(c) for c in cycle_nums]}}]


def _action(depths, first_ts, action_type="comp", actor=Actor.REAL_PERSON, vent_volume=None):
    return {
        "action_type": action_type,
        "compression_depth": depths,
        "compression_count": [3],
        "ventilation_volume": vent_volume or [0],
        "first_timestamp": first_ts,
        "last_timestamp": first_ts + (len(depths) // 10) * 50,
        "part_num": 1,
        "cycle_cnt": 1,
        "overlap_type": None,
        "actor": actor,
    }


def _tail_cycle_actions(actor=Actor.REAL_PERSON):
    # 실데이터 구조: 깨끗한 압박 14회 + 마지막 압박(상승) + 하강 꼬리 윈도우(액션 16개).
    actions, ts = [], 0
    for _ in range(14):
        actions.append(_action(CLEAN_COMP, ts, actor=actor))
        ts += 150
    actions.append(_action(LAST_COMP_RISE, ts, actor=actor))
    ts += 100
    actions.append(_action(FALL_TAIL, ts, actor=actor))
    return actions


class TestCompressionEventBars(TestCase):
    """차트는 압박 바를 파형 이벤트 단위로 그린다(바 수 = 실제 압박 수, 1·2인구조 공통)."""

    def test_16_actions_draw_15_bars(self):
        chart = make_chart_data(_tail_cycle_actions(), [], _scores([1]))
        bars = chart["cpr_data_set"]
        self.assertEqual(15, len(bars))
        self.assertEqual(list(range(1, 16)), [b["comp_count"] for b in bars])
        self.assertEqual([80] * 15, [b["comp_depth_max"] for b in bars])
        timestamps = [b["timestamp"] for b in bars]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_one_rescuer_also_draws_event_bars(self):
        # Phase 3 통합: 1인구조 차트도 이벤트당 바 1개(부유 바 미표시).
        chart = make_chart_data(_tail_cycle_actions(), [], _scores([1]))
        self.assertEqual(15, len(chart["cpr_data_set"]))

    def test_virtual_partner_run_is_marked_virtual(self):
        chart = make_chart_data(_tail_cycle_actions(actor=Actor.VIRTUAL_PARTNER), [], _scores([1]))
        bars = chart["cpr_data_set"]
        self.assertEqual(15, len(bars))
        self.assertTrue(all(b["is_virtual_action"] for b in bars))

    def test_vent_action_flushes_run_and_keeps_order(self):
        actions = _tail_cycle_actions()
        vent_ts = actions[-1]["last_timestamp"] + 100
        actions.append(_action([0] * 10, vent_ts, action_type="vent", vent_volume=VENT_VOLUME))
        chart = make_chart_data(actions, [], _scores([1]))
        bars = chart["cpr_data_set"]
        self.assertEqual(16, len(bars))  # 압박 이벤트 15 + 환기 1
        self.assertEqual("vent", bars[-1]["action_type"])
        self.assertEqual(600, bars[-1]["vent_vol_max"])
