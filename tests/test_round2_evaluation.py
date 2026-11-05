"""Round 2 evaluation and unchanged core scores with gate evidence enabled."""

import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from calculators.gate_evaluator import collect_action_evidence, evaluate_gates
from mock_journey.course_contracts import validate_assessment_policy
from mock_journey import typed
from mock_journey.assembly import internal_calculator
from mock_journey.contracts import EOF_VENT_ADAPTER_VERSION, ROUND2_ADAPTER_VERSION, expected_profile_version
from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import LoadedInput, ProjectionSchema, project_input, typed_identity
from mock_journey.worker import evaluate
from services.calculation_context import CalculationOptions, CycleEvidence
from tests._synth import _breaths, _compressions, cpr_session, packet
from tests.calculation_context_support import STEM, run_with_options


DATASET = Path(__file__).parent / "dataset"
PROJECTION = "arc-local-projection-v1"
RAW_BASE = "calculator_result/interpreted_rtdata/arc/test/_no_org/2023-11-14/" + STEM
MINIMUM = "MINIMUM_QUANTITY_NOT_MET"
GATE = "GATE_NOT_PASSED"
GOAL = "GOAL_NOT_MET"


def definition(*, adapter=ROUND2_ADAPTER_VERSION, target="adult", kind="cycles", ratio="302",
               required=3, minimum=(90, 6), two_rescuers=False, course_type="BLS", aed=False,
               pass_rule="composite", thresholds=None):
    condition = {
        "mode": "training", "target": target, "guideline": "ARC2025", "cpr_cycle_type": ratio,
        "training_type": {"compressions": "compression_only", "ventilations": "ventilation_only"}.get(kind, "cpr"),
        "is_2rescuers": two_rescuers,
    }
    result = {
        "condition": condition, "calculation_profile": {}, "adapter_version": adapter,
        "profile_version": expected_profile_version(adapter), "projection_version": PROJECTION,
        "catalog_version": "round2-test-v1", "goal": {"kind": kind, "required": required},
    }
    if adapter == ROUND2_ADAPTER_VERSION:
        result.update(
            courseType=course_type, requiresAed=aed,
            minimumQuantity={"compressions": minimum[0], "ventilations": minimum[1]},
            assessment={"passRule": pass_rule, "gates": thresholds or {}},
        )
    return result


def run(definition, cpr, aed=b"", vp=()):
    projected = project_input(
        {"condition": definition["condition"], "cpr_b64_data": cpr, "aed_b64_data": aed, "vp_event_list": list(vp)},
        definition, ProjectionSchema(PROJECTION, {}),
    )
    version = definition["adapter_version"]
    binding = {key: str(uuid.uuid4()) for key in ("attempt_id", "epoch", "job_id", "call_id")}
    binding.update(input_digest=typed_identity(projected), adapter_version=version, projection_version=PROJECTION)
    adapter = internal_calculator(version, projection=PROJECTION, stage="test")
    raw = adapter.calculate(LoadedInput(projected, RAW_BASE), binding, lambda: None)
    verified = adapter.validate_response(raw, projected, binding)
    assessment = evaluate(verified.core_result, definition, verified)
    assert DynamoJobRepository.check_evaluation(assessment, {"definition_json": json.dumps(definition)}) == assessment
    return verified, assessment


@pytest.mark.parametrize("cpr_file,aed_file,settings", [
    *[(f"cpr_{index}.bin", f"aed_{index}.bin", {"aed": True}) for index in range(1, 6)],
    ("cpr_eof_truncated_vent_1.bin", None, {}),
    ("cco_1.bin", None, {"kind": "compressions", "required": 60, "minimum": (60, 0)}),
    ("vo_1.bin", None, {"kind": "ventilations", "required": 8, "target": "infant", "ratio": "152", "minimum": (0, 8)}),
    ("adult_vo_1.bin", None, {"kind": "ventilations", "required": 8, "minimum": (0, 8)}),
])
def test_gate_evidence_preserves_core_scores_and_v5_evaluation_rules_on_recorded_inputs(cpr_file, aed_file, settings):
    cpr = (DATASET / cpr_file).read_bytes()
    aed = (DATASET / aed_file).read_bytes() if aed_file else b""
    config = definition(**settings)
    core, _, _ = run_with_options(cpr, aed, config["condition"], CalculationOptions())
    new, after = run(config, cpr, aed)
    # v5 stored evaluation rules remain callable; no retained adapter recalculates.
    before = evaluate(core, definition(adapter=EOF_VENT_ADAPTER_VERSION, **settings), new)
    for key in ("cpr_score", "metrics"):
        assert typed.json_bytes(new.core_result[key]) == typed.json_bytes(core[key])
    for key in ("score", "program_completed", "reason_codes"):
        assert typed.json_bytes(after[key]) == typed.json_bytes(before[key])
    assert "gates" not in before and "gates" in after


