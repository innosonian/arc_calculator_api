"""DEPTH_V1의 경계·시간 좌표·환기 동시 경계 통합 계약."""
from copy import deepcopy

import pytest

from calculators.action_evaluator import ActionEvaluator
from config.enums import Actor
from data_handlers.action_data import ActionDataPrepare
from services.preparers import prepare_data
from tests.test_action_boundary_recovery import _config, _rt


def _records(depths):
    return [_rt(depth=depth, ts=1000 + i * 50) for i, depth in enumerate(depths)]


def test_nominal_peak_sample_rate_ignores_firmware_and_timestamp_jitter():
    config = _config()
    records = _records([0, 100, 60, 0, 0, 100, 60, 0])
    records[1]["compression_depth"] = [0] * 9 + [100]
    records[5]["compression_depth"] = [0, 100] + [0] * 8
    for record, timestamp in zip(records, [1000, 1001, 1100, 5000, 5001, 5100, 9000, 9001]):
        record.update(timestamp=timestamp, compression_rate=255, compression_count=255)
    actions = ActionDataPrepare(config).get_action_list(records)
    scored = ActionEvaluator(config).evaluate_action(actions)
    # (51 - 19) × 5 = 160 ms, 60000/160 = 375. 실제 시각 차이나 byte 11은 사용하지 않는다.
    assert [a.score["comp_rate"]["value"] for a in scored] == [0, 375]
    assert [(a["action_start"], a["action_end"]) for a in actions] == [(0, 2), (2, 6)]


def test_ventilation_starts_a_new_rate_cycle():
    config = _config()
    records = _records([0, 100, 60, 0, 0, 0, 0, 100, 60])
    for i in (3, 4):
        records[i]["ventilation_volume"] = [40, 40]
    actions = ActionDataPrepare(config).get_action_list(records)
    assert [a["action_type"] for a in actions] == ["comp", "vent", "comp"]
    prepared = prepare_data(actions, [], config)
    actions = prepared["prepared_cpr_data"][0]["action_list"]
    assert [a["cycle_cnt"] for a in actions] == [1, 1, 2]
    assert [a.score["comp_rate"]["value"] for a in ActionEvaluator(config).evaluate_action(actions)
            if a.action_type == "comp"] == [0, 0]


def test_aed_part_boundary_resets_previous_peak():
    config = _config()
    actions = ActionDataPrepare(config).get_action_list(_records([0, 100, 60, 0, 100, 60, 0, 100, 60]))
    # 가운데 압박은 기존 AED 제외 정책이 소비한다. 다음 파트의 첫 압박 속도는 0이다.
    actions[1]["is_aed_overlapped"] = [True] * len(actions[1]["is_aed_overlapped"])
    prepared = prepare_data(actions, [], config)
    second = prepared["prepared_cpr_data"][1]["action_list"][0]
    assert second["prev_peak_sample"] is None
    assert ActionEvaluator(config).evaluate_action([second])[0].score["comp_rate"]["value"] == 0


@pytest.mark.parametrize("actor", [Actor.REAL_PERSON, Actor.VIRTUAL_PARTNER])
def test_same_record_ventilation_and_compression_both_survive(actor):
    from data_handlers.chart_data import make_chart_data
    from services.calculators import calculate_cpr

    config = _config()
    records = _records([0, 100, 60])
    for record in records:
        record["actor"] = actor
    records[1]["ventilation_volume"] = [40, 40]
    actions = ActionDataPrepare(config).get_action_list(records)
    assert [a["action_type"] for a in actions] == ["vent", "comp"]
    assert (actions[0]["action_start"], actions[0]["action_end"]) == (0, 2)
    assert (actions[1]["action_start"], actions[1]["action_end"]) == (2, 2)
    assert actions[1]["peak_record"] == 1
    assert actions[1]["depth_raw"] == 100
    assert actions[1]["first_timestamp"] == actions[1]["last_timestamp"] == records[2]["timestamp"]
    assert actions[1]["signal_record"] == 1
    assert actions[1]["is_aed_overlapped"] == [records[2]["is_aed_overlapped"]]
    assert actions[1]["hand_position"] == []
    assert actions[1]["actor"] == actor
    assert actions[1]["total_action_ms"] == actions[1]["handsoff_ms"] == 0
    assert sum(action["total_action_ms"] for action in actions) == actions[0]["total_action_ms"] == 50
    prepared = prepare_data(actions, [], config)
    calculated = calculate_cpr(prepared, config)
    chart = make_chart_data(prepared["whole_cpr_action_list"], [], calculated["part_with_scores"])
    assert prepared["comp_count"] == 1
    bars = [bar for bar in chart["cpr_data_set"] if bar["action_type"] == "comp"]
    assert len(bars) == 1
    assert bars[0]["comp_depth_max"] == 100
    assert bars[0]["is_virtual_action"] == (actor == Actor.VIRTUAL_PARTNER)


