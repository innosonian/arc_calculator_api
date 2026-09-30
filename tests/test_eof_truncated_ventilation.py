"""D138: a ventilation cut at the end of the file counts from what was recorded.
D139: the ARC minimum-quantity null is not applied to new calculations (the scores are
shown); the minimum stays a pass condition (tests/test_minimum_quantity_pass_gate.py).

The app stops recording the moment it detects the target breath itself (the
CPR cycle limit, the Ventilation Only limit of 8) and writes nothing after the
stop signal, and the server cannot know whether the cut came before the peak.
A candidate that is still open when the file ends is therefore one breath when
(a) at least one packet already lies the drop threshold below its peak, or
(b) no such packet exists but its highest volume lies at least the drop
    threshold (adult/child 10 mL, infant 5 mL) above the baseline it rose from;
    the cut point is then taken as the peak.
A rise below the threshold, a locked detector and a breath already confirmed
by two packets add nothing. File length is never the evidence.

Expected values are hand-derived from the packet volumes (detection), from
the recorded files' tails, or are the unchanged tester's scores of the named
recording. The retained adapters' options (arc-internal-detection-v4 and
arc-internal-detection-pending-v3: D42 end of file, D07/D08 nulls) keep their
previous values next to every new expectation.
"""

import hashlib
import json
import uuid

import pytest

from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from config.enums import Actor
from data_handlers.action_data import ActionDataPrepare
from data_handlers.data_parser import DataParser
from data_handlers.detection import PacketActionDetector
from main import run_calculator
from mock_journey import typed
from mock_journey.assembly import internal_calculator
from mock_journey.catalog import PROGRAMS
from mock_journey.contracts import (
    CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION,
    RETAINED_PENDING_GOAL_ADAPTER_VERSION, expected_profile_version,
)
from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import LoadedInput, ProjectionSchema, project_input, typed_identity
from mock_journey.worker import evaluate
from scripts.verify_reference_parity import _differences
from services.calculation_context import (
    AcceptedRaw, CalculationContextError, CalculationExecutionContext, CalculationOptions,
)
from services.config import Config
from tests._synth import comp_session, cpr_session, packet, vo_session
from tests.calculation_options_support import CURRENT_OPTIONS, RETAINED_OPTIONS, STEM, run_with_options
from tests.detection_oracle import run_expected
from tests.eof_vent_support import (
    EIGHTH_BREATH_FIRST_LOW_PACKET, PACKET_BYTES, TESTER_RECORDING, TESTER_SHA256, VO_STOPPED_AT_EIGHTH_BREATH,
)
from tests.request_support import condition


PROJECTION = "eof-vent-test-projection-v1"
RAW_BASE = "calculator_result/interpreted_rtdata/arc/test/_no_org/2023-11-14/" + STEM
REQUIRED = {row[0]: (row[2], row[3]) for row in PROGRAMS}
EOF_ONLY = CalculationOptions(eof_single_confirmation=True, minimum_quantity_null=True)
NO_NULL_ONLY = CalculationOptions(eof_single_confirmation=False, minimum_quantity_null=False)


# ---------------------------------------------------------------------------
# 1. The detector
# ---------------------------------------------------------------------------

def _config(target="adult", training="cpr", guideline="ARC2025"):
    return Config(condition(target, training, guideline))


def _packets(volumes, counts=None, depths=None):
    counts = counts if counts is not None else [0] * len(volumes)
    depths = depths if depths is not None else [0] * len(volumes)
    return [{"ventilation_volume": [volume, volume], "compression_count": count, "compression_depth": [depth] * 10,
             "compression_rate": 100 + index, "timestamp": index * 50}
            for index, (volume, count, depth) in enumerate(zip(volumes, counts, depths, strict=True))]


def _vents(volumes, target="adult", training="cpr", *, eof=True, **signal):
    detector = PacketActionDetector(_config(target, training), eof_single_confirmation=eof)
    return [(event.packet_index, event.evidence_start, event.evidence_stop, event.peak_volume, event.peak_index,
             event.first_confirmation_index)
            for event in detector.detect(_packets(volumes, **signal)) if event.action_type == ACTION_TYPE_VENT]


@pytest.mark.parametrize("target,drop", [("adult", 10), ("child", 10), ("infant", 5)])
def test_end_of_file_confirmation_count_boundary(target, drop):
    peak, low = 100, 100 - drop
    # (a) exactly one confirmation packet: recognized only by the current rule.
    assert _vents([peak, low], target) == [(1, 0, 2, peak, 0, 1)]
    assert _vents([40, 70, peak, low], target) == [(3, 0, 4, peak, 2, 3)]
    # (b) zero confirmation packets: still rising, at the peak, on a plateau, or one unit
    # short of a confirmation. The highest recorded volume is the peak; no confirmation index.
    assert _vents([40, 70, peak], target) == [(2, 0, 3, peak, 2, None)]
    assert _vents([peak], target) == [(0, 0, 1, peak, 0, None)]
    assert _vents([peak, peak], target) == [(1, 0, 2, peak, 0, None)]
    assert _vents([peak, low + 1], target) == [(1, 0, 2, peak, 0, None)]
    # The retained D42 option recognizes none of them.
    for volumes in ([peak, low], [40, 70, peak, low], [40, 70, peak], [peak], [peak, peak], [peak, low + 1]):
        assert _vents(volumes, target, eof=False) == []
    # Two confirmation packets were always a breath; the end of the file adds nothing.
    for eof in (True, False):
        assert _vents([peak, low, low], target, eof=eof) == [(2, 0, 3, peak, 0, 1)]


