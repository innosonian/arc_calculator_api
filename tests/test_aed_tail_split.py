from pathlib import Path

import pytest

from config.constants import HANDSOFF_DEADTIME_MS, MINIMUM_COMP_DEPTH
from data_handlers.action_data import ActionDataPrepare
from data_handlers.chart_data import make_chart_data
from services.calculators import calculate_cpr
from services.http.schemas import ParsedData
from services.preparers import make_pre_action_list, parse_data, prepare_data
from tests.test_action_boundary_recovery import _config, _rt


def _stream(*, wait=6, rise=(12, 40, 80, 96)):
    records = [_rt(depth=depth) for depth in (40, 100, 40)]
    records += [_rt(count=1, depth=depth) for depth in (60, 20)]
    records += [_rt(count=1, depth=4, aed=True) for _ in range(20)]
    records += [_rt(count=1, depth=8) for _ in range(wait)]
    records += [_rt(count=1, depth=depth) for depth in rise]
    records += [_rt(count=2, depth=depth) for depth in (rise[-1], 80, 40, 8, 12, 40, 90)]
    records += [_rt(count=3, depth=depth) for depth in (90, 60, 20, 0)]
    for index, record in enumerate(records):
        record["timestamp"] = (index + 1) * 50
    return records


def _prepared(records):
    config = _config()
    actions, aed = make_pre_action_list(config, ParsedData(rtdata_list=records, aed_data_list=[]), [])
    return prepare_data(actions, aed, config)


def _second_cycle(prepared):
    return calculate_cpr(prepared, _config())["part_with_scores"][1]["result"]["cycle_with_score_list"][0]


def test_aed_tail_restores_first_compression_without_changing_counts():
    prepared = _prepared(_stream())
    actions = prepared["whole_cpr_action_list"]
    assert [a["action_type"] for a in actions] == ["comp", "aed", "comp", "comp"]
    assert len(prepared["prepared_cpr_data"]) == 2
    first = prepared["prepared_cpr_data"][1]["action_list"][0]
    assert max(first["compression_depth"]) == 96
    assert first["compression_depth"][0] >= MINIMUM_COMP_DEPTH
    assert actions[1]["compression_depth"][-1] < MINIMUM_COMP_DEPTH or actions[1]["is_aed_overlapped"][-1]
    assert prepared["comp_count"] == 3
    assert prepared["vent_count"] == 0
    cycle = _second_cycle(prepared)
    assert len(cycle._compression_events()) == cycle.cycle.get_comp_count() == 2
    assert cycle._compression_events()[0].peak_depth == 96
    assert all(a["handsoff_ms"] == 0 for a in prepared["prepared_cpr_data"][1]["action_list"])


def test_waiting_tail_keeps_cover_aed_boundary():
    actions = _prepared(_stream())["whole_cpr_action_list"]
    assert actions[1]["action_type"] == "aed"
    assert actions[1]["overlap_type"] == "action_cover_aed"


def test_tail_below_threshold_is_still_consumed_without_split():
    # DEPTH_V1은 Ccnt만 오른 8 raw 꼬리를 압박으로 만들지 않는다. AED 분할 자체의 하한을 검증한다.
    action = {"action_type": "comp", "ventilation_speed": 0,
              "rtdata_list": [_rt(depth=4, aed=True), _rt(depth=8), _rt(depth=8)]}
    assert ActionDataPrepare(_config())._split_aed_tail([action]) == [action]


def test_immediate_compression_keeps_partial_boundary():
    actions = _prepared(_stream(wait=0))["whole_cpr_action_list"]
    assert actions[1]["action_type"] == "aed"
    assert actions[1]["overlap_type"] == "partial_overlapped"


def test_equal_peaks_keep_the_entire_rise():
    prepared = _prepared(_stream(rise=(12, 40, 80, 112, 112)))
    first = prepared["prepared_cpr_data"][1]["action_list"][0]
    # 정점 동률은 앞 Record, 112 - 80 >= 30인 CONFIRM Record(80)는 다음 창의 선두다.
    assert first["compression_depth"][::10] == [12, 40, 80, 112, 112, 112]
    assert _second_cycle(prepared)._compression_events()[0].peak_depth == 112


def test_flat_section_before_rise_stays_in_aed_boundary():
    prepared = _prepared(_stream(rise=(12, 40, 40, 80, 96)))
    first = prepared["prepared_cpr_data"][1]["action_list"][0]
    # AED 상승 복원 시작은 그대로이며 96 - 40 >= 30인 확정 Record(40) 직전에서 끝난다.
    assert first["compression_depth"][::10] == [40, 80, 96, 96, 80]
    assert prepared["whole_cpr_action_list"][1]["compression_depth"][-1] == 40


@pytest.mark.parametrize("flags", [[False, True], [True, True], [False, False]])
def test_no_tail_or_no_overlap_leaves_action_unchanged(flags):
    action = {"action_type": "comp", "rtdata_list": [_rt(depth=80, aed=flag) for flag in flags],
              "ventilation_speed": 0}
    assert ActionDataPrepare(_config())._split_aed_tail([action]) == [action]