def test_same_record_empty_comp_keeps_signal_majority_instead_of_peak_actor():
    records = _records([0, 60, 100, 80, 70])
    records[2]["actor"] = Actor.VIRTUAL_PARTNER  # 정점은 VP지만 신호 창은 RP 2 : VP 1이다.
    for record in records[1:4]:
        record["ventilation_volume"] = [40, 40]
    actions = ActionDataPrepare(_config()).get_action_list(records)
    comp = actions[1]
    assert (comp["action_start"], comp["action_end"], comp["total_action_ms"]) == (4, 4, 0)
    assert comp["hand_position"] == []
    assert comp["peak_record"] == 2
    assert comp["actor"] == Actor.REAL_PERSON


@pytest.mark.parametrize("end,count", [(29, 0), (30, 1)])
def test_finish_counts_exact_rise_amplitude(end, count):
    records = _records([0, end])
    actions = ActionDataPrepare(_config()).get_action_list(records)
    assert len(actions) == count
    if count:
        assert (actions[0]["count_record"], actions[0]["peak_record"], actions[0]["recoil_raw"]) == (1, 1, 0)


def test_firmware_count_rate_and_cycle_bytes_do_not_change_actions():
    config = _config()
    records = _records([0, 100, 99, 100, 60, 30, 100, 60, 0])
    expected = ActionDataPrepare(config).get_action_list(deepcopy(records))
    for index, record in enumerate(records):
        record.update(compression_count=index, compression_rate=255 - index, cycle_num=index % 3)
    assert ActionDataPrepare(config).get_action_list(records) == expected


def test_finish_preserves_the_existing_truncated_ventilation():
    records = _records([0] + [100] * 15)
    for record in records:
        record["ventilation_volume"] = [40, 40]
    actions = ActionDataPrepare(_config()).get_action_list(records)
    assert [a["action_type"] for a in actions] == ["comp", "vent"]
    assert (actions[0]["action_start"], actions[0]["action_end"]) == (0, 15)
    assert (actions[1]["action_start"], actions[1]["action_end"]) == (0, 16)


def test_confirmed_peak_outside_window_still_counts_for_quality():
    from calculators.cycle_evaluator import Cycle, CycleWithScore
    from config.score_weight import ScoreWeightFactory

    config = _config()
    actions = ActionDataPrepare(config).get_action_list(_records([0, 100]))
    assert max(actions[0]["compression_depth"]) == 0  # finish 정점은 LAST 버퍼의 첫 Record다.
    scored = ActionEvaluator(config).evaluate_action(actions)
    cycle = CycleWithScore(Cycle(scored, 1), 1, ScoreWeightFactory.create("adult", "cpr", "AHA2020"),
                           config.calculation_config)
    assert cycle._real_comp_actions() == scored
    assert scored[0].score["comp_depth"]["value"] == 50


def test_finish_after_final_record_vent_boundary_keeps_empty_comp_window():
    config = _config()
    records = _records([0, 100])
    records[0]["ventilation_volume"] = [40, 40]
    actions = ActionDataPrepare(config).get_action_list(records)
    assert [a["action_type"] for a in actions] == ["vent", "comp"]
    comp = actions[1]
    assert (comp["action_start"], comp["action_end"], comp["total_action_ms"]) == (1, 1, 0)
    assert comp["compression_depth"] == []
    prepared = prepare_data(actions, [], config)
    assert prepared["comp_count"] == 1
    scored = ActionEvaluator(config).evaluate_action(prepared["prepared_cpr_data"][0]["action_list"])
    assert scored[1].score["comp_depth"]["value"] == 50


def test_cycle_and_chart_never_resegment_the_stream(monkeypatch):
    from calculators import waveform
    from data_handlers.chart_data import make_chart_data
    from services.calculators import calculate_cpr

    config = _config()
    records = _records([0, 100, 0] * 10)
    fed = []
    feed = waveform.CompressionSegmenter.feed

    def counted_feed(self, index, value):
        fed.append(index)
        return feed(self, index, value)

    def no_batch_segmentation(*args, **kwargs):
        raise AssertionError("action 이후 재분절 금지")

    monkeypatch.setattr(waveform.CompressionSegmenter, "feed", counted_feed)
    monkeypatch.setattr(waveform, "segment_compressions", no_batch_segmentation)
    actions = ActionDataPrepare(config).get_action_list(records)
    prepared = prepare_data(actions, [], config)
    calculated = calculate_cpr(prepared, config)
    chart = make_chart_data(prepared["whole_cpr_action_list"], [], calculated["part_with_scores"])
    assert fed == list(range(len(records)))
    assert len(chart["cpr_data_set"]) == prepared["comp_count"] == 10