def test_stream_cut_while_rising_at_a_plateau_or_on_a_slow_rise():
    # The tester's sixth breath without its one low packet: cut at the 480 mL peak.
    assert _vents([0, 250, 380, 440, 480]) == [(4, 1, 5, 480, 4, None)]
    # Cut two packets earlier, still rising.
    assert _vents([0, 250, 380]) == [(2, 1, 3, 380, 2, None)]
    # A plateau: 480 then 478 (2 mL lower is no descent); the peak stays the first 480 packet.
    assert _vents([0, 250, 380, 440, 480, 478]) == [(5, 1, 6, 480, 4, None)]
    # A slow rise: the last step is +5 mL, the rise over the baseline is 470 mL.
    assert _vents([0, 200, 400, 465, 470]) == [(4, 1, 5, 470, 4, None)]
    for volumes in ([0, 250, 380, 440, 480], [0, 250, 380], [0, 250, 380, 440, 480, 478], [0, 200, 400, 465, 470]):
        assert _vents(volumes, eof=False) == []


@pytest.mark.parametrize("target,drop", [("adult", 10), ("child", 10), ("infant", 5)])
def test_single_packet_rise_at_the_end_of_the_file_needs_the_threshold(target, drop):
    # One positive packet ends the file: below / at / above the drop threshold over a zero baseline.
    assert _vents([0, 0, drop - 1], target) == []
    assert _vents([0, 0, drop], target) == [(2, 2, 3, drop, 2, None)]
    assert _vents([0, 0, drop + 1], target) == [(2, 2, 3, drop + 1, 2, None)]
    # A candidate that starts at the first packet of the file has baseline 0.
    assert _vents([drop - 1], target) == [] and _vents([drop], target) == [(0, 0, 1, drop, 0, None)]
    # A sub-threshold excursion is not rescued by later packets at the same level.
    assert _vents([0, drop - 1, drop - 1, drop - 1], target) == []
    for volumes in ([0, 0, drop], [0, 0, drop + 1], [drop]):
        assert _vents(volumes, target, eof=False) == []


def test_re_rise_after_the_descent_started_is_an_open_candidate_without_a_confirmation():
    # 100 -> 90 started a descent, 95 cancelled it (D39 reset): zero confirmations at the end.
    # The candidate is still open and rose 100 mL, so (b) recognizes it; D42 does not.
    assert _vents([100, 90, 95]) == [(2, 0, 3, 100, 0, None)]
    assert _vents([100, 90, 95, 95]) == [(3, 0, 4, 100, 0, None)]
    assert _vents([100, 90, 95], eof=False) == [] and _vents([100, 90, 95, 95], eof=False) == []
    # A later higher peak replaces the first; with one low packet it is (a), without it (b).
    assert _vents([100, 90, 120, 100]) == [(3, 0, 4, 120, 2, 3)]
    assert _vents([100, 90, 120, 115]) == [(3, 0, 4, 120, 2, None)]
    assert _vents([100, 90, 120, 100], eof=False) == [] and _vents([100, 90, 120, 115], eof=False) == []


def test_confirmed_breath_is_never_added_again_at_the_end_of_the_file():
    for eof in (True, False):
        # Confirmed by its last two packets: the file ends right after the confirmation.
        assert _vents([100, 90, 90], eof=eof) == [(2, 0, 3, 100, 0, 1)]
        # Locked after the confirmation (positive tail, no rearm): nothing is open at the end,
        # however high the tail is.
        assert _vents([100, 90, 90, 80, 70, 60], eof=eof) == [(2, 0, 3, 100, 0, 1)]
        assert _vents([100, 90, 90, 80, 70, 95, 60], eof=eof) == [(2, 0, 3, 100, 0, 1)]
        assert _vents([100, 90, 90, 300, 400, 500], eof=eof) == [(2, 0, 3, 100, 0, 1)]
        # Confirmed by two zero packets and rearmed: no candidate remains.
        assert _vents([100, 0, 0], eof=eof) == [(2, 0, 3, 100, 0, 1)]
        assert _vents([100, 0, 0, 0], eof=eof) == [(2, 0, 3, 100, 0, 1)]