def test_ventilation_overlap_is_unchanged():
    action = {"action_type": "vent", "rtdata_list": [_rt(depth=80, vol=500, aed=flag) for flag in (True, False)],
              "ventilation_speed": 0}
    assert ActionDataPrepare(_config())._split_aed_tail([action]) == [action]


def test_two_aed_windows_are_split_independently():
    first, second = _stream(), _stream()
    for record in second:
        record["timestamp"] += first[-1]["timestamp"]
        record["compression_count"] += 3
    prepared = _prepared(first + second)
    assert len(prepared["prepared_cpr_data"]) == 3
    assert prepared["comp_count"] == 6
    assert sum(a["action_type"] == "aed" for a in prepared["whole_cpr_action_list"]) == 2


def test_aed_before_cpr_keeps_part_number_one():
    records = [_rt(depth=4, aed=True)] * 20 + [_rt(depth=d) for d in (8, 12, 40, 80, 96)]
    records += [_rt(count=1, depth=d) for d in (96, 80, 40, 8, 40, 90)]
    records += [_rt(count=2, depth=90)]
    records = [{**record, "timestamp": (index + 1) * 50} for index, record in enumerate(records)]
    prepared = _prepared(records)
    assert len(prepared["prepared_cpr_data"]) == 1
    assert prepared["prepared_cpr_data"][0]["part_num"] == 1
    assert [a["action_type"] for a in prepared["whole_cpr_action_list"]] == ["aed", "comp", "comp"]
    assert max(prepared["prepared_cpr_data"][0]["action_list"][0]["compression_depth"]) == 96
    assert prepared["comp_count"] == 2


def test_chart_skips_aed_boundary_action():
    prepared = _prepared(_stream())
    calculated = calculate_cpr(prepared, _config())
    chart = make_chart_data(prepared["whole_cpr_action_list"], [], calculated["part_with_scores"])
    assert [bar["action_type"] for bar in chart["cpr_data_set"]] == ["comp"] * 3
    assert [bar["comp_depth_max"] for bar in chart["cpr_data_set"]] == [100, 96, 90]


# R6 §4의 독립 기대값. 실행 결과나 result_*.json에서 만들지 않는다.
@pytest.mark.parametrize("number,count,peak,depth,hand,recoil,overall,total,comp_count", [
    (2, 30, 102, 100, 100, 100, 98, 96, 99),
    (3, 30, 110, 100, 97, 100, 99, 91, 100),
    (4, 30, 108, 100, 100, 92, 99, 82, 69),
    (5, 9, 108, 100, 100, 31, 74, 73, 36),
])
def test_real_aed_fixture_scores(number, count, peak, depth, hand, recoil, overall, total, comp_count):
    prepared = _fixture(number)
    calculated = calculate_cpr(prepared, _config())
    cycle = calculated["part_with_scores"][1]["result"]["cycle_with_score_list"][0]
    assert cycle.cycle.get_comp_count() == len(cycle._compression_events()) == count
    assert cycle._compression_events()[0].peak_depth == peak
    assert (cycle.score_comp_depth, cycle.score_comp_rate, cycle.score_hand_position,
            cycle.score_recoil, cycle.score_ccf, cycle.overall()) == (depth, 100, hand, recoil, 100, overall)
    assert calculated["cpr_total_scores"]["overall"] == total
    assert prepared["comp_count"] == comp_count


def _fixture(number):
    config = _config()
    parsed = parse_data(Path(f"tests/dataset/cpr_{number}.bin").read_bytes(),
                        Path(f"tests/dataset/aed_{number}.bin").read_bytes(), config)
    actions, aed = make_pre_action_list(config, parsed, [])
    return prepare_data(actions, aed, config)


def test_first_fixture_not_calc_and_total_stay_unchanged():
    prepared = _fixture(1)
    calculated = calculate_cpr(prepared, _config())
    cycle = calculated["part_with_scores"][1]["result"]["cycle_with_score_list"][0]
    assert cycle.cycle.get_comp_count() == 9
    assert cycle.calc_case == "not_calc"
    assert cycle.score_comp_depth is None
    assert calculated["cpr_total_scores"]["overall"] == 83
    assert prepared["comp_count"] == 48


@pytest.mark.parametrize("number", range(1, 6))
def test_recovered_fixture_time_excludes_wait_and_preserves_handsoff(number, monkeypatch):
    with monkeypatch.context() as original:
        original.setattr(ActionDataPrepare, "_split_aed_tail", lambda self, actions: actions)
        before = _second_cycle(_fixture(number))
    prepared = _fixture(number)
    after = _second_cycle(prepared)
    restored = prepared["prepared_cpr_data"][1]["action_list"][0]
    increase = after.total_action_ms - before.total_action_ms
    # 이제 정점 후 30 raw 하강까지 포함한다. 복원된 압박은 대기시간 없이 deadtime 안에 끝난다.
    assert 0 < increase < HANDSOFF_DEADTIME_MS
    assert increase == restored["total_action_ms"] < HANDSOFF_DEADTIME_MS
    assert restored["handsoff_ms"] == 0
    assert after.cycle.handsoff() == before.cycle.handsoff()
