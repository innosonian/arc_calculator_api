"""환기 '속도' 코칭 신호(score_vent_rate_measured)와 0회 환기 사이클 크래시 방지 검증.

핵심 사실:
- 액션 duration 환산(60000/소요ms)은 실제 환기(1~2초)가 30회/분 이상으로 환산되어
  첫 호흡을 포함해 거의 전부 0점이 되는 아티팩트다(실데이터 액션 grade 0 비율 89~98%).
- 따라서 CPR 코칭 속도(measured)는 사이클 단위 분당 환기율 판정(_calc_cycle_vent_rate)의
  grade를 쓴다. HSTM criteria 분포(ResultByCriteria.VentilationRate)와 같은 판정이다.
  vent-only는 모든 환기가 유효하므로 기존 참평균을 유지한다.
- HSTM/총점용 score_vent_rate(V1 패리티)는 이 변경으로 바뀌지 않는다.
"""
from calculators.cycle_evaluator import Cycle, CycleWithScore
from calculators.merge_calculator import calculate_total_score
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT, CALC_CASE_ONLY_VENT
from config.enums import Actor
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from services.config import Config

CPR_COND = {
    "mode": "training", "target": "adult", "training_type": "cpr",
    "guideline": "AHA2020", "cpr_cycle_type": "302", "is_2rescuers": False,
}
GOOD = {"grade": 100, "criterion": "good"}


def _cfg_sw(cond=CPR_COND):
    return Config(cond), ScoreWeightFactory.create(cond["target"], cond["training_type"], cond["guideline"])


def _comp():
    data = {
        "action_type": ACTION_TYPE_COMP, "compression_count": 1, "ventilation_count": 0,
        "handsoff_ms": 0, "total_action_ms": 400, "compression_depth": [50] * 10,
        # 환기 집계 단위 테스트의 확정 압박 메타데이터(분절은 상위 파이프라인의 책임).
        "peak_record": 0, "peak_offset": 0, "depth_raw": 50, "recoil_raw": 0,
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
    assert c.score_vent_rate == 0  # V1 패리티: (0+0)/(2-1)=0 그대로


def test_cpr_measured_low_when_over_ventilating():
    # 같은 사이클 길이에 환기 6회 → 분당 25회 → 과환기 0점. 실제 결함만 낮게 잡힌다.
    c = _cpr_cycle(30, [0] * 6)
    assert c.score_vent_rate_measured == 0


def test_cpr_v1_score_vent_rate_unchanged():
    # HSTM/총점용 액션 기반 집계는 한 글자도 바뀌지 않는다: 합/(N-1), N==1이면 0.
    assert _cpr_cycle(30, [100, 0]).score_vent_rate == 100
    assert _cpr_cycle(30, [100, 0, 0]).score_vent_rate == 50
    assert _cpr_cycle(30, [100]).score_vent_rate == 0


def test_vent_only_measured_is_true_average():
    # vent-only는 모든 환기가 유효하므로 measured == 기존 참평균(score_vent_rate).
    cond = dict(CPR_COND, training_type="ventilation_only")
    cfg, sw = _cfg_sw(cond)
    cws = CycleWithScore(Cycle([_vent(g) for g in (0, 100, 50)], 1), 1, sw, cfg.calculation_config)
    assert cws.calc_case == CALC_CASE_ONLY_VENT
    cws.make_score(cfg.border)
    # 첫 사이클 첫 환기는 rate 집계에서 제외(기존 로직) → (100+50)/(3-1)=75
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
