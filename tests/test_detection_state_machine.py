"""Hand-derived event expectations, independent of score/timeline allocation."""

from copy import deepcopy
from itertools import product

import pytest

from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from config.enums import Actor
from calculators.action_evaluator import ActionEvaluator
from calculators.action_timeline import timeline_totals
from data_handlers.action_data import ActionDataPrepare
from data_handlers.data_parser import DataParser
from data_handlers.detection import PacketActionDetector
from services.config import Config
from transformers.counter import CountMarker


GUIDELINES = ("AHA2020", "ARC2020", "ARC2025", "ERC2020", "STD2015")
TARGETS = ("adult", "child", "infant")
TRAINING_TYPES = ("cpr", "compression_only", "ventilation_only")


def config(target="adult", training="cpr", guideline="ARC2025"):
    return Config({
        "mode": "training", "target": target, "training_type": training,
        "guideline": guideline, "cpr_cycle_type": "152" if target == "infant" else "302",
        "is_2rescuers": False,
    })


def packets(volumes, counts=None, depths=None):
    counts = counts if counts is not None else [0] * len(volumes)
    depths = depths if depths is not None else [0] * len(volumes)
    return [
        {
            "ventilation_volume": list(volume) if isinstance(volume, tuple) else [volume, volume],
            "compression_count": count, "compression_depth": [depth] * 10,
            "compression_rate": 100 + index, "timestamp": index * 50,
        }
        for index, (volume, count, depth) in enumerate(zip(volumes, counts, depths, strict=True))
    ]


def vents(stream, target="adult", eof_single_confirmation=True):
    # eof_single_confirmation=False is the retained adapters' D42 rule (v4 / pending-v3).
    detector = PacketActionDetector(config(target), eof_single_confirmation=eof_single_confirmation)
    return [event for event in detector.detect(stream) if event.action_type == ACTION_TYPE_VENT]


@pytest.mark.parametrize("volumes", [
    [100, 90, 89], [100, 90, 90], [100, 89, 90], [100, 0, 0],
])
@pytest.mark.parametrize("target", ["adult", "child"])
def test_user_confirmed_two_packet_examples(volumes, target):
    events = vents(packets(volumes), target)
    assert [(event.packet_index, event.peak_volume, event.first_confirmation_index)
            for event in events] == [(2, 100, 1)]


@pytest.mark.parametrize("target,drop", [("adult", 10), ("child", 10), ("infant", 5)])
@pytest.mark.parametrize("offset,expected", [(-1, 0), (0, 1), (1, 1)])
def test_drop_threshold_boundaries_in_scaled_ml(target, drop, offset, expected):
    lower = 30 - drop - offset
    # The D39 two-packet confirmation itself (retained D42 option: the end of the file adds nothing).
    assert len(vents(packets([30, lower, lower]), target, eof_single_confirmation=False)) == expected
    # D138 (b): the same three-packet stream ends with the candidate still open when the drop
    # was one unit short; it rose 30 above its baseline, so the current rule recognizes it once.
    assert len(vents(packets([30, lower, lower]), target)) == 1
    # Followed by more packets at the same level the confirmation boundary is the only question again.
    assert len(vents(packets([30, lower, lower, 0, 0, 0]), target)) == 1


@pytest.mark.parametrize("eof_single_confirmation", [False, True])
@pytest.mark.parametrize("volumes,index", [
    ([100, 90, 95, 90], None),
    ([100, 90, 95, 90, 90], 4),
    ([100, 90, 110, 100, 100], 4),
])
def test_confirmation_reset_and_peak_replacement(volumes, index, eof_single_confirmation):
    events = vents(packets(volumes), eof_single_confirmation=eof_single_confirmation)
    if index is None and eof_single_confirmation:
        # D138 (a): the reset candidate ends the file one observed low packet after
        # its peak; the retained D42 option leaves it unrecognized.
        assert [(e.packet_index, e.evidence_start, e.evidence_stop, e.peak_volume, e.peak_index,
                 e.first_confirmation_index) for e in events] == [(3, 0, 4, 100, 0, 3)]
        return
    assert [event.packet_index for event in events] == ([] if index is None else [index])
    if len(volumes) == 5 and volumes[2] == 110:
        assert events[0].peak_volume == 110
        assert events[0].peak_index == 2