def _gate(rows, gate_id):
    return next(row for row in rows if row["id"] == gate_id)


def _gates(*, observed=3, cycle_comp=30, percentages=None, interval=70, aed_facts=None, **settings):
    facts = {
        "pads_attached": 1, "analysis_contacts": 0, "shock_delivered": 1,
        "no_shock_only": False, "arrived": True, "arrival_seconds": 2,
        "ventilation_interval_score": interval,
        **(aed_facts or {}),
    }
    evidence = SimpleNamespace(cycles=(CycleEvidence(1, 1, "cpr", "ONLY:RP", cycle_comp, 2),), gate_evidence=facts)
    metrics = {name: None if value is None else {"%_Good": value} for name, value in (percentages or {}).items()}
    return evaluate_gates(definition(aed=True, **settings), observed, metrics, evidence)


def test_gate_defaults_accept_partial_settings_and_nullable_metrics():
    assert validate_assessment_policy({"passRule": "gates"}) == {"passRule": "gates", "gates": {}}
    thresholds = {"ventilation_volume_in_range_pct": 75}
    assert validate_assessment_policy({"gates": thresholds}) == {"passRule": "composite", "gates": thresholds}
    row = _gate(_gates(percentages={"VentilationVolume": None}), "ventilation_volume_in_range_pct")
    assert row["applicable"] is True and row["value"] is None and row["passed"] is False


@pytest.mark.parametrize("gate_id,metric,threshold", [
    ("compression_depth_in_range_pct", "CompressionDepth", 70),
    ("compression_rate_in_range_pct", "CompressionRate", 70),
    ("recoil_pct", "Recoil", 80),
    ("hand_position_pct", "HandPosition", 80),
    ("ventilation_volume_in_range_pct", "VentilationVolume", 70),
])
def test_percentage_gates_use_inclusive_threshold_and_override(gate_id, metric, threshold):
    for value, passed in ((threshold - 1, False), (threshold, True)):
        assert _gate(_gates(percentages={metric: value}), gate_id)["passed"] is passed
    gate = _gate(_gates(percentages={metric: threshold}, thresholds={gate_id: threshold + 1}), gate_id)
    assert gate["threshold"] == threshold + 1 and gate["passed"] is False


@pytest.mark.parametrize("observed,passed", [(2, False), (3, True)])
def test_repetitions_threshold(observed, passed):
    assert _gate(_gates(observed=observed), "repetitions")["passed"] is passed


@pytest.mark.parametrize("count,passed", [(27, False), (28, True), (32, True), (33, False)])
def test_compression_per_cycle_tolerance(count, passed):
    assert _gate(_gates(cycle_comp=count), "compressions_per_cycle_tolerance")["passed"] is passed


def test_interval_gate_applies_only_to_bls_ventilation_only_and_exposes_measured_score():
    gate_id = "ventilation_interval_score"
    assert _gate(_gates(kind="ventilations", interval=69), gate_id)["passed"] is False
    assert _gate(_gates(kind="ventilations", interval=70), gate_id)["passed"] is True
    assert _gate(_gates(kind="ventilations", interval=None), gate_id)["passed"] is False
    for course_type, kind in (("BLS", "cycles"), ("BLS", "compressions"),
                              ("CPR_AED", "cycles"), ("CPR_AED", "ventilations")):
        assert _gate(_gates(interval=70, course_type=course_type, kind=kind), gate_id) == {
            "id": gate_id, "applicable": False, "value": None, "threshold": None, "unit": "score", "passed": None,
        }


def test_aed_analysis_gate_requires_analyzing_event():
    for codes, value, passed in (((0, 1, 10, 16), None, False), ((0, 1, 10, 13, 16), 0, True)):
        events = [{"event": code, "timestamp": index * 100} for index, code in enumerate(codes)]
        facts = collect_action_evidence([], events)
        gate = _gate(_gates(aed_facts=facts), "aed_no_contact_during_analysis")
        assert gate["applicable"] is True
        assert gate["value"] == value and gate["passed"] is passed