def test_second_breath_after_rearm_is_judged_from_its_own_baseline():
    first = (2, 0, 3, 100, 0, 1)
    # Rearmed at zero: the next candidate's baseline is 0.
    assert _vents([100, 0, 0, 40, 80]) == [first, (4, 3, 5, 80, 4, None)]          # rising at the end (b)
    assert _vents([100, 0, 0, 40, 80, 80]) == [first, (5, 3, 6, 80, 4, None)]      # ends at its peak (b)
    assert _vents([100, 0, 0, 40, 80, 70]) == [first, (5, 3, 6, 80, 4, 5)]         # one low packet (a)
    assert _vents([100, 0, 0, 9]) == [first]                                       # 9 mL: below the threshold
    for volumes in ([100, 0, 0, 40, 80], [100, 0, 0, 40, 80, 80], [100, 0, 0, 40, 80, 70], [100, 0, 0, 9]):
        assert _vents(volumes, eof=False) == [first]
    # A depth-onset rearm (D43) at a non-zero level: the rearm packet (80 mL) is the baseline of
    # the candidate that starts with the next rise.
    depths = [0, 0, 0, 1, 1, 1, 1, 1]
    assert _vents([100, 90, 90, 80, 70, 75, 85, 70], depths=depths) == [first, (7, 5, 8, 85, 6, 7)]   # (a)
    assert _vents([100, 90, 90, 80, 70, 75, 85, 70], depths=depths, eof=False) == [first]
    depths = [0, 0, 0, 1, 1, 1]
    assert _vents([100, 90, 90, 80, 85, 89], depths=depths) == [first]                       # +9 over 80: no
    assert _vents([100, 90, 90, 80, 85, 90], depths=depths) == [first, (5, 4, 6, 90, 5, None)]   # +10 over 80: (b)
    assert _vents([100, 90, 90, 80, 85, 95], depths=depths) == [first, (5, 4, 6, 95, 5, None)]
    assert _vents([100, 90, 90, 80, 85, 95], depths=depths, eof=False) == [first]
    # Infant: the same with the 5 mL threshold.
    assert _vents([30, 25, 25, 20, 22, 24], "infant", depths=depths) == [(2, 0, 3, 30, 0, 1)]
    assert _vents([30, 25, 25, 20, 22, 25], "infant", depths=depths) == [(2, 0, 3, 30, 0, 1), (5, 4, 6, 25, 5, None)]


@pytest.mark.parametrize("target,volumes", [("adult", [9, 0]), ("adult", [5, 9, 3]), ("infant", [4, 0]),
                                            ("infant", [2, 4, 1])])
def test_signal_that_can_never_meet_the_drop_is_not_recognized(target, volumes):
    assert _vents(volumes, target) == []


def test_compression_only_has_no_ventilation_detection_at_the_end_of_the_file():
    counts = [0, 1, 1, 2, 2]
    detector = PacketActionDetector(_config(training="compression_only"))
    events = detector.detect(_packets([0, 0, 0, 100, 90], counts=counts))
    assert [(event.action_type, event.packet_index) for event in events] == [(ACTION_TYPE_COMP, 1), (ACTION_TYPE_COMP, 3)]


def test_compression_and_end_of_file_breath_in_the_last_packet_are_both_kept_in_order():
    detector = PacketActionDetector(_config())
    events = detector.detect(_packets([0, 100, 90], counts=[0, 0, 1]))
    assert [(event.action_type, event.packet_index) for event in events] == [(ACTION_TYPE_COMP, 2), (ACTION_TYPE_VENT, 2)]
    retained = PacketActionDetector(_config(), eof_single_confirmation=False)
    assert [(e.action_type, e.packet_index) for e in retained.detect(_packets([0, 100, 90], counts=[0, 0, 1]))] == [
        (ACTION_TYPE_COMP, 2)]


def test_option_is_a_keyword_only_bool_and_defaults_to_the_current_rule():
    assert PacketActionDetector(_config()).eof_single_confirmation is True
    assert ActionDataPrepare(_config()).eof_single_confirmation is True
    with pytest.raises(TypeError):
        PacketActionDetector(_config(), False)
    for value in (None, 1, 0, "true"):
        with pytest.raises(TypeError):
            PacketActionDetector(_config(), eof_single_confirmation=value)
    detector = PacketActionDetector(_config())
    stream = _packets([100, 90])
    assert detector.detect(stream) == detector.detect(stream)  # no state between calls
    assert detector.detect([]) == []


def _prepared(volumes, target="adult", training="cpr", eof=True, step_ms=50):
    complete = [dict(item, timestamp=index * step_ms, hand_position=0, ventilation_count=0, ventilation_speed=0,
                     actor=Actor.REAL_PERSON, is_aed_overlapped=False)
                for index, item in enumerate(_packets(volumes))]
    return ActionDataPrepare(_config(target, training), eof_single_confirmation=eof).get_action_list(complete)