@pytest.mark.parametrize("count_at", [1, 2, 3])
def test_compression_during_candidate_or_at_confirmation_preserves_both(count_at):
    stream = packets([0, 100, 90, 90], counts=[int(i >= count_at) for i in range(4)])
    events = PacketActionDetector(config()).detect(stream)
    assert [(e.action_type, e.packet_index) for e in events if e.action_type == ACTION_TYPE_COMP] == [(ACTION_TYPE_COMP, count_at)]
    assert [(e.action_type, e.packet_index, e.peak_volume) for e in events if e.action_type == ACTION_TYPE_VENT] == [(ACTION_TYPE_VENT, 3, 100)]


@pytest.mark.parametrize("guideline,target,training", list(product(GUIDELINES, TARGETS, TRAINING_TYPES)))
def test_all_supported_guidelines_ages_and_training_types(guideline, target, training):
    stream = packets([0, 100, 90, 90], counts=[0, 0, 0, 1])
    events = PacketActionDetector(config(target, training, guideline)).detect(stream)
    expected = ([ACTION_TYPE_COMP] if training != "ventilation_only" else []) + ([ACTION_TYPE_VENT] if training != "compression_only" else [])
    assert [event.action_type for event in events] == expected
    assert all(event.packet_index == 3 for event in events)


def test_exact_zero_rearms_without_reusing_confirmation_packet():
    events = vents(packets([100, 0, 0, 80, 70, 70]))
    assert [(e.packet_index, e.evidence_start, e.peak_volume) for e in events] == [(2, 0, 100), (5, 3, 80)]


def test_same_decline_and_positive_infant_tail_do_not_rearm():
    assert len(vents(packets([20, 15, 15, 9, 8, 7, 9, 7, 6]), "infant")) == 1


def test_depth_rearm_waits_for_later_volume_rise():
    stream = packets([100, 90, 90, 80, 70, 60, 65, 75, 65, 65],
                     depths=[0, 0, 0, 1, 2, 3, 3, 3, 3, 3])
    events = vents(stream)
    assert [(e.packet_index, e.evidence_start, e.peak_volume) for e in events] == [(2, 0, 100), (9, 6, 75)]


def test_ongoing_depth_rise_is_not_another_onset_after_confirmation():
    stream = packets([100, 90, 90, 95, 85, 85, 90, 80, 80],
                     depths=[0, 1, 2, 3, 4, 5, 6, 7, 8])
    assert [e.packet_index for e in vents(stream)] == [2]


def test_depth_onset_during_confirmation_does_not_cancel_candidate():
    stream = packets([100, 90, 90, 95, 85, 85], depths=[0, 0, 1, 1, 1, 1])
    assert [(e.packet_index, e.peak_volume) for e in vents(stream)] == [(2, 100), (5, 95)]


@pytest.mark.parametrize("volumes", [[100], [100] * 30, [100, 90], [100, 90] + [95] * 30])
def test_eof_never_infers_missing_confirmation(volumes):
    # Retained D42 option (v4 / pending-v3): unchanged.
    assert vents(packets(volumes), eof_single_confirmation=False) == []


@pytest.mark.parametrize("volumes,expected", [
    # (a) one observed low packet ends the file
    ([100, 90], [(1, 0, 2, 100, 0, 1)]),
    # (b) no descent packet: the highest recorded volume is taken as the peak, once,
    # whatever the length of the plateau (length is never the evidence; the rise is).
    ([100], [(0, 0, 1, 100, 0, None)]),
    ([100] * 30, [(29, 0, 30, 100, 0, None)]),
    ([100, 90] + [95] * 30, [(31, 0, 32, 100, 0, None)]),   # a recovery reset the confirmation
    ([0, 250, 380, 440, 480], [(4, 1, 5, 480, 4, None)]),   # still rising when cut
    # A rise below the drop threshold above the baseline is not a breath.
    ([9], []), ([0, 5, 9], []), ([9] * 30, []),
])
def test_eof_recognizes_an_open_candidate_by_its_descent_or_its_rise(volumes, expected):
    assert [(e.packet_index, e.evidence_start, e.evidence_stop, e.peak_volume, e.peak_index,
             e.first_confirmation_index) for e in vents(packets(volumes))] == expected


def test_confirmed_long_tail_is_not_counted_again_at_eof():
    assert len(vents(packets([100, 90, 90] + [80] * 30))) == 1


def test_first_counter_is_baseline_and_reset_wrap_jump_are_observed_once():
    stream = packets([0] * 9, counts=[1, 1, 2, 0, 1, 35, 1, 1, 3])
    events = PacketActionDetector(config()).detect(stream)
    assert [event.packet_index for event in events] == [2, 4, 5, 6, 8]
    assert [event.compression_rate for event in events] == [102, 104, 105, 106, 108]