def test_aed_gates_and_pending_arrival_threshold():
    for gate_id, key, invalid, valid in (
        ("aed_pads_attached", "pads_attached", 0, 1),
        ("aed_no_contact_during_analysis", "analysis_contacts", 1, 0),
        ("aed_shock_delivered", "shock_delivered", 0, 1),
    ):
        assert _gate(_gates(aed_facts={key: invalid}), gate_id)["passed"] is False
        assert _gate(_gates(aed_facts={key: valid}), gate_id)["passed"] is True
    no_shock = _gate(_gates(aed_facts={"shock_delivered": 0, "no_shock_only": True}), "aed_shock_delivered")
    assert no_shock["applicable"] is False and no_shock["passed"] is None
    timer = "aed_arrival_to_power_on"
    assert _gate(_gates(), timer) == {
        "id": timer, "applicable": True, "value": 2, "threshold": None, "unit": "seconds", "passed": None,
    }
    assert _gate(_gates(aed_facts={"arrived": False}), timer)["applicable"] is False
    assert _gate(_gates(thresholds={timer: 2}), timer)["passed"] is True
    assert _gate(_gates(thresholds={timer: 1.99}), timer)["passed"] is False


def _aed_packet(event, timestamp):
    return bytes((180, event)) + timestamp.to_bytes(8, "big")


def test_one_cycle_uses_its_definition_minimum_and_records_no_shock():
    cpr = cpr_session([(30, 2)])
    # Start AED before CPR, with no compression during analysis.
    first = 1_000_000
    shifted = b"".join(
        cpr[index:index + 20] + (int.from_bytes(cpr[index + 20:index + 28], "big") + first).to_bytes(8, "big")
        for index in range(0, len(cpr), 28)
    )
    aed = b"".join(_aed_packet(event, first - 250 + 50 * index) for index, event in enumerate((0, 1, 10, 13, 21)))
    definition_v6 = definition(required=1, minimum=(30, 2), aed=True, course_type="CPR_AED")
    verified, result = run(definition_v6, shifted, aed)
    assert result["goal"]["observed"] == 1
    assert result["program_completed"] is True
    assert verified.core_result["aed_score"]["part_scores"][0]["outcome"] == "no_shock"
    assert _gate(result["gates"], "aed_shock_delivered")["applicable"] is False

    old_definition = definition(adapter=EOF_VENT_ADAPTER_VERSION, required=1, minimum=(30, 2))
    old = evaluate(verified.core_result, old_definition, verified)
    assert old["reason_codes"] == [MINIMUM]
    assert not old["program_completed"]


def test_minimum_excludes_virtual_partner_actions_in_v6_only():
    cpr = cpr_session([(30, 2)] * 3)
    partner = [{"event": 20, "timestamp": 50}, {"event": 21, "timestamp": 15600}]
    new, result = run(definition(two_rescuers=True), cpr, vp=partner)
    assert new.core_result["action_count"] == {"comp": 90, "vent": 6}
    assert new.learner_counts["comp"] < 90 and new.learner_counts["vent"] < 6
    assert MINIMUM in result["reason_codes"]
    # With only the recorded learner quantity required, the minimum reason disappears.
    _, lenient = run(definition(two_rescuers=True, minimum=tuple(new.learner_counts.values())), cpr, vp=partner)
    assert MINIMUM not in lenient["reason_codes"]


def test_gates_mode_replaces_score_failure_and_keeps_reason_order():
    cpr = cpr_session([(20, 2)])
    _, result = run(definition(pass_rule="gates"), cpr)
    assert result["score"]["decision"] == "fail"
    assert result["reason_codes"] == [GOAL, MINIMUM, GATE]
    assert not result["program_completed"]
    with pytest.raises(JourneyError, match="CALCULATOR_CONTRACT_MISMATCH"):
        DynamoJobRepository.check_evaluation(
            {**result, "reason_codes": [GOAL, GATE, MINIMUM]}, {"definition_json": json.dumps(definition(pass_rule="gates"))},
        )


def _bridge():
    ts, count = 100, 0
    cpr = [packet()]
    for _ in range(6):
        presses, ts, count = _compressions(30, ts, count)
        breaths, ts = _breaths(2, ts, count)
        cpr.extend(presses + breaths)
    arrived = ts
    cpr.extend(packet(count, timestamp=arrived + offset) for offset in (100, 150))
    presses, ts, count = _compressions(3, arrived + 200, count)
    cpr.extend(presses)
    power_on, shock = arrived + 280, ts + 100
    for index in range(2):
        presses, ts, count = _compressions(30, shock + 250 if index == 0 else ts, count)
        breaths, ts = _breaths(2, ts, count)
        cpr.extend(presses + breaths)
    aed = [(0, arrived), (1, power_on), (10, power_on + 20), (13, power_on + 40), (16, shock)]
    vp = [{"event": 20, "timestamp": arrived}, {"event": 21, "timestamp": shock}]
    return b"".join(cpr), b"".join(_aed_packet(*entry) for entry in aed), vp