def test_end_of_file_breath_is_an_ordinary_action_downstream():
    # Nothing downstream reads past evidence_stop: the action spans the candidate's
    # own packets, the internal LAST action is the final packet and is removed.
    actions = _prepared([0, 100, 200, 300, 250])
    assert [action["action_type"] for action in actions] == [ACTION_TYPE_VENT]
    (vent,) = actions
    assert vent["_source_packet_indices"] == [1, 2, 3, 4] and vent["_event_packet"] == 4
    assert vent["ventilation_volume"] == [100, 100, 200, 200, 300, 300, 250, 250]
    assert (vent["first_timestamp"], vent["last_timestamp"], vent["total_action_ms"]) == (0, 200, 200)
    assert vent["_rate_duration_ms"] == 200
    assert _prepared([0, 100, 200, 300, 250], eof=False) == []
    # Infant ventilation speed: first positive packet to the peak packet (unchanged formula).
    (infant,) = _prepared([0, 10, 20, 30, 25], "infant")
    assert infant["ventilation_speed"] == 100
    # (b) a breath without a descent packet: the same fields, measured up to the cut point.
    (rising,) = _prepared([0, 100, 200, 300])
    assert rising["_source_packet_indices"] == [1, 2, 3] and rising["_event_packet"] == 3
    assert rising["ventilation_volume"] == [100, 100, 200, 200, 300, 300]
    assert (rising["first_timestamp"], rising["last_timestamp"], rising["total_action_ms"]) == (0, 150, 150)
    assert _prepared([0, 100, 200, 300], eof=False) == []
    (infant_rising,) = _prepared([0, 10, 20, 30], "infant")
    assert infant_rising["ventilation_speed"] == 100   # first positive packet to the cut point (the peak)
    (infant_plateau,) = _prepared([0, 10, 20, 30, 30, 30], "infant")
    assert infant_plateau["ventilation_speed"] == 100 and infant_plateau["total_action_ms"] == 250


# ---------------------------------------------------------------------------
# 2. Synthetic sessions cut at the end (the auto-stop shape)
# ---------------------------------------------------------------------------

def _counts(data, target, training, options):
    result, chart, _ = run_with_options(data, b"", condition(target, training), options)
    assert len([row for row in chart["cpr_data_set"] if row["action_type"] == "vent"]) == result["action_count"]["vent"]
    return result["action_count"]["comp"], result["action_count"]["vent"]


@pytest.mark.parametrize("target", ["adult", "child", "infant"])
@pytest.mark.parametrize("breaths", [8, 12])
def test_ventilation_only_last_breath_cut_at_any_point_after_it_rose(target, breaths):
    full = vo_session(breaths)
    # Each synthetic breath is 0, 120, 350, 520 (peak), 0, 0 mL (infant: 0, 12, 35, 52, 0, 0).
    for options in (CURRENT_OPTIONS, RETAINED_OPTIONS):
        assert _counts(full, target, "ventilation_only", options) == (0, breaths)
    # Cut after one low packet (a), at the peak, or while still rising (b): the current rule
    # counts the last breath; the retained D42 option never does.
    for dropped in (1, 2, 3, 4):
        cut = full[:-dropped * PACKET_BYTES]
        assert _counts(cut, target, "ventilation_only", CURRENT_OPTIONS) == (0, breaths)
        assert _counts(cut, target, "ventilation_only", RETAINED_OPTIONS) == (0, breaths - 1)
    # Cut before the last breath has any volume: nothing to count under either rule.
    for options in (CURRENT_OPTIONS, RETAINED_OPTIONS):
        assert _counts(full[:-5 * PACKET_BYTES], target, "ventilation_only", options) == (0, breaths - 1)


@pytest.mark.parametrize("target,per_cycle", [("adult", 30), ("child", 30), ("infant", 15)])
def test_cpr_last_cycle_breath_cut_at_any_point_after_it_rose(target, per_cycle):
    full = cpr_session([(per_cycle, 2)] * 3)
    one_low = full[:-PACKET_BYTES]
    for dropped in (1, 2, 3, 4):   # one low packet, the peak, two points of the rise
        cut = full[:-dropped * PACKET_BYTES]
        assert _counts(cut, target, "cpr", CURRENT_OPTIONS) == (per_cycle * 3, 6)
        assert _counts(cut, target, "cpr", RETAINED_OPTIONS) == (per_cycle * 3, 5)
    assert _counts(full[:-5 * PACKET_BYTES], target, "cpr", CURRENT_OPTIONS) == (per_cycle * 3, 5)
    # The three cycles stay closed either way; the last one holds one or two breaths.
    for options, last in ((CURRENT_OPTIONS, 2), (RETAINED_OPTIONS, 1)):
        _, _, evidence = run_with_options(one_low, b"", condition(target, "cpr"), options)
        assert [(cycle.calc_case, cycle.ventilation_action_count) for cycle in evidence.cycles] == [
            ("cpr", 2), ("cpr", 2), ("cpr", last)]


def test_compression_only_session_ignores_a_trailing_breath_signal():
    tail = [packet(60, (50, 52), 7000), packet(60, (48, 50), 7050)]  # 520 -> 500 mL, one low packet
    data = comp_session(60) + b"".join(tail)
    for options in (CURRENT_OPTIONS, RETAINED_OPTIONS):
        assert _counts(data, "adult", "compression_only", options) == (60, 0)


# ---------------------------------------------------------------------------
# 3. The tester's recording (tests/dataset/cpr_eof_truncated_vent_1.bin)
# ---------------------------------------------------------------------------

CHEST = ("score_comp_depth", "score_recoil", "score_hand_position", "score_comp_no", "score_comp_count")
VENT = ("score_vent_vol", "score_vent_count", "score_vent_rate")


