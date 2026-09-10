# 사용자 확정 예외: ARC2020/ARC2025 CPR의 연령별 최소횟수 null 정책.
# 예외 이외에는 참고 프로젝트의 계산·zero/null 계약을 유지한다:
#   - chest 필드 = score_comp_depth, score_recoil, score_comp_count(=score_comp_no), score_hand_position
#   - vent 필드 = score_vent_vol, score_vent_count, score_vent_rate, score_vent_speed
#   - 전용 훈련: 원본 merge_calculator의 total/part 조기 반환·_adj_field_value 유지
#   - ARC CPR: 성인·소아 압박 <90 / 영아 압박 <45 → chest null, 호흡 <6 → vent null (전 레벨)
#   - overall: null 필드 가중치 제외 후 잔여 가중치 재정규화, 양측 null이면 overall도 null
#   - CCF·comp_rate·AED는 정책 비대상(현행 유지)
import pytest

from calculators.cycle_evaluator import Cycle, CycleWithScore, NullPolicy
from config.calculation_config import AdultCalculationConfig
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from config.enums import Actor
from config.score_weight import ScoreWeightAdult
from main import run_calculator
from models.action import ActionWithScore
from services.config import Config
from tests._synth import comp_session, cpr_session, vo_session

CHEST_KEYS = ("score_comp_depth", "score_recoil", "score_comp_no", "score_comp_count", "score_hand_position")
VENT_KEYS = ("score_vent_vol", "score_vent_count", "score_vent_rate", "score_vent_speed")


def _condition(training_type="cpr", target="adult", guideline="ARC2025"):
    return {
        "mode": "training",
        "target": target,
        "training_type": training_type,
        "guideline": guideline,
        "cpr_cycle_type": "152" if target == "infant" else "302",
        "is_2rescuers": False,
    }


def _run(data, training_type="cpr", target="adult", guideline="ARC2025"):
    return run_calculator(data, b"", _condition(training_type, target, guideline), stage="test")


def _all_levels(result):
    """(total_score, [part score dict...], [cycle dict...]) 튜플."""
    total = result["cpr_score"]["total_score"]
    parts = [p["score"] for p in result["cpr_score"]["part_scores"]]
    cycles = [c for p in result["cpr_score"]["part_scores"] for c in p["cycle_with_score_list"]]
    return total, parts, cycles


class TestCompressionOnlyVentNull:
    def test_vent_fields_null_at_every_level(self):
        result = _run(comp_session(60), training_type="compression_only")
        total, parts, cycles = _all_levels(result)

        for key in VENT_KEYS:
            assert total[key] is None, (key, total[key])
        for score in parts:
            for key in VENT_KEYS:
                assert score.get(key) is None, (key, score)
        for cycle in cycles:
            for key in VENT_KEYS:
                assert cycle.get(key) is None, (key, cycle)

    def test_chest_fields_and_overall_stay_real(self):
        result = _run(comp_session(60), training_type="compression_only")
        total = result["cpr_score"]["total_score"]
        for key in ("score_comp_depth", "score_recoil", "score_hand_position"):
            assert total[key] is not None, key
        assert total["overall"] is not None
        assert 0 <= total["overall"] <= 100

    def test_ventilation_speed_metric_preserves_reference_zero_dict(self):
        # Reference compression-only metric keeps its initialized dictionary.
        result = _run(comp_session(60), training_type="compression_only")
        assert isinstance(result["metrics"]["VentilationSpeed"], dict)
        assert all(value == 0 for value in result["metrics"]["VentilationSpeed"].values())

    def test_rescue_vent_field_preserves_reference_total_zero_and_part_null(self):
        # 원본 merge_calculator: 정상 total은 score_rescue_vent=0을 직접 반환하고,
        # part는 _adj_field_value에서 rescue 대상이 아니면 None으로 바꾼다.
        result = _run(comp_session(60), training_type="compression_only")
        total, parts, _cycles = _all_levels(result)
        assert total["score_rescue_vent"] == 0
        assert type(total["score_rescue_vent"]) is int
        assert all(part["score_rescue_vent"] is None for part in parts)


