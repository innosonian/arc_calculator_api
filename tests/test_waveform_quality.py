from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.metric_evaluator import MetricEvaluator
from config.constants import ACTION_TYPE_COMP
from config.enums import Actor
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config
from tests.test_compression_peaks import _scored

# adult AHA2020 기준: comp_depth border [40,50,60,70](digit) → raw 100~120이 good.
# 깨끗한 1회 압박(상승→110→완전 이완), 3패킷.
CLEAN_COMP = [0] * 10 + [0, 20, 40, 60, 110, 110, 60, 40, 20, 10] + [5, 2, 0, 0, 0, 0, 0, 0, 0, 0]
# 마지막 압박: 정점까지 오르고 하강이 시작된 채 윈도우가 끝남.
LAST_COMP_RISE = [0] * 10 + [0, 20, 40, 60, 110, 110, 104, 100, 96, 92]
# 경계가 어긋나 직전 압박의 하강 꼬리만 담긴 가짜 압박 윈도우(max 62 → 액션 기반이면 shallow).
FALL_TAIL = [62, 50, 30, 10, 0, 0, 0, 0, 0, 0]
# 불완전 이완 3회: 정점 100, 이완이 48까지만 복귀.
PARTIAL_RECOIL = ([55, 70, 85, 95, 100, 92, 80, 70, 60, 52] + [48, 50, 52, 55, 58, 60, 62, 64, 66, 68]) * 3

SW = ScoreWeightFactory.create("adult", "cpr", "AHA2020")


def _comp(depths, depth=(100, "good"), recoil=(100, "good"), actor=Actor.REAL_PERSON):
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
    return ActionWithScore(ACTION_TYPE_COMP, action_data, score, actor)


def _cfg(is_2rescuers):
    return Config(
        {
            "mode": "training",
            "target": "adult",
            "training_type": "cpr",
            "guideline": "AHA2020",
            "cpr_cycle_type": "302",
            "is_2rescuers": is_2rescuers,
        }
    )


def _cws(actions, is_2rescuers):
    return CycleWithScore(Cycle(actions, 1), 1, SW, _cfg(is_2rescuers).calculation_config)


def _tail_cycle_actions():
    # DEPTH_V1: 마지막 상승과 하강 꼬리가 한 action이므로 처음부터 15개다.
    return _scored([(CLEAN_COMP * 14 + LAST_COMP_RISE + FALL_TAIL, Actor.REAL_PERSON)])


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
        # 별도 꼬리 action이 없어졌으므로 fallback도 15개 압박의 100점 평균이다.
        self.assertEqual(100, cws.score_comp_depth)

    def test_incomplete_recoil_penalized_by_residual_depth(self):
        # 잔여 깊이 48(digit 24) → recoil border [5,10] 밖 → 0점.
        cws = _cws(_scored([(PARTIAL_RECOIL, Actor.REAL_PERSON)]), True)
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
        # 압박 액션 수도 사이클 순회에서 센다(액션 순회는 환기·시간 지표만).
        self.assertEqual(0, me.metric["CompressionActionNumber"])

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


class TestGlobalRecoilAcrossActors(TestCase):
    def test_rp_recoil_stops_at_next_vp_peak(self):
        # 승인된 §4: RP100 → 골50 → VP110 → 골10 → RP100.
        # 첫 RP의 recoil은 다음 VP 전의 50이며, 다음 RP 전까지의 10이 아니다.
        chunks = [([value] * 10, actor) for value, actor in [
            (0, Actor.REAL_PERSON), (100, Actor.REAL_PERSON), (50, Actor.REAL_PERSON),
            # VP plateau 두 Record로 [이전 확정, 이번 확정) 창의 VP 다수결을 보장한다.
            (110, Actor.VIRTUAL_PARTNER), (110, Actor.VIRTUAL_PARTNER), (10, Actor.VIRTUAL_PARTNER),
            (100, Actor.REAL_PERSON), (0, Actor.REAL_PERSON),
        ]]
        events = _cws(_scored(chunks), True)._compression_events()
        self.assertEqual([1, 6], [event.peak_packet_index for event in events])
        self.assertEqual([50, 0], [event.recoil_depth for event in events])