def test_recording_identity_and_tail():
    assert len(TESTER_RECORDING) == 23968 and hashlib.sha256(TESTER_RECORDING).hexdigest() == TESTER_SHA256
    packets = DataParser().parse_cpr_bytes(TESTER_RECORDING, _config())
    assert len(packets) == 856
    volumes = [max(item["ventilation_volume"]) for item in packets]
    # The sixth breath: 250 -> 380 -> 440 -> 480 (peak) -> 380, then the file ends.
    assert volumes[846:] == [0, 0, 0, 0, 0, 250, 380, 440, 480, 380]
    assert packets[-1]["compression_count"] == 6  # the app's own counter in the last cycle


def _recording(options):
    result, chart, evidence = run_with_options(TESTER_RECORDING, b"", condition("adult", "cpr"), options)
    return result, chart, evidence


def test_recording_under_the_current_rules():
    result, chart, evidence = _recording(CURRENT_OPTIONS)
    total = result["cpr_score"]["total_score"]
    assert result["action_count"] == {"comp": 61, "vent": 6}
    # D139: every chest and ventilation group score is present; the overall is numeric.
    assert {key: total[key] for key in CHEST} == {"score_comp_depth": 99, "score_recoil": 35, "score_hand_position": 81,
                                                 "score_comp_no": 0, "score_comp_count": 0}
    assert {key: total[key] for key in VENT} == {"score_vent_vol": 100, "score_vent_count": 100, "score_vent_rate": 0}
    assert (total["score_comp_rate"], total["score_ccf"], total["overall"]) == (94, 100, 83)
    assert total["score_vent_speed"] is None  # not evaluated for an adult, as before
    for part in result["cpr_score"]["part_scores"]:
        assert all(part["score"][key] is not None for key in CHEST + VENT + ("overall",))
        for cycle in part["cycle_with_score_list"]:
            assert all(cycle[key] is not None for key in CHEST + VENT + ("overall",))
    # D136 closed cycles: three, with 20/21/20 compressions and two breaths each.
    assert [(cycle.calc_case, cycle.compression_action_count, cycle.ventilation_action_count)
            for cycle in evidence.cycles] == [("cpr", 20, 2), ("cpr", 21, 2), ("cpr", 20, 2)]
    assert result["training_stats"]["cycle_count"] == 3
    # The chart shows both breaths of the last cycle; the sixth peaks at 480 mL.
    rows = chart["cpr_data_set"]
    assert [row["vent_vol_max"] for row in rows if row["action_type"] == "vent" and row["cycle_num"] == 3] == [530, 480]
    assert sum(row["action_type"] == "vent" for row in rows) == 6


def test_recording_under_the_retained_adapters_options_is_unchanged():
    result, chart, evidence = _recording(RETAINED_OPTIONS)
    total = result["cpr_score"]["total_score"]
    assert result["action_count"] == {"comp": 61, "vent": 5}
    assert all(total[key] is None for key in CHEST + VENT) and total["overall"] is None
    assert (total["score_comp_rate"], total["score_ccf"]) == (94, 100)
    assert [(cycle.calc_case, cycle.compression_action_count, cycle.ventilation_action_count)
            for cycle in evidence.cycles] == [("cpr", 20, 2), ("cpr", 21, 2), ("cpr", 20, 1)]
    assert [row["vent_vol_max"] for row in chart["cpr_data_set"]
            if row["action_type"] == "vent" and row["cycle_num"] == 3] == [530]


def test_each_decision_changes_only_its_own_part_of_the_recording():
    # D138 alone (the minimum-quantity null kept): the sixth breath meets the
    # ventilation minimum, the chest group (61 < 90) stays null, overall 98.
    eof_only = _recording(EOF_ONLY)[0]
    total = eof_only["cpr_score"]["total_score"]
    assert eof_only["action_count"] == {"comp": 61, "vent": 6}
    assert all(total[key] is None for key in CHEST)
    assert {key: total[key] for key in VENT} == {"score_vent_vol": 100, "score_vent_count": 100, "score_vent_rate": 0}
    assert total["overall"] == 98
    # D139 alone (the D42 end of file kept): five breaths, everything scored, overall 82.
    no_null = _recording(NO_NULL_ONLY)[0]
    total = no_null["cpr_score"]["total_score"]
    assert no_null["action_count"] == {"comp": 61, "vent": 5}
    assert all(total[key] is not None for key in CHEST + VENT)
    assert (total["score_vent_count"], total["overall"]) == (83, 82)


def test_direct_call_without_a_context_uses_the_current_rules():
    direct = run_calculator(TESTER_RECORDING, b"", condition("adult", "cpr"), stage="test")
    assert not _differences(json.loads(json.dumps(direct)), json.loads(json.dumps(_recording(CURRENT_OPTIONS)[0])))
    assert direct["action_count"] == {"comp": 61, "vent": 6} and direct["cpr_score"]["total_score"]["overall"] == 83


@pytest.mark.parametrize("options", [CURRENT_OPTIONS, RETAINED_OPTIONS, EOF_ONLY, NO_NULL_ONLY])
def test_recording_matches_the_independent_detection_oracle(options):
    expected, expected_chart = run_expected(TESTER_RECORDING, b"", condition("adult", "cpr"), options=options)
    result, chart, _ = _recording(options)
    assert not _differences(json.loads(json.dumps(expected)), json.loads(json.dumps(result)))
    assert not _differences(json.loads(json.dumps(expected_chart)), json.loads(json.dumps(chart)))