class TestVentilationOnlyChestNull:
    def test_reference_total_and_cycle_nulls_and_part_zero_are_distinct(self):
        result = _run(vo_session(12), training_type="ventilation_only")
        total, parts, cycles = _all_levels(result)

        for key in CHEST_KEYS:
            assert total[key] is None, (key, total[key])
        # 현행(원본 _adj_field_value) 유지분: comp_rate·ccf도 vo에서는 null이다.
        assert total["score_comp_rate"] is None
        assert total["score_ccf"] is None
        for score in parts:
            # 원본 calculate_part_score의 comp_cycle_count=0/vent_cycle_count>0
            # 조기 반환은 모든 값이 int 0이며 score_comp_count 별칭은 없다.
            assert "score_comp_count" not in score
            assert all(type(value) is int and value == 0 for value in score.values())
        for cycle in cycles:
            for key in CHEST_KEYS:
                assert cycle.get(key) is None, (key, cycle)

    def test_vent_fields_and_overall_stay_real(self):
        result = _run(vo_session(12), training_type="ventilation_only")
        total = result["cpr_score"]["total_score"]
        assert total["score_vent_vol"] is not None
        assert total["score_vent_rate"] is not None
        assert total["overall"] is not None
        assert 0 <= total["overall"] <= 100


class TestCprMinimumAttemptNull:
    def test_low_comp_count_nulls_chest_at_every_level(self):
        # 성인 압박 30(<90) → chest null. 호흡 12(≥6) → vent 실점수.
        result = _run(cpr_session([(30, 12)]))
        total, parts, cycles = _all_levels(result)

        for key in CHEST_KEYS:
            assert total[key] is None, (key, total[key])
        for score in parts:
            for key in CHEST_KEYS:
                assert score.get(key) is None, (key, score)
        for cycle in cycles:
            for key in CHEST_KEYS:
                assert cycle.get(key) is None, (key, cycle)

        # vent 측은 실점수, overall은 잔여 가중치 재정규화로 산출(null 아님).
        assert total["score_vent_vol"] is not None
        assert total["score_vent_count"] is not None
        assert total["score_vent_rate"] is not None
        assert total["overall"] is not None
        assert 0 <= total["overall"] <= 100
        # comp_rate는 chest 매핑에 없으므로(스펙 §5.3) 현행 계산 유지.
        assert total["score_comp_rate"] is not None

    def test_low_vent_count_nulls_vent_at_every_level(self):
        # 성인 압박 90(≥90) → chest 실점수. 호흡 4(<6) → vent null.
        result = _run(cpr_session([(45, 2), (45, 2)]))
        total, parts, cycles = _all_levels(result)

        for key in VENT_KEYS:
            assert total[key] is None, (key, total[key])
        for score in parts:
            for key in VENT_KEYS:
                assert score.get(key) is None, (key, score)
        for cycle in cycles:
            for key in VENT_KEYS:
                assert cycle.get(key) is None, (key, cycle)

        for key in ("score_comp_depth", "score_recoil", "score_comp_no", "score_comp_count", "score_hand_position"):
            assert total[key] is not None, key
        assert total["overall"] is not None
        assert 0 <= total["overall"] <= 100

    def test_both_below_minimum_nulls_overall(self):
        # 성인 압박 30(<90) + 호흡 2(<6) → 양측 null → overall도 null.
        result = _run(cpr_session([(30, 2)]))
        total, _parts, _cycles = _all_levels(result)

        for key in CHEST_KEYS + VENT_KEYS:
            assert total[key] is None, key
        assert total["overall"] is None

    def test_enough_attempts_keep_all_fields_real(self):
        # 성인 압박 90·호흡 6의 경계값부터 최소량 null 정책이 발동하지 않는다.
        result = _run(cpr_session([(30, 2), (30, 2), (30, 2)]))
        total = result["cpr_score"]["total_score"]
        for key in CHEST_KEYS + ("score_vent_vol", "score_vent_count", "score_vent_rate", "score_comp_rate", "score_ccf"):
            assert total[key] is not None, key
        assert total["overall"] is not None

    def test_measured_signal_not_exposed_in_response(self):
        # 코칭 내부 신호(score_vent_rate_measured)는 응답에서 pop 유지(스펙 §5.3).
        result = _run(cpr_session([(30, 6), (30, 6)]))
        assert "score_vent_rate_measured" not in result["cpr_score"]["total_score"]