def test_packet_representative_is_max_not_min():
    # Retained D42 option: the representative (max) stays 100, so nothing confirms.
    assert vents(packets([(100, 1), (100, 0), (100, 0)]), eof_single_confirmation=False) == []
    assert len(vents(packets([(1, 100), (90, 0), (0, 90)]), eof_single_confirmation=False)) == 1
    # Current rule: the same plateau is an open candidate at the end of the file (D138 b);
    # it is one breath with the representative maximum as its peak, not the minimum.
    assert [(e.packet_index, e.peak_volume, e.first_confirmation_index)
            for e in vents(packets([(100, 1), (100, 0), (100, 0)]))] == [(2, 100, None)]
    assert len(vents(packets([(1, 100), (90, 0), (0, 90)]))) == 1


@pytest.mark.parametrize("peak,expected", [(4, 0), (5, 1), (6, 1)])
def test_provisional_infant_five_ml_has_no_extra_ten_ml_gate(peak, expected):
    assert len(vents(packets([peak, 0, 0]), "infant")) == expected


def test_unconfirmable_signal_returning_to_zero_is_not_next_breath_onset():
    events = vents(packets([4, 0, 0, 5, 0, 0]), "infant")
    assert [(event.packet_index, event.evidence_start, event.peak_volume) for event in events] == [(5, 3, 5)]


@pytest.mark.parametrize("target", ["adult", "child"])
def test_binary_raw_fifty_fortynine_fortynine_preserves_parser_types(target):
    binary = bytearray()
    for index, volume in enumerate([50, 49, 49]):
        packet = bytearray(28)
        packet[0] = 168
        packet[14:16] = bytes([volume, volume])
        packet[20:28] = (index * 50).to_bytes(8, "big")
        binary.extend(packet)
    cfg = config(target)
    parsed = DataParser().parse_cpr_bytes(bytes(binary), cfg)
    before = deepcopy(parsed)
    assert [packet["ventilation_volume"] for packet in parsed] == [[500, 500], [490, 490], [490, 490]]
    assert all(type(value) is int for packet in parsed for value in packet["ventilation_volume"])
    assert len(PacketActionDetector(cfg).detect(parsed)) == 1
    assert parsed == before


def test_detector_does_not_mutate_packets_or_retain_previous_call_state():
    stream = packets([100, 90, 90], counts=[0, 0, 1])
    before = deepcopy(stream)
    detector = PacketActionDetector(config())
    first = detector.detect(stream)
    assert first == detector.detect(stream)
    assert detector.detect([]) == []
    assert stream == before


def prepared_actions(stream, target="adult", training="cpr"):
    complete = [dict(packet, hand_position=0, ventilation_count=0, ventilation_speed=0,
                     actor=Actor.REAL_PERSON, is_aed_overlapped=False) for packet in stream]
    return ActionDataPrepare(config(target, training)).get_action_list(complete)


def test_pipeline_preserves_both_evidence_ranges_and_boundary_rate():
    stream = packets([0, 100, 90, 90], counts=[0, 0, 0, 1])
    actions = prepared_actions(stream)
    assert [a["action_type"] for a in actions] == [ACTION_TYPE_COMP, ACTION_TYPE_VENT]
    comp, vent = actions
    assert comp["compression_rate"] == [103]
    assert comp["_source_timestamps"] == [0, 50, 100]
    assert vent["_source_timestamps"] == [50, 100, 150]
    assert (comp["first_timestamp"], comp["last_timestamp"], comp["total_action_ms"]) == (0, 100, 100)
    # D45: the overlapping breath starts at its own first positive packet.
    assert (vent["first_timestamp"], vent["last_timestamp"], vent["total_action_ms"]) == (50, 150, 100)
    assert vent["_rate_duration_ms"] == 100
    assert max(vent["ventilation_volume"]) == 100
    assert timeline_totals(actions) == (150, 50)
    assert all(type(a["compression_depth"]) is list and type(a["ventilation_volume"]) is list for a in actions)
    assert all(type(a["first_timestamp"]) is int and type(a["total_action_ms"]) is int for a in actions)


def test_intervening_compression_does_not_change_breath_rate_or_peak():
    clean = prepared_actions(packets([0, 100, 90, 90]))[0]
    noisy = [a for a in prepared_actions(packets([0, 100, 90, 90], counts=[0, 0, 1, 1]))
             if a["action_type"] == ACTION_TYPE_VENT][0]
    assert noisy["_rate_duration_ms"] == clean["_rate_duration_ms"] == 150
    assert noisy["ventilation_volume"] == clean["ventilation_volume"]