def test_ventilation_only_recording_stopped_at_the_eighth_breath():
    packets = DataParser().parse_cpr_bytes(VO_STOPPED_AT_EIGHTH_BREATH, _config("infant", "ventilation_only"))
    volumes = [max(item["ventilation_volume"]) for item in packets]
    assert len(packets) == EIGHTH_BREATH_FIRST_LOW_PACKET + 1
    assert max(volumes[321:]) == 41
    assert volumes[-8:] == [41, 41, 41, 41, 41, 41, 41, 0]  # the peak, then exactly one low packet
    current = run_with_options(VO_STOPPED_AT_EIGHTH_BREATH, b"", condition("infant", "ventilation_only"), CURRENT_OPTIONS)[0]
    retained = run_with_options(VO_STOPPED_AT_EIGHTH_BREATH, b"", condition("infant", "ventilation_only"), RETAINED_OPTIONS)[0]
    assert (current["action_count"]["vent"], current["cpr_score"]["total_score"]["overall"]) == (8, 92)
    assert (retained["action_count"]["vent"], retained["cpr_score"]["total_score"]["overall"]) == (7, 91)


def test_real_recordings_cut_without_a_descent_packet():
    cond = condition("infant", "ventilation_only")
    # vo_1.bin cut at the eighth breath's 41 mL plateau (no low packet recorded).
    at_peak = VO_STOPPED_AT_EIGHTH_BREATH[:-PACKET_BYTES]
    current, chart, _ = run_with_options(at_peak, b"", cond, CURRENT_OPTIONS)
    assert (current["action_count"], current["cpr_score"]["total_score"]["overall"]) == ({"comp": 0, "vent": 8}, 92)
    vents = [row for row in chart["cpr_data_set"] if row["action_type"] == "vent"]
    assert len(vents) == 8 and vents[-1]["vent_vol_max"] == 41
    retained = run_with_options(at_peak, b"", cond, RETAINED_OPTIONS)[0]
    assert (retained["action_count"]["vent"], retained["cpr_score"]["total_score"]["overall"]) == (7, 91)
    # The prepared action of that breath: packets 321..344, infant speed from the first positive
    # packet to the first 41 mL packet (871 ms), duration up to the cut point (2580 ms).
    packets = DataParser().parse_cpr_bytes(at_peak, _config("infant", "ventilation_only"))
    for item in packets:
        item.update(is_aed_overlapped=False, actor=Actor.REAL_PERSON)
    last = ActionDataPrepare(_config("infant", "ventilation_only")).get_action_list(packets)[-1]
    assert (last["action_type"], last["_event_packet"], last["_source_packet_indices"][0],
            last["_source_packet_indices"][-1]) == (ACTION_TYPE_VENT, 344, 321, 344)
    assert (last["ventilation_speed"], last["total_action_ms"], last["_rate_duration_ms"]) == (871, 2580, 2580)
    assert max(last["ventilation_volume"]) == 41
    # Cut 12 packets earlier, while the breath is still rising through 32 mL.
    rising = VO_STOPPED_AT_EIGHTH_BREATH[:-13 * PACKET_BYTES]
    current, chart, _ = run_with_options(rising, b"", cond, CURRENT_OPTIONS)
    assert (current["action_count"]["vent"], current["cpr_score"]["total_score"]["overall"]) == (8, 92)
    assert [row for row in chart["cpr_data_set"] if row["action_type"] == "vent"][-1]["vent_vol_max"] == 32
    assert run_with_options(rising, b"", cond, RETAINED_OPTIONS)[0]["action_count"]["vent"] == 7
    # Cut before the eighth breath has any volume (packet 320 is the last zero): seven under both rules.
    before = VO_STOPPED_AT_EIGHTH_BREATH[:321 * PACKET_BYTES]
    for options in (CURRENT_OPTIONS, RETAINED_OPTIONS):
        assert run_with_options(before, b"", cond, options)[0]["action_count"]["vent"] == 7

    # The tester's recording without its one low packet (cut at the 480 mL peak) and cut at 380 mL.
    for dropped, shown in ((1, 480), (3, 380)):
        cut = TESTER_RECORDING[:-dropped * PACKET_BYTES]
        current, chart, evidence = run_with_options(cut, b"", condition("adult", "cpr"), CURRENT_OPTIONS)
        assert current["action_count"] == {"comp": 61, "vent": 6}
        assert current["cpr_score"]["total_score"]["overall"] == 83
        assert [row["vent_vol_max"] for row in chart["cpr_data_set"]
                if row["action_type"] == "vent" and row["cycle_num"] == 3] == [530, shown]
        assert [cycle.ventilation_action_count for cycle in evidence.cycles] == [2, 2, 2]
        retained = run_with_options(cut, b"", condition("adult", "cpr"), RETAINED_OPTIONS)[0]
        assert retained["action_count"] == {"comp": 61, "vent": 5}
        assert retained["cpr_score"]["total_score"]["overall"] is None


