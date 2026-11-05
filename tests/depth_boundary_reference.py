"""DEPTH_V1r3 raw reference for dataset checks; never imports production segmentation.

The scanner alternates searches for a first 30-raw fall and a first 30-raw rise.
Existing scoring borders are deliberately reused: this change does not redefine grades.
"""
from math import floor

from config.borders import dart_get_point, trapezium_get_point
from config.enums import Actor


# Derived from raw peak intervals by assert_dataset_depth_contract, not refreshed snapshots.
RATE_SCORES = {"cco_1": 99, "cpr_1": 92, "cpr_2": 100, "cpr_3": 100, "cpr_4": 97,
               "cpr_5": 98, "infant_cco_idle_flag": 94, "infant_cpr_idle_flag_pause": 93}


def reference_events(raw):
    packets = [raw[i + 1:i + 11] for i in range(0, len(raw) - 27, 28)]
    maxima = [max(packet) for packet in packets]
    peaks = []
    cursor = 0
    valley = maxima[0] if maxima else 0
    while cursor < len(maxima):
        peak = cursor
        while cursor < len(maxima) and maxima[peak] - maxima[cursor] < 30:
            if maxima[cursor] > maxima[peak]:
                peak = cursor
            cursor += 1
        if cursor == len(maxima):
            if maxima[peak] - valley >= 30:
                peaks.append((peak, cursor - 1))
            break
        peaks.append((peak, cursor))
        valley = maxima[cursor]
        while cursor < len(maxima) and maxima[cursor] - valley < 30:
            valley = min(valley, maxima[cursor])
            cursor += 1
    samples = b"".join(packets)
    events = []
    for record, confirm in peaks:
        offset = packets[record].index(maxima[record])
        events.append({"peak_record": record, "count_record": confirm, "peak_offset": offset,
                       "peak_sample": record * 10 + offset, "depth_raw": maxima[record]})
    for index, event in enumerate(events):
        end = events[index + 1]["peak_sample"] if index + 1 < len(events) else len(samples)
        span = samples[event["peak_sample"] + 1:end]
        recoil = min(span) if span else 0
        if index + 1 == len(events) and span and recoil == span[-1]:
            recoil = 0
        event["recoil_raw"] = recoil
    return events


def reference_hand(raw, action, following, infant):
    codes = raw[:len(raw) // 28 * 28][13::28]
    own = list(codes[action["action_start"]:action["action_end"]])
    next_start = following["action_start"] if following else action["action_end"]
    next_end = following["action_end"] if following else len(codes)
    after = list(codes[next_start:next_end])
    trimmed = list(own)
    while trimmed and trimmed[-1] == 0:
        trimmed.pop()
    last_zero = max((i for i, value in enumerate(trimmed) if value == 0), default=-1)
    window = trimmed[last_zero + 1:]
    if own and after and own[-1] and after[0] and 0 in after:
        window += after[:after.index(0)]
    judged = [code & 15 for code in window] if infant else window
    if 2 in judged:
        spot = "down"
    elif 4 in judged or 8 in judged:
        spot = "left" if judged.count(4) > judged.count(8) else "right"
    else:
        spot = "center"
    return window, spot


def assert_dataset_depth_contract(raw, config, prepared, calculation, result, chart):
    events = reference_events(raw)
    by_peak = {event["peak_record"]: event for event in events}
    actions = prepared["whole_cpr_action_list"]
    comps = [action for action in actions if action["action_type"] == "comp"]
    bars = [bar for bar in chart["cpr_data_set"] or [] if bar["action_type"] == "comp"]
    cycles = [cycle for part in calculation["part_with_scores"] for cycle in part["result"]["cycle_with_score_list"]]
    assert all(action["actor"] == Actor.REAL_PERSON for action in comps)  # These fixtures have no VP.
    assert len(events) == result["action_count"]["comp"] == len(comps) == len(bars)
    assert len(comps) == sum(cycle.cycle.get_comp_count() for cycle in cycles)
    assert [action["peak_record"] for action in comps] == list(by_peak)
    for action, event, bar in zip(comps, events, bars):
        for key in ("peak_record", "count_record", "peak_offset", "depth_raw", "recoil_raw"):
            assert action[key] == event[key], (key, action[key], event[key])
        assert action["action_end"] == event["count_record"]
        assert (bar["comp_depth_max"], bar["comp_depth_min"]) == (event["depth_raw"], event["recoil_raw"])
        record = event["peak_record"]
        timestamp = int.from_bytes(raw[record * 28 + 20:record * 28 + 28], "big")
        assert bar["timestamp"] == (timestamp + event["peak_offset"] * 5) / 1000
    hand = {}
    for index, action in enumerate(actions):
        assert action["action_start"] == (actions[index - 1]["action_end"] if index else 0)
        if action["action_type"] == "comp":
            following = actions[index + 1] if index + 1 < len(actions) else None
            window, spot = reference_hand(raw, action, following, config.condition["target"] == "infant")
            assert action["hand_position"] == window
            hand[action["peak_record"]] = spot
    rate_cycle_scores, hand_cycle_scores, scored_count = [], [], 0
    rounded = lambda value: floor(value + 0.5)
    for cycle in cycles:
        cycle_comps = [a for a in cycle.cycle.actions if a.action_type == "comp"]
        cycle_events = cycle._compression_events()
        assert len(cycle_events) == len(cycle_comps)
        for event, action in zip(cycle_events, cycle_comps):
            expected = by_peak[action.action_data["peak_record"]]
            assert (event.peak_packet_index, event.peak_sample_offset, event.peak_depth, event.recoil_depth) == (
                expected["peak_record"], expected["peak_offset"], expected["depth_raw"], expected["recoil_raw"],
            )
        rate_grades, hand_grades = [], []
        previous = None
        for action in cycle_comps:
            data = action.action_data
            sample = by_peak[data["peak_record"]]["peak_sample"]
            rate = 0 if previous is None else rounded(60000 / ((sample - previous) * 5))
            assert action.score["comp_rate"]["value"] == rate
            assert action.score["hand_position"]["value"] == hand[data["peak_record"]]
            if previous is not None:
                rate_grades.append(trapezium_get_point(config.border, "comp_rate", rate)["grade"])
            hand_grades.append(dart_get_point(config.border, "hand_position", hand[data["peak_record"]])["grade"])
            previous = sample
        if cycle.can_calc_comp():
            expected_rate = rounded(sum(rate_grades) / len(rate_grades)) if rate_grades else 0
            expected_hand = rounded(sum(hand_grades) / len(hand_grades))
            assert cycle.score_comp_rate == expected_rate
            assert cycle.score_hand_position == expected_hand
            rate_cycle_scores.append(expected_rate)
            hand_cycle_scores.append(expected_hand)
            scored_count += len(cycle_comps)
    assert result["metrics"]["CompressionActionNumber"] == scored_count
    if rate_cycle_scores:
        expected_rate = rounded(sum(rate_cycle_scores) / len(rate_cycle_scores))
        assert result["cpr_score"]["total_score"]["score_comp_rate"] == expected_rate
        assert result["cpr_score"]["total_score"]["score_hand_position"] == rounded(sum(hand_cycle_scores) / len(hand_cycle_scores))
        return expected_rate
    return None
