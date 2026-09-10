# 원본: hstm_v2 tests/test_waveform_quality.py (ARC 각색 이식 — guideline만 ARC2020으로 교체.
# adult ARC2020 comp_depth border는 AHA2020과 동일([40,50,60,70] digit)이라 기대값 불변.)
from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.metric_evaluator import MetricEvaluator
from config.constants import ACTION_TYPE_COMP
from config.enums import Actor
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config

# adult ARC2020 기준: comp_depth border [40,50,60,70](digit) → raw 100~120이 good.
# 깨끗한 1회 압박(상승→110→완전 이완), 3패킷.
CLEAN_COMP = [0] * 10 + [0, 20, 40, 60, 110, 110, 60, 40, 20, 10] + [5, 2, 0, 0, 0, 0, 0, 0, 0, 0]
# 마지막 압박: 정점까지 오르고 하강이 시작된 채 윈도우가 끝남.
LAST_COMP_RISE = [0] * 10 + [0, 20, 40, 60, 110, 110, 104, 100, 96, 92]
# 경계가 어긋나 직전 압박의 하강 꼬리만 담긴 가짜 압박 윈도우(max 62 → 액션 기반이면 shallow).
FALL_TAIL = [62, 50, 30, 10, 0, 0, 0, 0, 0, 0]
# 불완전 이완 3회: 정점 100, 이완이 48까지만 복귀.
PARTIAL_RECOIL = ([55, 70, 85, 95, 100, 92, 80, 70, 60, 52] + [48, 50, 52, 55, 58, 60, 62, 64, 66, 68]) * 3

SW = ScoreWeightFactory.create("adult", "cpr", "ARC2020")


def _comp(depths, depth=(100, "good"), recoil=(100, "good")):
    action_data = {
        "action_type": ACTION_TYPE_COMP,
        "compression_depth": depths,
        "compression_count": 15,
        "ventilation_count": 0,
        "handsoff_ms": 0,
        "total_action_ms": 400,
    }
    score = {
        "comp_depth": {"grade": depth[0], "criterion": depth[1]},
        "recoil": {"grade": recoil[0], "criterion": recoil[1]},
        "comp_rate": {"grade": 100, "criterion": "good"},
        "hand_position": {"grade": 100, "criterion": "good"},
    }
    return ActionWithScore(ACTION_TYPE_COMP, action_data, score, Actor.REAL_PERSON)


def _cfg(is_2rescuers):
    return Config(
        {
            "mode": "training",
            "target": "adult",
            "training_type": "cpr",
            "guideline": "ARC2020",
            "cpr_cycle_type": "302",
            "is_2rescuers": is_2rescuers,
        }
    )


def _cws(actions, is_2rescuers):
    return CycleWithScore(Cycle(actions, 1), 1, SW, _cfg(is_2rescuers).calculation_config)


def _tail_cycle_actions():
    # 실데이터 구조: 깨끗한 압박 14회 + 마지막 압박(상승) + 하강 꼬리 윈도우.
    # 액션 기반이면 꼬리(max 62 → digit 31)가 shallow 압박 1회로 채점된다.
    actions = [_comp(CLEAN_COMP) for _ in range(14)]
    actions.append(_comp(LAST_COMP_RISE))
    actions.append(_comp(FALL_TAIL, depth=(0, "low")))
    return actions


class TestEventBasedDepthRecoil(TestCase):
    """depth/recoil은 파형 이벤트(정점/잔여 깊이)로 채점한다(1·2인구조 공통)."""

    def test_two_rescuer_fall_tail_not_scored_as_shallow(self):
        cws = _cws(_tail_cycle_actions(), True)
        cws._embed_comp_score(_cfg(True).border)
        self.assertEqual(100, cws.score_comp_depth)  # 꼬리(62)가 shallow로 평균을 깎지 않는다
        self.assertEqual(100, cws.score_recoil)
        self.assertEqual(15, len(cws.comp_event_scores))
        self.assertEqual(cws._get_comp_cnt(), len(cws.comp_event_scores))  # 분모 = 카운트

    def test_one_rescuer_also_event_based(self):
        # Phase 3 통합: 1인구조도 파형 이벤트 기반으로 채점한다.
        cws = _cws(_tail_cycle_actions(), False)
        cws._embed_comp_score(_cfg(False).border)
        self.assertEqual(100, cws.score_comp_depth)
        self.assertEqual(15, len(cws.comp_event_scores))
        self.assertEqual(cws._get_comp_cnt(), len(cws.comp_event_scores))

    def test_without_border_falls_back_to_action_based(self):
        cws = _cws(_tail_cycle_actions(), True)
        cws._embed_comp_score()
        self.assertEqual(94, cws.score_comp_depth)

    def test_incomplete_recoil_penalized_by_residual_depth(self):
        # 잔여 깊이 48(digit 24) → recoil border [5,10] 밖 → 0점.
        cws = _cws([_comp(PARTIAL_RECOIL)], True)
        cws._embed_comp_score(_cfg(True).border)
        self.assertEqual(3, len(cws.comp_event_scores))
        self.assertEqual(100, cws.score_comp_depth)  # 정점 100 → digit 50 good
        self.assertEqual(0, cws.score_recoil)

    def test_rate_and_hand_remain_action_based(self):
        cws = _cws(_tail_cycle_actions(), True)
        cws._embed_comp_score(_cfg(True).border)
        self.assertEqual(100, cws.score_comp_rate)
        self.assertEqual(100, cws.score_hand_position)