@pytest.mark.parametrize("options", [CURRENT_OPTIONS, RETAINED_OPTIONS])
@pytest.mark.parametrize("dropped", [1, 3])
def test_recording_cut_without_a_descent_matches_the_independent_oracle(dropped, options):
    cut = TESTER_RECORDING[:-dropped * PACKET_BYTES]
    expected, expected_chart = run_expected(cut, b"", condition("adult", "cpr"), options=options)
    result, chart, _ = run_with_options(cut, b"", condition("adult", "cpr"), options)
    assert not _differences(json.loads(json.dumps(expected)), json.loads(json.dumps(result)))
    assert not _differences(json.loads(json.dumps(expected_chart)), json.loads(json.dumps(chart)))


# ---------------------------------------------------------------------------
# 4. Options travel with the adapter version
# ---------------------------------------------------------------------------

def test_calculation_options_are_a_frozen_pair_of_bools_with_current_defaults():
    assert CalculationOptions() == CalculationOptions(eof_single_confirmation=True, minimum_quantity_null=False)
    with pytest.raises(Exception):
        CalculationOptions().eof_single_confirmation = False
    for bad in ({"eof_single_confirmation": 1}, {"minimum_quantity_null": None}, {"minimum_quantity_null": "no"}):
        with pytest.raises(CalculationContextError):
            CalculationOptions(**bad)


def test_execution_context_carries_the_options_and_rejects_anything_else():
    receipt = AcceptedRaw(hashlib.sha256(b"x").hexdigest(), 1, hashlib.sha256(b"").hexdigest(), 0, STEM, "_no_org")
    base = {"accepted_raw": receipt, "publish_chart": lambda chart: None, "observe": lambda evidence: None}
    assert CalculationExecutionContext(**base).options == CalculationOptions()
    assert CalculationExecutionContext(**base, options=RETAINED_OPTIONS).options is RETAINED_OPTIONS
    for bad in ({"eof_single_confirmation": False}, (False, True), True, "retained"):
        with pytest.raises(CalculationContextError):
            CalculationExecutionContext(**base, options=bad)
    with pytest.raises(AttributeError):
        CalculationExecutionContext(**base).options = RETAINED_OPTIONS


