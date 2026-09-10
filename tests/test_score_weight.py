# 원본: hstm_v2 tests/test_score_weight.py (ARC 각색 이식 — rescue vent 미이식(스펙 §4.3)으로
# ScoreWeightInfantWithRescueVent 케이스 제거. 잔존 클래스(Adult/Infant) 계약은 원본 그대로.)
from unittest import TestCase

from config.score_weight import (
    ScoreWeightAdult,
    ScoreWeightInfant,
)


class TestGetVpCompWeight(TestCase):
    """VP:COMP 사이클(사용자 환기) 분모에 vent_speed 누락 회귀 방지.

    분자(_overall_vent)는 infant일 때 vent_speed * VENT_SPEED를 가산하므로,
    분모(get_vp_comp_weight)도 include_vent_speed=True일 때 VENT_SPEED를 포함해야 한다.
    """

    def test_infant_includes_vent_speed_when_requested(self):
        weight = ScoreWeightInfant()
        # VENT_VOL(0.26) + VENT_COUNT(0.10) + CCF(0.10) + VENT_SPEED(0.09)
        self.assertAlmostEqual(55.0, weight.get_vp_comp_weight(include_vent_speed=True))

    def test_infant_excludes_vent_speed_by_default(self):
        weight = ScoreWeightInfant()
        # 기본값(False)은 기존 동작 유지: VENT_VOL + VENT_COUNT + CCF
        self.assertAlmostEqual(46.0, weight.get_vp_comp_weight())
        self.assertAlmostEqual(46.0, weight.get_vp_comp_weight(include_vent_speed=False))

    def test_infant_denominator_grows_with_vent_speed(self):
        weight = ScoreWeightInfant()
        # vent_speed 포함 시 분모가 더 커져 환기 사이클 점수 과대평가가 사라진다.
        self.assertGreater(
            weight.get_vp_comp_weight(include_vent_speed=True),
            weight.get_vp_comp_weight(include_vent_speed=False),
        )

    def test_adult_unaffected_because_vent_speed_weight_is_zero(self):
        weight = ScoreWeightAdult()
        # adult은 VENT_SPEED=0이라 include 여부와 무관하게 동일해야 한다(회귀 없음).
        self.assertAlmostEqual(
            weight.get_vp_comp_weight(include_vent_speed=False),
            weight.get_vp_comp_weight(include_vent_speed=True),
        )
