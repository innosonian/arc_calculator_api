from unittest import TestCase

from config.enums import Actor
from data_handlers.chart_data import make_chart_data
from tests.test_compression_peaks import _actions

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


def _action(depths, first_ts, action_type="comp", actor=Actor.REAL_PERSON, vent_volume=None, count=None):
    return {
        "action_type": action_type,
        "compression_depth": depths,
        "compression_count": 3 if count is None else count,
        "ventilation_volume": vent_volume or [0],
        "first_timestamp": first_ts,
        "last_timestamp": first_ts + (len(depths) // 10) * 50,
        "part_num": 1,
        "cycle_cnt": 1,
        "overlap_type": None,
        "actor": actor,
    }


def _tail_cycle_actions(actor=Actor.REAL_PERSON):
    # DEPTH_V1: 하강 꼬리가 마지막 압박에 속하므로 원시 파형에서 15 action이 나온다.
    return _actions([(CLEAN_COMP * 14 + LAST_COMP_RISE + FALL_TAIL, actor)])


def _comp_bars(chart):
    return [b for b in chart["cpr_data_set"] if b["action_type"] == "comp"]


class TestCompressionEventBars(TestCase):
    """차트는 압박 바를 파형 이벤트 단위로 그린다(바 수 = 실제 압박 수, 1·2인구조 공통)."""

    def test_fall_tail_produces_15_actions_and_15_bars(self):
        actions = _tail_cycle_actions()
        self.assertEqual(15, len(actions))
        chart = make_chart_data(actions, [], _scores([1]))
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


class TestActorRuns(TestCase):
    """전체 스트림의 확정 압박을 actor 표시와 함께 한 번씩 그린다."""

    def test_mixed_cycle_preserves_each_actor_confirmations(self):
        # 전체 30개 CONFIRM 중 VP15/RP15. 하강 꼬리는 추가 action이 아니다.
        actions = _actions([(CLEAN_COMP * 15, Actor.VIRTUAL_PARTNER),
                            (CLEAN_COMP * 14 + LAST_COMP_RISE + FALL_TAIL, Actor.REAL_PERSON)])
        bars = _comp_bars(make_chart_data(actions, [], _scores([1])))
        vp_bars = [b for b in bars if b["is_virtual_action"]]
        rp_bars = [b for b in bars if not b["is_virtual_action"]]
        self.assertEqual(15, len(vp_bars))
        self.assertEqual(15, len(rp_bars))
        self.assertEqual(list(range(1, 16)), [b["comp_count"] for b in vp_bars])
        self.assertEqual(list(range(1, 16)), [b["comp_count"] for b in rp_bars])
        self.assertEqual(vp_bars + rp_bars, bars)  # 순서 유지: VP 막대 다음에 RP 막대

    def test_actor_switching_twice_draws_three_runs(self):
        # RP→VP→RP도 단일 분절. 표시 순번만 기존 actor 구간마다 1부터 센다.
        actions = _actions([(CLEAN_COMP * 5, Actor.REAL_PERSON),
                            (CLEAN_COMP * 5, Actor.VIRTUAL_PARTNER),
                            (CLEAN_COMP * 5, Actor.REAL_PERSON)])
        bars = _comp_bars(make_chart_data(actions, [], _scores([1])))
        self.assertEqual([False] * 5 + [True] * 5 + [False] * 5, [b["is_virtual_action"] for b in bars])
        self.assertEqual(list(range(1, 6)) * 3, [b["comp_count"] for b in bars])

    def test_recoil_uses_global_next_peak_across_actor_change(self):
        chunks = [([value] * 10, actor) for value, actor in [
            (0, Actor.REAL_PERSON), (100, Actor.REAL_PERSON), (50, Actor.REAL_PERSON),
            # VP plateau 두 Record로 [이전 확정, 이번 확정) 창의 VP 다수결을 보장한다.
            (110, Actor.VIRTUAL_PARTNER), (110, Actor.VIRTUAL_PARTNER), (10, Actor.VIRTUAL_PARTNER),
            (100, Actor.REAL_PERSON), (0, Actor.REAL_PERSON),
        ]]
        bars = _comp_bars(make_chart_data(_actions(chunks), [], _scores([1])))
        self.assertEqual([50, 10, 0], [bar["comp_depth_min"] for bar in bars])
        self.assertEqual([False, True, False], [bar["is_virtual_action"] for bar in bars])
