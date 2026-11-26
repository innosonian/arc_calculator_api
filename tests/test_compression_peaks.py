from unittest import TestCase

from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.waveform import segment_compressions
from calculators.metric_evaluator import MetricEvaluator
from config.constants import ACTION_TYPE_COMP
from config.enums import Actor
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from data_handlers.action_data import ActionDataPrepare
from tests.test_action_boundary_recovery import _rt
from services.config import Config

# 패킷별 max로 환산하면 [0, 80, 0]이 되는, 한 번의 압박(상승→마루→하강) 깊이 버퍼.
ONE_COMP_DEPTHS = [0] * 10 + [80] * 10 + [0] * 10
ZERO_DEPTHS = [0] * 30  # Ccnt 마커가 있어도 깊이 변화가 없으면 액션을 만들지 않는다.


def _comp(depths, grade=100, actor=Actor.REAL_PERSON):
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
    return ActionWithScore(ACTION_TYPE_COMP, action_data, score, actor)


def _cfg(is_2rescuers, cpr_cycle_type="302"):
    return Config(
        {
            "mode": "training",
            "target": "adult",
            "training_type": "cpr",
            "guideline": "AHA2020",
            "cpr_cycle_type": cpr_cycle_type,
            "is_2rescuers": is_2rescuers,
        }
    )


def _actions(chunks, config=None):
    """(깊이 샘플, actor) 조각을 한 스트림으로 분절한 실제 액션 픽스처."""
    records = []
    for depths, actor in chunks:
        for offset in range(0, len(depths), 10):
            record = _rt(ts=1000 + len(records) * 50)
            record.update(compression_depth=depths[offset:offset + 10], actor=actor)
            records.append(record)
    actions = ActionDataPrepare(config or _cfg(True)).get_action_list(records)
    for ordinal, action in enumerate(actions, 1):
        action.update(compression_count=ordinal, ventilation_count=0,
                      part_num=1, cycle_cnt=1, overlap_type=None)
    return actions


def _scored(chunks, config=None):
    # 품질 평균 테스트는 분절된 액션에 동일 점수를 주고 분모·actor 제외만 검증한다.
    return [ActionWithScore(ACTION_TYPE_COMP, action,
            {key: {"grade": 100, "criterion": ""} for key in ("comp_depth", "comp_rate", "recoil", "hand_position")},
            action["actor"]) for action in _actions(chunks, config)]


def _count(samples):
    return len(segment_compressions([[sample] for sample in samples]))


SW = ScoreWeightFactory.create("adult", "cpr", "AHA2020")


class TestCountDepthPeaks(TestCase):
    """깊이 파형(패킷별 max) → 진폭 기반 압박 횟수. 절대 임계가 아니라 진폭이라
    이완 정도·경계 마커·하강 edge 아티팩트에 강건해야 한다. (COMP_PEAK_AMP=30)"""

    def test_empty(self):
        self.assertEqual(0, _count([]))

    def test_flat_has_no_peak(self):
        self.assertEqual(0, _count([0, 0, 0, 0]))
        self.assertEqual(0, _count([5, 5, 5]))

    def test_single_compression(self):
        self.assertEqual(1, _count([0, 80, 0]))

    def test_fifteen_clean_compressions(self):
        self.assertEqual(15, _count([0, 80, 0] * 15))

    def test_boundary_marker_artifact_not_counted(self):
        # 15회 압박 뒤 깊이 0 마커가 붙어도(진폭 없음) 15회로 센다.
        self.assertEqual(15, _count([0, 80, 0] * 15 + [0, 0, 0]))

    def test_last_compression_without_full_fall_is_counted(self):
        # 마지막 압박이 마루까지만 오르고 하강이 잘려도 보정해 카운트.
        self.assertEqual(15, _count([0, 80, 0] * 14 + [0, 80]))

    def test_incomplete_recoil_not_merged(self):
        # 이완 불완전으로 깊이가 50 밑으로 안 떨어져도, 진폭(50)이 충분하면 분리된다.
        # (절대 임계 50 방식이라면 1회로 병합됐을 케이스)
        self.assertEqual(3, _count([50, 100, 50, 100, 50, 100, 50]))

    def test_amplitude_below_threshold_not_counted(self):
        # 진폭 20 < 30면 압박으로 세지 않는다(노이즈·미세 흔들림).
        self.assertEqual(0, _count([0, 20, 0, 20, 0]))

    def test_falling_edge_dip_does_not_split_one_compression(self):
        # 하강 중 잠깐(진폭 22<30) 끊긴 edge는 같은 압박으로 병합 → 1회.
        self.assertEqual(1, _count([0, 80, 58, 0]))


