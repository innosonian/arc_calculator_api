"""Raw measurements -> actual calculator -> tester Pass -> goal completion.

Expected counts and scores below are arithmetic, not snapshots of the current
calculator. No fake calculated score or observed count is injected.
"""

from copy import deepcopy
import json
import uuid

import pytest

from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_PROFILE_VERSION
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.projection import LoadedInput, ProjectionSchema, project_input, typed_identity
from mock_journey.worker import evaluate
from tests._synth import packet


TARGETS = ("adult", "child", "infant")
GUIDELINES = ("AHA2020", "ARC2020", "ARC2025", "ERC2020", "STD2015")


def condition(target, training, guideline="ARC2025"):
    return {"mode": "training", "target": target, "training_type": training, "guideline": guideline,
            "cpr_cycle_type": "152" if target == "infant" else "302", "is_2rescuers": False}


def compression_bytes(count, target, good):
    peak = (70 if target == "infant" else 100) if good else 40
    ramp = (0, peak // 5, peak // 2, peak, peak, peak // 2, peak // 5, 0, 0, 0)
    packets = [packet(timestamp=0, rate=110)]
    for number in range(1, count + 1):
        packets += [packet(number, timestamp=number * 100, depth=ramp, rate=110),
                    packet(number, timestamp=number * 100 + 50, rate=110)]
    return b"".join(packets)


def ventilation_bytes(count, target, good):
    # 10/min adult, 25/min child/infant are inside the existing ARC good bands.
    interval = 6000 if target == "adult" else 2400
    peak = (30 if target == "infant" else 50) if good else (5 if target == "infant" else 10)
    drop = 5 if target == "infant" else 1
    packets = [packet(timestamp=0)]
    for breath in range(count):
        for offset in range(50, interval + 1, 50):
            if offset == 50:
                volume = 0  # rearm only, not also a new candidate
            elif offset < 700:
                volume = max(1, peak // 3)
            elif offset < interval - 50:
                volume = peak
            else:
                volume = peak - drop  # two consecutive peak-relative lows
            packets.append(packet(vent_raw=(volume, volume), timestamp=breath * interval + offset))
    return b"".join(packets)


def calculated(cpr, cond):
    training = cond["training_type"]
    kind, required = {"compression_only": ("compressions", 60), "ventilation_only": ("ventilations", 8),
                      "cpr": ("cycles", 3)}[training]
    projection = "detection-completion-test-v1"
    definition = {"condition": deepcopy(cond), "calculation_profile": {}, "goal": {"kind": kind, "required": required},
                  "catalog_version": "mock-catalog-v1", "profile_version": PENDING_GOAL_PROFILE_VERSION,
                  "adapter_version": PENDING_GOAL_ADAPTER_VERSION, "projection_version": projection}
    projected = project_input({"condition": cond, "cpr_b64_data": cpr, "aed_b64_data": b"", "vp_event_list": []},
                              definition, ProjectionSchema(projection, {}))
    binding = {"attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()), "input_digest": typed_identity(projected),
               "adapter_version": PENDING_GOAL_ADAPTER_VERSION, "projection_version": projection,
               "job_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    loaded = LoadedInput(projected, "calculator_result/interpreted_rtdata/arc/test/_no_org/2023-11-14/"
                         "CPR-ACTION-1700000000-12345678-1234-4234-9234-123456789abc")
    adapter = InternalCalculator(version=PENDING_GOAL_ADAPTER_VERSION, projection_version=projection,
                                 stage="test", allow_pending_cycle_goal=True)
    raw = adapter.calculate(loaded, binding, lambda: None)
    verified = adapter.validate_response(raw, projected, binding)
    return verified, evaluate(verified.core_result, definition, verified), json.loads(raw)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("count", (59, 60, 61))
@pytest.mark.parametrize("good,expected_score", ((True, 100), (False, 70)))
def test_raw_compression_goal_boundary_and_actual_tester_score(target, count, good, expected_score):
    # The unchanged CycleWithScore._real_comp_actions excludes the baseline's
    # zero-depth interval from the quality average (not from observed count).
    # All scored depths are good or weak: 100*.3+70=100, 0*.3+70=70.
    verified, assessment, candidate = calculated(compression_bytes(count, target, good), condition(target, "compression_only"))
    assert verified.observed == count and type(verified.observed) is int
    assert verified.core_result["action_count"] == {"comp": count, "vent": 0}
    assert verified.core_result["cpr_score"]["total_score"]["overall"] == expected_score
    assert assessment["goal"] == {"kind": "compressions", "required": 60, "observed": count,
                                   "met": count >= 60, "status": "evaluated"}
    assert assessment["score"] == {"decision": "pass" if good else "fail"}
    assert assessment["program_completed"] is (count >= 60 and good)
    assert candidate["counts"] == {"comp": count, "vent": 0}


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("count", (7, 8, 9))
@pytest.mark.parametrize("good", (True, False))
def test_raw_ventilation_goal_boundary_and_actual_tester_score(target, count, good):
    # All good volume/rate/speed grades100 give100. A small but confirmed
    # breath has volume grade0: adult/child rate weight25%=25; infant rate20%
    # plus speed20%=40. Recognition alone must never imply passing quality.
    expected_score = 100 if good else 40 if target == "infant" else 25
    verified, assessment, candidate = calculated(ventilation_bytes(count, target, good), condition(target, "ventilation_only"))
    assert verified.observed == count and type(verified.observed) is int
    assert verified.core_result["action_count"] == {"comp": 0, "vent": count}
    assert verified.core_result["cpr_score"]["total_score"]["overall"] == expected_score
    assert assessment["goal"] == {"kind": "ventilations", "required": 8, "observed": count,
                                   "met": count >= 8, "status": "evaluated"}
    assert assessment["score"] == {"decision": "pass" if good else "fail"}
    assert assessment["program_completed"] is (count >= 8 and good)
    assert candidate["counts"] == {"comp": 0, "vent": count}


@pytest.mark.parametrize("guideline", GUIDELINES)
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("training", ("cpr", "compression_only", "ventilation_only"))
def test_raw_simultaneous_detection_across_supported_conditions(guideline, target, training):
    from main import run_calculator
    peak, low = (30, 25) if target == "infant" else (50, 49)
    raw = b"".join([packet(timestamp=0), packet(vent_raw=(peak, peak), timestamp=50),
                    packet(vent_raw=(low, low), timestamp=100),
                    packet(1, (low, low), timestamp=150), packet(1, timestamp=200)])
    result = run_calculator(raw, b"", condition(target, training, guideline), stage="test")
    assert result["action_count"] == {"comp": 0 if training == "ventilation_only" else 1,
                                      "vent": 0 if training == "compression_only" else 1}


@pytest.mark.parametrize("target", TARGETS)
def test_new_detection_does_not_complete_cpr_without_cycle_policy(target):
    from tests._synth import cpr_session
    raw = cpr_session([(15 if target == "infant" else 30, 2)] * 3)
    verified, assessment, _ = calculated(raw, condition(target, "cpr"))
    assert verified.core_result["action_count"] == {"comp": 45 if target == "infant" else 90, "vent": 6}
    assert assessment["goal"] == {"kind": "cycles", "required": 3, "observed": None,
                                   "met": None, "status": "pending_policy"}
    assert assessment["program_completed"] is False
    assert "GOAL_POLICY_UNRESOLVED" in assessment["reason_codes"]


def test_approved_overlapping_raw_breath_keeps_its_one_second_measurement():
    from calculators.action_evaluator import ActionEvaluator
    from calculators.action_timeline import timeline_totals
    from data_handlers.action_data import ActionDataPrepare
    from data_handlers.data_parser import DataParser
    from services.config import Config

    cfg = Config(condition("adult", "cpr"))
    raw = []
    for timestamp in range(0, 2001, 50):
        volume = 0 if timestamp < 1000 else 50 if timestamp < 1950 else 49
        raw.append(packet(int(timestamp == 2000), (volume, volume), timestamp,
                          depth=(0, 20, 50, 100, 100, 50, 20, 0, 0, 0)))
    packets = DataParser().parse_cpr_bytes(b"".join(raw), cfg)
    for item in packets:
        item["is_aed_overlapped"] = False
    actions = ActionDataPrepare(cfg).get_action_list(packets)
    assert [a["action_type"] for a in actions] == ["comp", "vent"]
    vent = actions[1]
    assert vent["_source_timestamps"][0] == 1000 and vent["_source_timestamps"][-1] == 2000
    assert vent["_rate_duration_ms"] == 1000
    assert ActionEvaluator(cfg).evaluate_action([vent])[0].score["vent_rate"]["value"] == 60.0
    assert timeline_totals(actions) == (2000, 1000)


@pytest.mark.parametrize("compression_at,last_packet,training,expected_duration", [
    ((1500,), 2000, "cpr", 1000),              # comp confirms during the breath
    ((2500,), 2500, "cpr", 1000),              # overlapping comp confirms later
    ((1050,), 2000, "cpr", 2000),              # source intervals only touch at1000
    ((1500, 1750, 2000), 2000, "cpr", 1000),  # multiple comps do not move the onset
    ((1500, 1750, 2000), 2000, "ventilation_only", 2000),  # unused events cannot change cadence
])
def test_raw_overlap_measurement_boundaries(compression_at, last_packet, training, expected_duration):
    from data_handlers.action_data import ActionDataPrepare
    from data_handlers.data_parser import DataParser
    from services.config import Config

    cfg = Config(condition("adult", training))
    raw = []
    for timestamp in range(0, last_packet + 1, 50):
        volume = 0 if timestamp < 1000 else 50 if timestamp < 1950 else 49
        depth = (0 if timestamp < 1500 else 80) if last_packet == 2500 else 100
        raw.append(packet(sum(t <= timestamp for t in compression_at), (volume, volume), timestamp,
                          depth=(depth,) * 10))
    stream = DataParser().parse_cpr_bytes(b"".join(raw), cfg)
    for item in stream:
        item["is_aed_overlapped"] = False
    vents = [action for action in ActionDataPrepare(cfg).get_action_list(stream) if action["action_type"] == "vent"]
    assert len(vents) == 1
    assert vents[0]["_source_timestamps"][0] == 1000 and vents[0]["_source_timestamps"][-1] == 2000
    assert vents[0]["_rate_duration_ms"] == expected_duration


def test_raw_chart_keeps_source_sample_time_and_overlapping_breath_midpoint():
    from hashlib import sha256
    from main import run_calculator
    from services.calculation_context import AcceptedRaw, CalculationExecutionContext

    # One pressure peak is sample3 in the packet at50ms: 50+3*5=65ms.
    # The confirmed breath occupies1000..2000ms; its midpoint is1500ms.
    # Deliberately unequal packet gaps detect accidental synthetic timestamps.
    raw = b"".join([
        packet(timestamp=0),
        packet(timestamp=50, depth=(0, 20, 50, 100, 100, 50, 20, 0, 0, 0)),
        packet(1, timestamp=500),
        packet(1, (50, 50), timestamp=1000),
        packet(1, (49, 49), timestamp=1500),
        packet(2, (49, 49), timestamp=2000),
    ])
    charts = []
    context = CalculationExecutionContext(
        accepted_raw=AcceptedRaw(sha256(raw).hexdigest(), len(raw), sha256(b"").hexdigest(), 0,
                                 "CPR-ACTION-1700000000-12345678-1234-4234-9234-123456789abc", "_no_org"),
        publish_chart=lambda chart: charts.append(deepcopy(chart)), observe=lambda _evidence: None,
    )
    result = run_calculator(raw, b"", condition("adult", "cpr"), stage="test", execution_context=context)
    assert result["action_count"] == {"comp": 2, "vent": 1}
    assert len(charts) == 1
    chart = charts[0]
    assert chart["meta"] == {"start_timestamp": 0.065, "end_timestamp": 1.5}
    assert chart["aed_data_set"] is None
    marks = chart["cpr_data_set"]
    assert [(mark["action_type"], mark["timestamp"]) for mark in marks] == [("comp", 0.065), ("vent", 1.5)]
    assert marks[0]["comp_depth_max"] == 100 and type(marks[0]["comp_depth_max"]) is int
    assert marks[1]["vent_vol_max"] == 500 and type(marks[1]["vent_vol_max"]) is int
    assert all(mark["cycle_num"] == 1 and mark["is_virtual_action"] is False for mark in marks)
