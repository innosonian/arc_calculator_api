"""HSTM legacy zero/null contracts and D139 displayed scores below minimum quantity."""
import pytest

from main import run_calculator
from services.config import Config
from tests._synth import comp_session, cpr_session, vo_session
from services.calculation_context import CalculationOptions
from tests.calculation_context_support import run_with_options

CURRENT_OPTIONS = CalculationOptions()

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


def _run(data, training_type="cpr", target="adult", guideline="ARC2025", options=None):
    """options=None: context 없는 직접 호출(현재 규칙). 옵션을 주면 그 어댑터가 실행하는 경로."""
    if options is None:
        return run_calculator(data, b"", _condition(training_type, target, guideline), stage="test")
    return run_with_options(data, b"", _condition(training_type, target, guideline), options)[0]


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




class TestCprMinimumNoLongerNullsByDefault:
    """D139: 현재 규칙(직접 호출·v6 context)은 최소량에 못 미쳐도 점수를 계산한다."""

    @pytest.mark.parametrize("options", [None, CURRENT_OPTIONS])
    @pytest.mark.parametrize("cycles", [[(30, 12)], [(45, 2), (45, 2)], [(30, 2)]])
    def test_below_minimum_sessions_keep_real_scores_at_every_level(self, cycles, options):
        result = _run(cpr_session(cycles), options=options)
        total, parts, cycle_scores = _all_levels(result)
        assert parts and cycle_scores
        for score in [total, *parts, *cycle_scores]:
            for key in CHEST_KEYS + ("score_vent_vol", "score_vent_count", "score_vent_rate"):
                assert score[key] is not None, (key, score)
            assert score["overall"] is not None and 0 <= score["overall"] <= 100

    def test_direct_call_and_current_options_are_the_same_result(self):
        data = cpr_session([(30, 2)])
        assert _run(data) == _run(data, options=CURRENT_OPTIONS)

    def test_both_below_minimum_session_scores_like_another_guideline(self):
        # 같은 입력을 최소량 정책이 원래 없던 STD2015로 계산한 점수와 같다(D139: "다른
        # 가이드라인과 같은 방식"). ARC2025와 STD2015의 성인 CPR 경계·가중치는 이 입력에서 같은 값을 낸다.
        arc = _run(cpr_session([(30, 2)]))["cpr_score"]["total_score"]
        other = _run(cpr_session([(30, 2)]), guideline="STD2015")["cpr_score"]["total_score"]
        assert arc["overall"] is not None
        assert {key: arc[key] for key in CHEST_KEYS + VENT_KEYS + ("overall",)} == \
            {key: other[key] for key in CHEST_KEYS + VENT_KEYS + ("overall",)}



@pytest.mark.parametrize("target,comp_per_cycle", [("adult", 30), ("child", 30), ("infant", 15)])
@pytest.mark.parametrize("guideline", ["ARC2020", "ARC2025"])
@pytest.mark.parametrize("comp_delta,vent_delta", [(-1, -1), (-1, 0), (0, -1), (0, 0), (1, 1)])
def test_current_rule_has_no_minimum_count_null_at_any_score_level(target, comp_per_cycle, guideline, comp_delta, vent_delta):
    # D139: 같은 최소량 경계 입력에서 어느 그룹도 null이 되지 않는다.
    cycles = [(comp_per_cycle, 2), (comp_per_cycle, 2), (comp_per_cycle + comp_delta, 2 + vent_delta)]
    result = _run(cpr_session(cycles), target=target, guideline=guideline)
    assert result["action_count"] == {"comp": comp_per_cycle * 3 + comp_delta, "vent": 6 + vent_delta}
    total, parts, cycle_scores = _all_levels(result)
    assert parts and cycle_scores
    for score in [total, *parts, *cycle_scores]:
        for key in CHEST_KEYS + VENT_KEYS:
            if key == "score_vent_speed" and target != "infant":
                continue  # 성인·소아에서 이 지표는 원래 평가 대상이 아니다.
            assert score[key] is not None, (target, key, score)
        assert score["overall"] is not None




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