class TestCountCompressionPeaks(TestCase):
    """전체 스트림에서 확정된 압박 액션과 사이클 이벤트는 같은 횟수다."""

    def _cws(self, actions, is_2rescuers=True):
        return CycleWithScore(Cycle(actions, 1), 1, SW, _cfg(is_2rescuers).calculation_config)

    def test_counts_real_compressions(self):
        cws = self._cws(_scored([(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 15))
        self.assertEqual(15, cws._count_compression_peaks())

    def test_ignores_zero_depth_marker_action(self):
        # DEPTH_V1: 15 실제 압박 뒤 깊이 0 마커는 액션 단계에서 제거되어 15개다.
        cws = self._cws(_scored([(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 15 + [(ZERO_DEPTHS, Actor.REAL_PERSON)]))
        self.assertEqual(15, cws._count_compression_peaks())


class TestRealCompActions(TestCase):
    """품질(depth/recoil/rate/hand) 평균은 가짜 압박을 빼고 실제 압박만으로 낸다(1·2인구조 공통)."""

    def _cws(self, actions, is_2rescuers):
        return CycleWithScore(Cycle(actions, 1), 1, SW, _cfg(is_2rescuers).calculation_config)

    def test_two_rescuer_excludes_zero_depth_artifact(self):
        cws = self._cws(_scored([(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 15 + [(ZERO_DEPTHS, Actor.REAL_PERSON)]), True)
        self.assertEqual(15, len(cws._real_comp_actions()))

    def test_one_rescuer_also_excludes_zero_depth_artifact(self):
        # Phase 3 통합: 1인구조도 가짜 압박(깊이≈0 마커)을 품질 분모에서 제외한다.
        cws = self._cws(_scored([(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 15 + [(ZERO_DEPTHS, Actor.REAL_PERSON)]), False)
        self.assertEqual(15, len(cws._real_comp_actions()))

    def test_count_and_quality_denominator_agree(self):
        # count(peak)와 품질 분모(실제 압박 수)가 일치해야 한다.
        cws = self._cws(_scored([(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 15 + [(ZERO_DEPTHS, Actor.REAL_PERSON)]), True)
        self.assertEqual(cws._count_compression_peaks(), len(cws._real_comp_actions()))

    def test_comp_quality_not_diluted_by_artifact(self):
        # 실제 압박 15회(100점) + 가짜 압박 1회(0점). 보정 전이면 (15*100+0)/16=94로 희석되나,
        # 가짜 압박 제외 후 15회 평균이라 100이 유지돼야 한다.
        actions = _scored([(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 15 + [(ZERO_DEPTHS, Actor.REAL_PERSON)])
        cws = self._cws(actions, True)
        cws._embed_comp_score()
        self.assertEqual(100, cws.score_comp_depth)
        self.assertEqual(100, cws.score_recoil)
        self.assertEqual(100, cws.score_hand_position)

    def test_one_rescuer_quality_not_diluted(self):
        # Phase 3 통합: 1인구조도 가짜 압박이 평균을 희석하지 않는다((15*100+0)/16=94가 아니라 100).
        actions = _scored([(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 15 + [(ZERO_DEPTHS, Actor.REAL_PERSON)])
        cws = self._cws(actions, False)
        cws._embed_comp_score()
        self.assertEqual(100, cws.score_comp_depth)


# VP 압박 파형: 잔여 깊이 48에서 정점 110까지 누르고 48로 복귀(depth digit 55 good, recoil digit 24 bad).
VP_DEEP_INCOMPLETE = [48] * 10 + [110] * 10 + [48] * 10


class TestRpOnlyCompScoring(TestCase):
    """채점 사이클에 VP 압박이 섞여도 압박 채점은 실제 사용자(RP) 압박만 대상으로 한다."""

    def _cws(self, actions, cfg=None):
        cfg = cfg or _cfg(True)
        return CycleWithScore(Cycle(actions, 1), 1, SW, cfg.calculation_config)

    def _handover_actions(self):
        # 역할 교대도 한 번만 분절한다. 마지막 깊이 0 마커는 압박 액션이 아니다.
        return _scored([(ONE_COMP_DEPTHS, Actor.VIRTUAL_PARTNER)] * 15
                       + [(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 15
                       + [(ZERO_DEPTHS, Actor.REAL_PERSON)])

    def test_waveform_input_excludes_vp_compressions(self):
        self.assertEqual(15, self._cws(self._handover_actions())._count_compression_peaks())

    def test_handover_cycle_comp_no_is_not_too_many(self):
        # 15:2 기준. VP 압박까지 세면 30회로 Too Many가 된다.
        cfg = _cfg(True, cpr_cycle_type="152")
        cws = self._cws(self._handover_actions(), cfg).make_score(cfg.border)
        self.assertEqual(15, len(cws.comp_event_scores))
        self.assertEqual(15, cws._get_comp_cnt())
        self.assertEqual("good", cws.evaluate_result_comp_no["criterion"])

    def _vp_deep_then_user_actions(self):
        # VP 압박 15회(depth good, recoil bad) 다음에 사용자 압박 15회(정점 80: depth low, 완전 이완: recoil good).
        return _scored([(VP_DEEP_INCOMPLETE, Actor.VIRTUAL_PARTNER)] * 15
                       + [(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 15)

    def test_vp_compressions_excluded_from_user_depth_recoil(self):
        # VP 압박이 사용자 depth 평균을 올리거나 recoil 평균을 내리지 않는다.
        cfg = _cfg(True)
        cws = self._cws(self._vp_deep_then_user_actions(), cfg)
        cws._embed_comp_score(cfg.border)
        self.assertEqual(15, len(cws.comp_event_scores))
        self.assertEqual(0, cws.score_comp_depth)
        self.assertEqual(100, cws.score_recoil)

    def test_depth_recoil_metrics_count_rp_events_only(self):
        # depth/recoil 지표도 채점에 사용한 RP 압박 이벤트만 센다.
        cfg = _cfg(True)
        cws = self._cws(self._vp_deep_then_user_actions(), cfg)
        cws._embed_comp_score(cfg.border)
        me = MetricEvaluator(cfg.calculation_config)
        me.collect_cycle_metrics([cws])
        self.assertEqual(15, me.metric["CompressionDepth"]["n_TooShallow"])
        self.assertEqual(0, me.metric["CompressionDepth"]["n_Good"])
        self.assertEqual(15, me.metric["Recoil"]["n_Good"])
        self.assertEqual(0, me.metric["Recoil"]["n_Incomplete"])

    def test_rate_hand_average_excludes_vp_actions(self):
        actions = _scored([(ONE_COMP_DEPTHS, Actor.REAL_PERSON)] * 10
                          + [(ONE_COMP_DEPTHS, Actor.VIRTUAL_PARTNER)] * 2)
        reals, vps = actions[:10], actions[10:]
        for action in vps:
            for score in action.score.values():
                score["grade"] = 0
        cws = self._cws(actions)
        self.assertEqual(reals, cws._real_comp_actions())
        cws._embed_comp_score()
        self.assertEqual(100, cws.score_hand_position)
        self.assertEqual(100, cws.score_comp_rate)

    def test_fallback_to_all_rp_actions(self):
        # RP 압박이 전부 경계 마커(깊이≈0)여도 VP 압박으로 후퇴하지 않고 RP 압박 전체를 사용한다.
        rps = [_comp(ZERO_DEPTHS) for _ in range(2)]
        vps = [_comp(ONE_COMP_DEPTHS, actor=Actor.VIRTUAL_PARTNER) for _ in range(3)]
        self.assertEqual(rps, self._cws(rps + vps)._real_comp_actions())

    def test_fallback_to_all_actions(self):
        # RP 압박이 없으면 전체 압박으로 후퇴해 분모 0을 방지한다.
        vps = [_comp(ONE_COMP_DEPTHS, actor=Actor.VIRTUAL_PARTNER) for _ in range(3)]
        self.assertEqual(vps, self._cws(vps)._real_comp_actions())