@pytest.mark.parametrize("target,comp_per_cycle", [("adult", 30), ("child", 30), ("infant", 15)])
@pytest.mark.parametrize("guideline", ["ARC2020", "ARC2025"])
@pytest.mark.parametrize("comp_delta,vent_delta", [(-1, -1), (-1, 0), (0, -1), (0, 0), (1, 1)])
def test_age_specific_minimum_counts_propagate_to_every_score_level(target, comp_per_cycle, guideline, comp_delta, vent_delta):
    # 사용자 확정 계약의 직전·정확한 경계·초과를 실제 바이너리 파싱부터 검증한다.
    cycles = [(comp_per_cycle, 2), (comp_per_cycle, 2), (comp_per_cycle + comp_delta, 2 + vent_delta)]
    result = _run(cpr_session(cycles), target=target, guideline=guideline)
    assert result["action_count"] == {"comp": comp_per_cycle * 3 + comp_delta, "vent": 6 + vent_delta}
    total, parts, cycle_scores = _all_levels(result)
    assert parts and cycle_scores
    for score in [total, *parts, *cycle_scores]:
        for key in CHEST_KEYS:
            assert (score[key] is None) == (comp_delta < 0), (target, key, score)
        for key in VENT_KEYS:
            if key == "score_vent_speed" and target != "infant":
                continue  # 성인·소아에서 이 지표는 원래 평가 대상이 아니다.
            assert (score[key] is None) == (vent_delta < 0), (target, key, score)
        assert (score["overall"] is None) == (comp_delta < 0 and vent_delta < 0)


@pytest.mark.parametrize("target", ["adult", "child", "infant"])
@pytest.mark.parametrize("training_type", ["compression_only", "ventilation_only"])
def test_cpr_minimum_counts_do_not_apply_to_single_skill_training(target, training_type):
    # CPR 최소량보다 적어도 total/cycle 평가 대상 점수는 유지한다.
    # part만의 원본 VO zero 조기 반환은 위 테스트에서 별도로 고정한다.
    data = comp_session(30) if training_type == "compression_only" else vo_session(4)
    total, parts, cycle_scores = _all_levels(_run(data, training_type=training_type, target=target))
    assert total["overall"] is not None
    assert parts and cycle_scores
    for score in [total, *cycle_scores]:
        assert (score["score_comp_depth"] is None) == (training_type == "ventilation_only")
        assert (score["score_vent_vol"] is None) == (training_type == "compression_only")
    for part in parts:
        if training_type == "ventilation_only":
            assert part["score_comp_depth"] == part["score_vent_vol"] == part["overall"] == 0
            assert "score_comp_count" not in part
        else:
            assert part["score_comp_depth"] is not None
            assert part["score_vent_vol"] is None


@pytest.mark.parametrize("target,minimum_comp_count", [("adult", 90), ("child", 90), ("infant", 45)])
@pytest.mark.parametrize("guideline", ["AHA2020", "ERC2020", "STD2015"])
@pytest.mark.parametrize("comp_delta,vent_delta", [(-1, -1), (-1, 0), (0, -1)])
def test_non_arc_low_count_cpr_preserves_reference_scoring(target, minimum_comp_count, guideline, comp_delta, vent_delta):
    # ARC의 양측/압박만/호흡만 미달 조건을 모두 다른 guideline에서 검사한다.
    # ERC 소아/영아는 초기 rescue 누락을 원본의 0점 사이클로 표현한다.
    comp_count, vent_count = minimum_comp_count + comp_delta, 6 + vent_delta
    config = Config(_condition(target=target, guideline=guideline))
    policy = NullPolicy.create(config.calculation_config, comp_count, vent_count)
    assert not policy.active
    total, parts, cycles = _all_levels(_run(cpr_session([(comp_count, vent_count)]), target=target, guideline=guideline))
    for score in [total, *parts]:
        for key in CHEST_KEYS + ("score_vent_vol", "score_vent_count", "score_vent_rate"):
            assert score[key] is not None, (guideline, target, key, score)
        assert score["overall"] is not None
    regular_cycles = [cycle for cycle in cycles if cycle["calc_case"] == "cpr"]
    assert regular_cycles
    for score in regular_cycles:
        assert score["score_comp_depth"] is not None
        assert score["score_vent_vol"] is not None
        assert score["overall"] is not None


