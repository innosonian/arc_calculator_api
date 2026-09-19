"""Independent reviewer expectations for the changed chart-rendering paths.

These expected times and bar counts are written from the input samples; they do
not use the detection oracle or run the candidate renderer to create goldens.
"""

from types import SimpleNamespace

from config.enums import Actor
from data_handlers.chart_data import make_chart_data


def _action(packet_depths, times, *, kind="comp", volume=0):
    return {"action_type": kind, "compression_depth": [v for packet in packet_depths for v in packet],
            "compression_count": 1, "ventilation_volume": [volume],
            "first_timestamp": times[0], "last_timestamp": times[-1], "_source_timestamps": times,
            "part_num": 1, "cycle_cnt": 1, "overlap_type": None, "actor": Actor.REAL_PERSON}


def _chart(actions):
    scores = [{"part_num": 1, "result": {"cycle_with_score_list": [
        SimpleNamespace(cycle=SimpleNamespace(cycle_num=1))]}}]
    return make_chart_data(actions, [], scores)


def test_peak_sample_time_uses_the_source_packet_after_a_large_gap():
    # The second packet starts at1000ms. Its first maximum is sample index4:
    # 1000 + 4*(50/10) =1020ms. Reconstructing the packet from its list index
    # would instead produce70ms and erase the observed gap.
    peak = [0, 20, 40, 60, 80, 80, 60, 40, 20, 0]
    action = _action([[0] * 10, peak, [0] * 10], [0, 1000, 1050])
    chart = _chart([action])
    assert len(chart["cpr_data_set"]) == 1
    mark = chart["cpr_data_set"][0]
    assert mark["timestamp"] == 1.020
    assert mark["comp_depth_max"] == 80 and mark["comp_depth_min"] == 0
    assert chart["meta"] == {"start_timestamp": 1.020, "end_timestamp": 1.020}


def test_same_cycle_interposed_breath_does_not_split_one_pressure_waveform():
    # Packet maxima0,40,30,80,0 have one qualifying peak: the intermediate
    # decline is10 (<unchanged amplitude25). Splitting at the breath would
    # independently close the40 ascent and the80 descent, producing two bars.
    rising = _action([[0] * 10, [40] * 10, [30] * 10], [0, 50, 100])
    breath = _action([[0] * 10, [0] * 10], [100, 150], kind="vent", volume=500)
    falling = _action([[80] * 10, [0] * 10], [150, 200])
    marks = _chart([rising, breath, falling])["cpr_data_set"]
    pressure = [mark for mark in marks if mark["action_type"] == "comp"]
    ventilation = [mark for mark in marks if mark["action_type"] == "vent"]
    assert len(pressure) == len(ventilation) == 1
    assert pressure[0]["comp_depth_max"] == 80 and pressure[0]["timestamp"] == 0.150
    assert pressure[0]["comp_count"] == 1
    assert ventilation[0]["vent_vol_max"] == 500 and ventilation[0]["timestamp"] == 0.125
    assert [mark["timestamp"] for mark in marks] == [0.125, 0.150]