def _accepted(program, target, data, adapter_version):
    kind, required = REQUIRED[program]
    training = {"compressions": "compression_only", "ventilations": "ventilation_only"}.get(kind, "cpr")
    cond = condition(target, training)
    definition = {"condition": cond, "calculation_profile": {}, "goal": {"kind": kind, "required": required},
                  "catalog_version": "mock-catalog-v1", "profile_version": expected_profile_version(adapter_version),
                  "adapter_version": adapter_version, "projection_version": PROJECTION}
    projected = project_input({"condition": cond, "cpr_b64_data": data, "aed_b64_data": b"", "vp_event_list": []},
                              definition, ProjectionSchema(PROJECTION, {}))
    binding = {"attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()), "input_digest": typed_identity(projected),
               "adapter_version": adapter_version, "projection_version": PROJECTION,
               "job_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    return LoadedInput(projected, RAW_BASE), binding, definition


def _through_adapter(program, target, data, adapter_version):
    loaded, binding, definition = _accepted(program, target, data, adapter_version)
    adapter = internal_calculator(adapter_version, projection=PROJECTION, stage="test")
    raw = adapter.calculate(loaded, binding, lambda: None)
    verified = adapter.validate_response(raw, loaded.projected, binding)
    assessment = evaluate(verified.core_result, definition, verified)
    assert DynamoJobRepository.check_evaluation(assessment, {"definition_json": json.dumps(definition)}) == assessment
    return verified, assessment, typed.parse_json(raw)


def test_tester_recording_through_each_adapter_version():
    verified, assessment, candidate = _through_adapter("mock-cpr", "adult", TESTER_RECORDING, CURRENT_ADAPTER_VERSION)
    assert CURRENT_ADAPTER_VERSION == "arc-internal-detection-v5"
    assert candidate["schema"] == "arc-internal-calculation-v4"
    assert candidate["counts"] == {"comp": 61, "vent": 6}
    assert verified.core_result["cpr_score"]["total_score"]["overall"] == 83
    # D139: the score is shown, but 61 compressions are below the ARC minimum of 90.
    assert assessment == {"goal": {"kind": "cycles", "required": 3, "observed": 3, "met": True, "status": "evaluated"},
                          "score": {"decision": "fail"}, "program_completed": False,
                          "reason_codes": ["MINIMUM_QUANTITY_NOT_MET"]}

    verified, assessment, candidate = _through_adapter("mock-cpr", "adult", TESTER_RECORDING, CYCLE_GOAL_ADAPTER_VERSION)
    assert candidate["schema"] == "arc-internal-calculation-v3"
    assert candidate["counts"] == {"comp": 61, "vent": 5}
    assert verified.core_result["cpr_score"]["total_score"]["overall"] is None
    # The v4 result carries exactly the goal/score reasons it always had (no minimum code there).
    assert assessment == {"goal": {"kind": "cycles", "required": 3, "observed": 3, "met": True, "status": "evaluated"},
                          "score": {"decision": "fail"}, "program_completed": False, "reason_codes": ["SCORE_NOT_PASS"]}

    verified, assessment, candidate = _through_adapter("mock-cpr", "adult", TESTER_RECORDING, PENDING_GOAL_ADAPTER_VERSION)
    assert candidate["schema"] == "arc-internal-calculation-v2"
    assert candidate["counts"] == {"comp": 61, "vent": 5}
    assert verified.core_result["cpr_score"]["total_score"]["overall"] is None
    assert assessment["goal"]["status"] == "pending_policy" and assessment["program_completed"] is False
    assert assessment["reason_codes"] == ["GOAL_POLICY_UNRESOLVED", "SCORE_NOT_PASS"]


def test_ventilation_only_goal_of_eight_is_met_only_by_the_current_adapter():
    verified, assessment, _ = _through_adapter("mock-ventilation-only", "infant", VO_STOPPED_AT_EIGHTH_BREATH,
                                               CURRENT_ADAPTER_VERSION)
    assert assessment == {"goal": {"kind": "ventilations", "required": 8, "observed": 8, "met": True, "status": "evaluated"},
                          "score": {"decision": "pass"}, "program_completed": True, "reason_codes": []}
    for retained in (CYCLE_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION):
        verified, assessment, _ = _through_adapter("mock-ventilation-only", "infant", VO_STOPPED_AT_EIGHTH_BREATH, retained)
        assert assessment == {"goal": {"kind": "ventilations", "required": 8, "observed": 7, "met": False,
                                       "status": "evaluated"},
                              "score": {"decision": "pass"}, "program_completed": False, "reason_codes": ["GOAL_NOT_MET"]}
    # The same recording cut at the eighth breath's peak (no low packet) or while it is still
    # rising: met and completed under the current adapter, 7 under the retained ones.
    for dropped in (1, 13):
        cut = VO_STOPPED_AT_EIGHTH_BREATH[:-dropped * PACKET_BYTES]
        _, assessment, _ = _through_adapter("mock-ventilation-only", "infant", cut, CURRENT_ADAPTER_VERSION)
        assert assessment["goal"]["observed"] == 8 and assessment["program_completed"] is True
        assert assessment["reason_codes"] == []
        for retained in (CYCLE_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION):
            _, assessment, _ = _through_adapter("mock-ventilation-only", "infant", cut, retained)
            assert assessment["goal"]["observed"] == 7 and assessment["reason_codes"] == ["GOAL_NOT_MET"]


def test_verify_only_adapter_still_never_calculates(monkeypatch):
    monkeypatch.setattr("main.run_calculator", lambda *args, **kwargs: pytest.fail("verify-only adapter ran the core"))
    loaded, binding, _ = _accepted("mock-cpr", "adult", TESTER_RECORDING, RETAINED_PENDING_GOAL_ADAPTER_VERSION)
    adapter = internal_calculator(RETAINED_PENDING_GOAL_ADAPTER_VERSION, projection=PROJECTION, stage="test")
    assert adapter.can_calculate is False
    with pytest.raises(JourneyError) as error:
        adapter.calculate(loaded, binding, lambda: None)
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"


def test_a_candidate_cannot_be_moved_between_the_v4_and_v5_adapters():
    loaded, binding, _ = _accepted("mock-cpr", "adult", TESTER_RECORDING, CYCLE_GOAL_ADAPTER_VERSION)
    v4 = internal_calculator(CYCLE_GOAL_ADAPTER_VERSION, projection=PROJECTION, stage="test")
    v5 = internal_calculator(CURRENT_ADAPTER_VERSION, projection=PROJECTION, stage="test")
    raw = v4.calculate(loaded, binding, lambda: None)
    with pytest.raises(JourneyError) as error:
        v5.validate_response(raw, loaded.projected, binding)
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    relabelled = typed.parse_json(raw)
    relabelled["schema"] = "arc-internal-calculation-v4"
    with pytest.raises(JourneyError):
        v4.validate_response(typed.json_bytes(relabelled), loaded.projected, binding)


def test_local_health_policy_is_unchanged_by_the_new_adapter():
    from local_server import http as local_http
    assert dict(local_http._COMPLETION_POLICY) == {"cycles": "evaluated", "compressions": "evaluated",
                                                  "ventilations": "evaluated"}


# ---------------------------------------------------------------------------
# 5. The public /api/v2 journey (in-memory store; integration_tests has the DynamoDB Local twin)
# ---------------------------------------------------------------------------

def test_tester_recording_is_scored_but_fails_the_minimum_and_a_full_session_completes_in_memory():
    from tests.eof_vent_support import recorded_cpr_below_minimum_journey
    from tests.journey_support import JourneyStore
    recorded_cpr_below_minimum_journey(JourneyStore.memory())


def test_auto_stopped_ventilation_only_recording_completes_the_practice_in_memory():
    from tests.eof_vent_support import auto_stopped_ventilation_only_journey
    from tests.journey_support import JourneyStore
    auto_stopped_ventilation_only_journey(JourneyStore.memory())


def test_in_flight_v4_attempts_keep_their_meaning_after_the_upgrade_in_memory():
    from tests.eof_vent_support import in_flight_v4_attempts_keep_their_meaning_after_the_upgrade
    from tests.journey_support import JourneyStore
    in_flight_v4_attempts_keep_their_meaning_after_the_upgrade(JourneyStore.memory())