def test_next_compression_does_not_reuse_peak_from_before_breath():
    stream = packets([0, 0, 100, 90, 90, 0, 0], counts=[0, 1, 1, 1, 1, 1, 2],
                     depths=[0, 100, 0, 0, 0, 0, 100])
    comp = [a for a in prepared_actions(stream) if a["action_type"] == ACTION_TYPE_COMP]
    assert len(comp) == 2
    assert comp[-1]["_source_timestamps"] == [200, 250]
    assert max(comp[-1]["compression_depth"]) == 0


def test_pressure_already_rising_survives_breath_confirmation_without_previous_peak():
    stream = packets([0, 0, 100, 90, 90, 0, 0], counts=[0, 1, 1, 1, 1, 1, 2],
                     depths=[0, 100, 0, 80, 90, 0, 0])
    comp = [a for a in prepared_actions(stream) if a["action_type"] == ACTION_TYPE_COMP]
    assert len(comp) == 2
    assert comp[-1]["_source_timestamps"] == [100, 150, 200, 250]
    assert max(comp[-1]["compression_depth"]) == 90


def test_new_breath_signal_does_not_borrow_previous_peak():
    stream = packets([100, 90, 90, 80, 70, 60, 65, 75, 65, 65],
                     depths=[0, 0, 0, 1, 2, 3, 3, 3, 3, 3])
    actions = prepared_actions(stream)
    assert [max(action["ventilation_volume"]) for action in actions] == [100, 75]


def test_approved_two_second_overlap_preserves_one_second_breath_rate():
    comp = {"action_type": ACTION_TYPE_COMP, "_elapsed_interval": (0, 2000),
            "total_action_ms": 2000, "handsoff_ms": 1000}
    vent = {"action_type": ACTION_TYPE_VENT, "_elapsed_interval": (1000, 2000),
            "total_action_ms": 1000, "handsoff_ms": 1000, "ventilation_volume": [100],
            "ventilation_speed": 0, "actor": Actor.REAL_PERSON, "_rate_duration_ms": 1000}
    assert timeline_totals([comp, vent]) == (2000, 1000)
    score = ActionEvaluator(config()).evaluate_action([vent])[0]
    assert score.score["vent_rate"]["value"] == 60
    assert score.action_data["total_action_ms"] == 1000


def test_disjoint_timeline_preserves_old_scalar_arithmetic():
    actions = [
        {"action_type": ACTION_TYPE_COMP, "_elapsed_interval": (0, 2000), "total_action_ms": 2000, "handsoff_ms": 1000},
        {"action_type": ACTION_TYPE_VENT, "_elapsed_interval": (2050, 3050), "total_action_ms": 1000, "handsoff_ms": 1000},
    ]
    assert timeline_totals(actions) == (3000, 2000)
    legacy = [{k: v for k, v in a.items() if k != "_elapsed_interval"} for a in actions]
    assert timeline_totals(legacy) == (3000, 2000)


def test_same_packet_group_uses_one_cycle_decision():
    stream = packets([0, 100, 90, 90, 0, 80, 70, 70], counts=[0, 0, 0, 1, 1, 1, 1, 2])
    actions = prepared_actions(stream)
    CountMarker().make_count([{"action_list": actions}])
    assert [(a["action_type"], a["cycle_cnt"]) for a in actions] == [
        (ACTION_TYPE_COMP, 1), (ACTION_TYPE_VENT, 1),
        (ACTION_TYPE_COMP, 2), (ACTION_TYPE_VENT, 2),
    ]
    assert actions[0]["_timeline_bounds"] == (0, 150)
    assert actions[1]["_timeline_bounds"] == (0, 150)
    assert actions[2]["_timeline_bounds"] == (150, None)
    assert actions[3]["_timeline_bounds"] == (150, None)
    assert sum(timeline_totals([a for a in actions if a["cycle_cnt"] == cycle])[0]
               for cycle in [1, 2]) == timeline_totals(actions)[0]


def test_small_infant_candidate_preserves_signal_actor():
    prep = ActionDataPrepare(config("infant"))
    assert prep._primary_actor([(Actor.REAL_PERSON, 0, 0), (Actor.VIRTUAL_PARTNER, 0, 5),
                                (Actor.REAL_PERSON, 0, 0)], ACTION_TYPE_VENT) == Actor.VIRTUAL_PARTNER