class TestEventBasedMetrics(TestCase):
    """CompressionDepth/Recoil 지표는 이벤트 판정으로 집계한다(1·2인구조 공통)."""

    def test_metrics_counted_from_events(self):
        cws = _cws(_tail_cycle_actions(), True)
        cws._embed_comp_score(_cfg(True).border)
        me = MetricEvaluator(_cfg(True).calculation_config)
        me.collect_cycle_metrics([cws])
        self.assertEqual(15, me.metric["CompressionDepth"]["n_Good"])
        self.assertEqual(0, me.metric["CompressionDepth"]["n_TooShallow"])  # 꼬리 미집계
        self.assertEqual(15, me.metric["Recoil"]["n_Good"])

    def test_two_rescuer_action_depth_not_double_counted(self):
        # 2인구조에선 액션 기반 depth/recoil 집계를 건너뛴다(이벤트로만 센다).
        me = MetricEvaluator(_cfg(True).calculation_config)
        me.collect_metrics([_comp(FALL_TAIL, depth=(0, "low"))])
        self.assertEqual(0, me.metric["CompressionDepth"]["n_TooShallow"])
        self.assertEqual(0, me.metric["CompressionDepth"]["n_Good"])
        self.assertEqual(1, me.metric["CompressionActionNumber"])

    def test_one_rescuer_metrics_counted_from_events(self):
        # Phase 3 통합: 1인구조도 depth/recoil 지표를 이벤트 판정으로 집계한다.
        cws = _cws(_tail_cycle_actions(), False)
        cws._embed_comp_score(_cfg(False).border)
        me = MetricEvaluator(_cfg(False).calculation_config)
        me.collect_cycle_metrics([cws])
        self.assertEqual(15, me.metric["CompressionDepth"]["n_Good"])
        self.assertEqual(0, me.metric["CompressionDepth"]["n_TooShallow"])  # 꼬리 미집계
        me2 = MetricEvaluator(_cfg(False).calculation_config)
        me2.collect_metrics([_comp(FALL_TAIL, depth=(0, "low"))])
        self.assertEqual(0, me2.metric["CompressionDepth"]["n_TooShallow"])  # 액션 기반 집계 제거


# 폭 3패킷(완전 이완까지 도달)의 압박과, 0 사이에 끼인 1패킷짜리 탭. 카운트 리스트는 패킷당 1개.
WIDE_COMP = [0, 0, 0, 0, 30, 60, 80, 100, 110, 110] + [110, 100, 80, 60, 50, 40, 35, 30, 28, 26] + [26, 20, 10, 5, 0, 0, 0, 0, 0, 0]
TAP_ACTION = [0] * 10 + [0, 0, 0, 28, 26, 6, 0, 0, 0, 0] + [0] * 10


def _comp_with_counts(depths, count_per_packet):
    action = _comp(depths)
    action.action_data["compression_count"] = count_per_packet
    return action


class TestCycleLevelTapFilter(TestCase):
    """사이클 레벨에서 혼합 폭(정상 압박 + 탭) 필터링이 카운트 수집과 함께 동작한다."""

    def _tap_cycle(self, n=8):
        # 정상 압박 n회(폭 3패킷, 카운트 +1) 사이마다 탭 1회(폭 1패킷, 카운트 유지).
        actions = []
        for k in range(1, n + 1):
            actions.append(_comp_with_counts(WIDE_COMP, [k - 1, k, k]))  # 압박 중 카운트 증가
            actions.append(_comp_with_counts(TAP_ACTION, [k, k, k]))  # 탭: 카운트 유지
        return actions

    def test_taps_filtered_in_cycle_scoring(self):
        cws = _cws(self._tap_cycle(8), False)
        events = cws._compression_events()
        # 이벤트 16개(압박 8 + 탭 8) 중 탭이 걸러져 8개만 남는다(분절 16 vs 카운트 증가 8 → 트리거).
        self.assertEqual(8, len(events))
        self.assertTrue(all(e.peak_depth >= 110 for e in events))  # 탭(28)은 전부 제거
        self.assertEqual(8, cws._get_comp_cnt())  # 카운트 = 필터 후 이벤트 수

    def test_small_tap_count_passes_through(self):
        # 알려진 한계: 탭이 트리거 최소치(5) 미만이면 보정하지 않는다.
        cws = _cws(self._tap_cycle(4), False)
        self.assertEqual(8, len(cws._compression_events()))  # 압박 4 + 탭 4 전부 유지