class TestOverallRenormalization:
    """스펙 §5.3 overall 재정규화의 수치 검증(사이클 레벨 단위 테스트).

    스펙 유도 불변식: 비-null 전 필드가 100점이면 재정규화된 overall도 정확히 100이어야 한다.
    재정규화 누락(분모 1.0 고정)이나 잘못된 분모(엉뚱한 가중치 제외)는 100에서 벗어나 검출된다.
    part/total 레벨은 사이클 overall의 평균이므로(merge_calculator) 사이클 불변식이 곧 전 레벨을 덮는다.
    """

    @staticmethod
    def _cpr_cycle_with_score(null_policy: NullPolicy) -> CycleWithScore:
        # 압박+환기가 있는 비마지막 사이클 → calc_case CPR, actor ONLY_REAL_PERSON.
        actions = [
            ActionWithScore(ACTION_TYPE_COMP, {"total_action_ms": 400}, {}, Actor.REAL_PERSON)
            for _ in range(30)
        ] + [
            ActionWithScore(ACTION_TYPE_VENT, {"total_action_ms": 400}, {}, Actor.REAL_PERSON)
            for _ in range(2)
        ]
        config = AdultCalculationConfig("ARC2025", {"target": "adult", "training_type": "cpr"})
        return CycleWithScore(Cycle(actions, 1, is_last_cycle=False), 1, ScoreWeightAdult(), config, null_policy)

    @classmethod
    def _scored(cls, null_policy: NullPolicy, **overrides) -> CycleWithScore:
        cws = cls._cpr_cycle_with_score(null_policy)
        # 정책이 만든 상태를 그대로 재현: null 그룹은 None, 비-null 필드는 만점(100).
        scores = {
            "score_ccf": 100,
            "score_comp_rate": 100,
            "score_comp_depth": None if null_policy.chest_null else 100,
            "score_recoil": None if null_policy.chest_null else 100,
            "score_hand_position": None if null_policy.chest_null else 100,
            "score_comp_count": None if null_policy.chest_null else 100,
            "score_vent_vol": None if null_policy.vent_null else 100,
            "score_vent_rate": None if null_policy.vent_null else 100,
            "score_vent_count": None if null_policy.vent_null else 100,
            "score_vent_speed": None,  # adult: VENT_SPEED 가중치 0(비대상)
        }
        scores.update(overrides)
        for key, value in scores.items():
            setattr(cws, key, value)
        return cws

    def test_chest_null_all_100_gives_exactly_100(self):
        # chest null: 분자 = ccf(.25)+comp_rate(.15)+vent_vol(.10)+vent_count(.05) = 55,
        # 분모 = 1.0 - chest(.45) = .55 → 정확히 100. 재정규화 누락이면 55가 된다.
        cws = self._scored(NullPolicy(chest_null=True))
        assert cws.overall() == 100

    def test_vent_null_all_100_gives_exactly_100(self):
        # vent null: 분자 = ccf(.25)+comp_rate(.15)+chest(.45) = 85, 분모 = 1.0 - vent(.15) = .85
        # → 정확히 100. 재정규화 누락이면 85가 된다.
        cws = self._scored(NullPolicy(vent_null=True))
        assert cws.overall() == 100

    def test_chest_null_partial_scores_match_renormalized_weighted_sum(self):
        # 잘못된 분모 검출용 비균일 케이스: ccf=50, 나머지 100.
        # 분자 = 50*.25 + 100*(.15+.10+.05) = 42.5, 분모 = .55 → 77.27... → custom_round 77.
        cws = self._scored(NullPolicy(chest_null=True), score_ccf=50)
        assert cws.overall() == 77

    def test_vent_null_partial_scores_match_renormalized_weighted_sum(self):
        # 분자 = 50*.25 + 100*(.15+.45) = 72.5, 분모 = .85 → 85.29... → custom_round 85.
        cws = self._scored(NullPolicy(vent_null=True), score_ccf=50)
        assert cws.overall() == 85

    def test_e2e_total_overall_is_mean_of_renormalized_cycle_overalls(self):
        # 파이프라인 전 구간 검증: chest-null 세션의 total overall이 응답 사이클 overall
        # (재정규화된 값)의 평균과 일치한다 — 재정규화가 total 레벨까지 전파됨을 고정.
        from util.custom_math import custom_round

        result = _run(cpr_session([(30, 12)]))
        total, _parts, cycles = _all_levels(result)
        cycle_overalls = [c["overall"] for c in cycles if c["overall"] is not None]
        assert cycle_overalls, cycles
        assert total["overall"] == custom_round(sum(cycle_overalls) / len(cycle_overalls))
