"""Reference ventilation-rate numerator, divisor, and separate coaching signal."""
from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.merge_calculator import calculate_total_score
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT, CALC_CASE_ONLY_VENT
from config.enums import Actor
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config

CPR_COND = {
    "mode": "training", "target": "adult", "training_type": "cpr",
    "guideline": "ARC2020", "cpr_cycle_type": "302", "is_2rescuers": False,
}
GOOD = {"grade": 100, "criterion": "good"}


def _cfg_sw(cond=CPR_COND):
    return Config(cond), ScoreWeightFactory.create(cond["target"], cond["training_type"], cond["guideline"])


def _comp():
    data = {
        "action_type": ACTION_TYPE_COMP, "compression_count": 1, "ventilation_count": 0,
        "handsoff_ms": 0, "total_action_ms": 400, "compression_depth": [50] * 10,
    }
    return ActionWithScore(
        ACTION_TYPE_COMP, data, {k: GOOD for k in ("comp_depth", "comp_rate", "recoil", "hand_position")}, Actor.REAL_PERSON
    )


def _vent(rate_grade=0):
    data = {
        "action_type": ACTION_TYPE_VENT, "compression_count": 0, "ventilation_count": 2,
        "handsoff_ms": 0, "total_action_ms": 400, "compression_depth": [],
    }
    score = {"vent_vol": GOOD, "vent_speed": GOOD, "vent_rate": {"grade": rate_grade, "criterion": "x"}}
    return ActionWithScore(ACTION_TYPE_VENT, data, score, Actor.REAL_PERSON)


def _cpr_cycle(n_comp, vent_grades):
    cfg, sw = _cfg_sw()
    actions = [_comp() for _ in range(n_comp)] + [_vent(g) for g in vent_grades]
    cws = CycleWithScore(Cycle(actions, 1), 1, sw, cfg.calculation_config)
    cws.make_score(cfg.border)
    return cws


def test_cpr_measured_uses_cycle_rate_not_action_artifact():
    # 30압박(12초) + 환기 2회 = 사이클 12.8초 → 분당 9.4회 → good(100).
    # 액션 grade는 전부 0(아티팩트)이지만 measured는 사이클 단위 판정을 따른다.
    c = _cpr_cycle(30, [0, 0])
    assert c.score_vent_rate_measured == 100
    assert c.score_vent_rate == 0  # (0 + 0) / (2 - 1) = 0


def test_cpr_measured_low_when_over_ventilating():
    # 같은 사이클 길이에 환기 6회 → 분당 25회 → 과환기 0점. 실제 결함만 낮게 잡힌다.
    c = _cpr_cycle(30, [0] * 6)
    assert c.score_vent_rate_measured == 0


# Reference CPR: all rate grades in numerator, n - 1 in denominator.

def test_reference_two_perfect_vents_can_score_200():
    # Preserve the supplied formula: (100 + 100) / 1 = 200, without clamping.
    c = _cpr_cycle(30, [100, 100])
    assert c.score_vent_rate == 200


def test_b1_first_grade_zero_equals_legacy_formula():
    # 실데이터 패턴(첫 vent grade=0): 첫 액션 제외 합(100) == 전체 합(0+100) → 기존 산식과 동일값.
    c = _cpr_cycle(30, [0, 100])
    assert c.score_vent_rate == 100  # 신·구 산식 모두 100/(2-1)=100


def test_b1_denominator_stays_n_minus_1():
    # n=3: all three grades are divided by 2.
    assert _cpr_cycle(30, [0, 100, 50]).score_vent_rate == 75
    assert _cpr_cycle(30, [100, 0, 0]).score_vent_rate == 50


def test_b1_single_vent_guard_preserved():
    # The reference special-cases one ventilation to zero.
    assert _cpr_cycle(30, [100]).score_vent_rate == 0


def test_reference_vent_only_subsequent_cycle_includes_first_vent():
    """The first-vent exclusion only applies to the first only-vent cycle."""
    cond = dict(CPR_COND, training_type="ventilation_only")
    cfg, sw = _cfg_sw(cond)
    # cycle_num=2 (후속 사이클): 원본 스킵 조건(cycle_num==1)이 성립하지 않는 사이클.
    cws = CycleWithScore(Cycle([_vent(g) for g in (100, 0, 0)], 2), 1, sw, cfg.calculation_config)
    assert cws.calc_case == CALC_CASE_ONLY_VENT
    cws.make_score(cfg.border)
    # Subsequent cycle: (100 + 0 + 0) / 2 = 50.
    assert cws.score_vent_rate == 50
    # measured(vent-only 참평균 신호)도 같은 산식을 공유한다(원본 :406-407 계약 유지).
    assert cws.score_vent_rate_measured == 50


def test_vent_only_measured_is_true_average():
    # vent-only는 모든 환기가 유효하므로 measured == 기존 참평균(score_vent_rate).
    cond = dict(CPR_COND, training_type="ventilation_only")
    cfg, sw = _cfg_sw(cond)
    cws = CycleWithScore(Cycle([_vent(g) for g in (0, 100, 50)], 1), 1, sw, cfg.calculation_config)
    assert cws.calc_case == CALC_CASE_ONLY_VENT
    cws.make_score(cfg.border)
    # 첫 사이클 첫 환기는 rate 집계에서 제외(기존 로직 보존) → (100+50)/(3-1)=75
    assert cws.score_vent_rate == 75
    assert cws.score_vent_rate_measured == 75


def test_zero_vent_cpr_cycle_measured_none_and_no_crash():
    # 압박만 있고 환기 0회인 비마지막 CPR 사이클: 속도는 측정 불가(None, 횟수 결함으로만 코칭)이고
    # merge/overall의 None 덧셈 500도 나지 않아야 한다.
    cfg, sw = _cfg_sw()
    zero_vent = CycleWithScore(Cycle([_comp() for _ in range(30)], 1), 1, sw, cfg.calculation_config)
    normal = CycleWithScore(
        Cycle([_comp() for _ in range(30)] + [_vent(100), _vent(0)], 2, is_last_cycle=True), 1, sw, cfg.calculation_config
    )
    for c in (zero_vent, normal):
        c.make_score(cfg.border)
    assert zero_vent.score_vent_rate_measured is None
    result = calculate_total_score([zero_vent, normal], cfg)  # 크래시 없어야 함
    assert result["overall"] is not None
