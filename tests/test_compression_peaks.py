# 원본: hstm_v2 tests/test_compression_peaks.py (ARC 각색 이식 — guideline만 ARC2020으로 교체.
# COMP_PEAK_AMP 등 파형 상수·기대값은 원본 그대로. count_depth_peaks는 원본이 cycle_evaluator
# 재수출로 import했으나 §4.5(미사용 import 제거)에 따라 원 정의(calculators/waveform.py)에서 가져온다.)
from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.waveform import count_depth_peaks
from config.constants import ACTION_TYPE_COMP
from config.enums import Actor
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config

# 패킷별 max로 환산하면 [0, 80, 0]이 되는, 한 번의 압박(상승→마루→하강) 깊이 버퍼.
ONE_COMP_DEPTHS = [0] * 10 + [80] * 10 + [0] * 10
ZERO_DEPTHS = [0] * 30  # 경계 마커가 만든 가짜 압박(깊이≈0)


def _comp(depths, grade=100):
    action_data = {
        "action_type": ACTION_TYPE_COMP,
        "compression_depth": depths,
        "compression_count": 15,
        "ventilation_count": 0,
        "handsoff_ms": 0,
        "total_action_ms": 400,
    }
    g = {"grade": grade, "criterion": ""}
    score = {k: g for k in ("comp_depth", "comp_rate", "recoil", "hand_position")}
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


SW = ScoreWeightFactory.create("adult", "cpr", "ARC2020")


class TestCountDepthPeaks(TestCase):
    """깊이 파형(패킷별 max) → 진폭 기반 압박 횟수. 절대 임계가 아니라 진폭이라
    이완 정도·경계 마커·하강 edge 아티팩트에 강건해야 한다. (COMP_PEAK_AMP=25)"""

    def test_empty(self):
        self.assertEqual(0, count_depth_peaks([]))

    def test_flat_has_no_peak(self):
        self.assertEqual(0, count_depth_peaks([0, 0, 0, 0]))
        self.assertEqual(0, count_depth_peaks([5, 5, 5]))

    def test_single_compression(self):
        self.assertEqual(1, count_depth_peaks([0, 80, 0]))

    def test_fifteen_clean_compressions(self):
        self.assertEqual(15, count_depth_peaks([0, 80, 0] * 15))

    def test_boundary_marker_artifact_not_counted(self):
        # 15회 압박 뒤 깊이 0 마커가 붙어도(진폭 없음) 15회로 센다.
        self.assertEqual(15, count_depth_peaks([0, 80, 0] * 15 + [0, 0, 0]))

    def test_last_compression_without_full_fall_is_counted(self):
        # 마지막 압박이 마루까지만 오르고 하강이 잘려도 보정해 카운트.
        self.assertEqual(15, count_depth_peaks([0, 80, 0] * 14 + [0, 80]))

    def test_incomplete_recoil_not_merged(self):
        # 이완 불완전으로 깊이가 50 밑으로 안 떨어져도, 진폭(50)이 충분하면 분리된다.
        # (절대 임계 50 방식이라면 1회로 병합됐을 케이스)
        self.assertEqual(3, count_depth_peaks([50, 100, 50, 100, 50, 100, 50]))

    def test_amplitude_below_threshold_not_counted(self):
        # 진폭 20 < 25면 압박으로 세지 않는다(노이즈·미세 흔들림).
        self.assertEqual(0, count_depth_peaks([0, 20, 0, 20, 0]))

    def test_falling_edge_dip_does_not_split_one_compression(self):
        # 하강 중 잠깐(진폭 22<25) 끊긴 edge는 같은 압박으로 병합 → 1회.
        self.assertEqual(1, count_depth_peaks([0, 80, 58, 0]))


class TestCountCompressionPeaks(TestCase):
    """사이클의 압박 액션들로부터 패킷별 max 파형을 만들어 압박 수를 센다."""

    def _cws(self, actions, is_2rescuers=True):
        return CycleWithScore(Cycle(actions, 1), 1, SW, _cfg(is_2rescuers).calculation_config)

    def test_counts_real_compressions(self):
        cws = self._cws([_comp(ONE_COMP_DEPTHS) for _ in range(15)])
        self.assertEqual(15, cws._count_compression_peaks())

    def test_ignores_zero_depth_marker_action(self):
        # 15 실제 압박 + 1 가짜 압박(깊이 0) → 15
        cws = self._cws([_comp(ONE_COMP_DEPTHS) for _ in range(15)] + [_comp(ZERO_DEPTHS)])
        self.assertEqual(15, cws._count_compression_peaks())


class TestRealCompActions(TestCase):
    """품질(depth/recoil/rate/hand) 평균은 가짜 압박을 빼고 실제 압박만으로 낸다(1·2인구조 공통)."""

    def _cws(self, actions, is_2rescuers):
        return CycleWithScore(Cycle(actions, 1), 1, SW, _cfg(is_2rescuers).calculation_config)

    def test_two_rescuer_excludes_zero_depth_artifact(self):
        cws = self._cws([_comp(ONE_COMP_DEPTHS) for _ in range(15)] + [_comp(ZERO_DEPTHS)], True)
        self.assertEqual(15, len(cws._real_comp_actions()))

    def test_one_rescuer_also_excludes_zero_depth_artifact(self):
        # Phase 3 통합: 1인구조도 가짜 압박(깊이≈0 마커)을 품질 분모에서 제외한다.
        cws = self._cws([_comp(ONE_COMP_DEPTHS) for _ in range(15)] + [_comp(ZERO_DEPTHS)], False)
        self.assertEqual(15, len(cws._real_comp_actions()))

    def test_count_and_quality_denominator_agree(self):
        # count(peak)와 품질 분모(실제 압박 수)가 일치해야 한다.
        cws = self._cws([_comp(ONE_COMP_DEPTHS) for _ in range(15)] + [_comp(ZERO_DEPTHS)], True)
        self.assertEqual(cws._count_compression_peaks(), len(cws._real_comp_actions()))

    def test_comp_quality_not_diluted_by_artifact(self):
        # 실제 압박 15회(100점) + 가짜 압박 1회(0점). 보정 전이면 (15*100+0)/16=94로 희석되나,
        # 가짜 압박 제외 후 15회 평균이라 100이 유지돼야 한다.
        reals = [_comp(ONE_COMP_DEPTHS, grade=100) for _ in range(15)]
        artifact = _comp(ZERO_DEPTHS, grade=0)
        cws = self._cws(reals + [artifact], True)
        cws._embed_comp_score()
        self.assertEqual(100, cws.score_comp_depth)
        self.assertEqual(100, cws.score_recoil)
        self.assertEqual(100, cws.score_hand_position)

    def test_one_rescuer_quality_not_diluted(self):
        # Phase 3 통합: 1인구조도 가짜 압박이 평균을 희석하지 않는다((15*100+0)/16=94가 아니라 100).
        reals = [_comp(ONE_COMP_DEPTHS, grade=100) for _ in range(15)]
        artifact = _comp(ZERO_DEPTHS, grade=0)
        cws = self._cws(reals + [artifact], False)
        cws._embed_comp_score()
        self.assertEqual(100, cws.score_comp_depth)