def test_arrived_marks_bridge_and_analysis_still_sees_whole_actions():
    cpr, aed, vp = _bridge()
    config = definition(required=8, minimum=(120, 8), two_rescuers=True, aed=True)
    _, with_arrival = run(config, cpr, aed, vp)
    _, without_arrival = run(config, cpr, aed[10:], vp)
    assert with_arrival["goal"]["observed"] == without_arrival["goal"]["observed"] == 8
    assert _gate(with_arrival["gates"], "aed_no_contact_during_analysis")["passed"] is False
    assert _gate(with_arrival["gates"], "aed_arrival_to_power_on")["value"] == 0.28
    assert _gate(without_arrival["gates"], "aed_arrival_to_power_on")["applicable"] is False

    options = CalculationOptions(collect_gates=True)
    _, _, marked = run_with_options(cpr, aed, config["condition"], options, vp)
    _, _, unmarked = run_with_options(cpr, aed[10:], config["condition"], options, vp)
    assert [(cycle.part_number, cycle.cycle_number) for cycle in marked.cycles] == [
        *((1, number) for number in range(1, 7)), (2, 1), (2, 2),
    ]
    # The first compression after a completed breath begins a new group.
    # With no ARRIVED event, the exposed VP pressure is in that unfinished
    # group, still outside the closed-cycle count.
    extra = [cycle for cycle in unmarked.cycles if cycle.part_number == 1 and cycle.cycle_number == 7]
    assert len(extra) == 1 and extra[0].compression_action_count == 1 and extra[0].ventilation_action_count == 0
    assert extra[0].actor_type == "ONLY:VP"


def test_recorded_aed_t1_and_arrival_gate_with_and_without_arrived():
    cpr, aed = ((DATASET / name).read_bytes() for name in ("cpr_2.bin", "aed_2.bin"))
    config = definition(aed=True)
    for stream, applicable, seconds in ((aed, True, 2.95), (aed[10:], False, 0)):
        verified, result = run(config, cpr, stream)
        part = verified.core_result["aed_score"]["part_scores"][0]
        assert part["intervals"]["t1"] == seconds
        assert part["outcome"] == "shock"
        gate = _gate(result["gates"], "aed_arrival_to_power_on")
        assert gate["applicable"] is applicable
        assert gate["value"] == (seconds if applicable else None)


def test_fractional_gate_survives_worker_commit_and_api_restart():
    from mock_journey.course_settings import fixture_course_settings
    from mock_journey.dev_course import DummyDevCourseProvider
    from mock_journey.execution_definitions import execution_catalog
    from tests._synth import multipart_event
    from tests.journey_support import JourneyStore, V2Journey
    from tests.vcc_support import event

    execution = execution_catalog()
    source = DummyDevCourseProvider(settings=fixture_course_settings(), execution=execution)
    h = V2Journey(JourneyStore.memory(), provider=source, execution=execution)
    token = h.login().token
    course = source.list_assignments(source.learner)[0].public_ids
    placement = source.fetch_bundle(source.list_assignments(source.learner)[0]).placements[4]
    attempt = h.start(token, course, placement.public_link_id)
    attempt_id = attempt["attemptId"]
    path = f"/api/v2/attempts/{attempt_id}/calculation/"
    cpr, aed, vp = _bridge()
    parts = multipart_event({
        "rawHexBPfile": cpr, "aedHexBPfile": aed, "condition": json.dumps(attempt["condition"]),
        "vp_event_list": json.dumps(vp),
    })
    request = event("POST", path, token=token, content_type=parts["headers"]["Content-Type"])
    request.update(body=parts["body"], isBase64Encoded=True)
    h.expect(202, "POST", path, event=request)
    assert h.work(attempt_id)
    stored = h.store.row(f"ATTEMPT#{attempt_id}", "META")
    assert "gates" not in stored["evaluation"]
    gates = json.loads(stored["evaluation_gates_json"])
    assert _gate(gates, "aed_arrival_to_power_on")["value"] == 0.28
    assert h.restart().result(token, attempt_id)["evaluation"]["gates"] == gates
