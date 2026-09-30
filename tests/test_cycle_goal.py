"""D136: the CPR completion cycle rule (Q22 closed).

Observed cycles of a ``cycles`` goal = the number of cycles the calculator
classified as ``cpr`` (compressions followed by ventilations, closed). The
last unfinished group and compression-only / ventilation-only / not-calculated
groups are excluded; a virtual partner's cycles count; AED actions do not take
part. required stays the catalog value (3 / 8 / 10); completion = cycles met
AND score pass. The rule is bound to the new adapter
``arc-internal-detection-v4`` / profile ``tester-goal-cycles-v1``; the retained
``arc-internal-detection-pending-v3`` still calculates its own attempts as
pending_policy and ``arc-local-calculator-pending-v2`` only verifies.

Expected observed counts below come from the calculator's own cycle
classification of the recorded datasets (tests/dataset/cpr_1..5.bin) and the
synthetic sessions; scores are the unchanged tester's.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from config.constants import (
    CALC_CASE_CPR, CALC_CASE_DID_NOT_RESCUE_VENT, CALC_CASE_NOT_CALC, CALC_CASE_ONLY_COMP, CALC_CASE_ONLY_VENT,
    EVENT_ID_END_COMP, EVENT_ID_START_COMP,
)
from mock_journey import typed
from mock_journey.assembly import internal_calculator
from mock_journey.catalog import PROGRAMS
from mock_journey.contracts import (
    CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION, CYCLE_GOAL_PROFILE_VERSION, PENDING_GOAL_ADAPTER_VERSION,
    PENDING_GOAL_PROFILE_VERSION, RETAINED_ADAPTER_VERSIONS, RETAINED_PENDING_GOAL_ADAPTER_VERSION,
    VerifiedCalculation,
)
from mock_journey.cycle_goal import closed_cycle_count
from mock_journey.errors import JourneyError
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import LoadedInput, ProjectionSchema, project_input, typed_identity
from mock_journey.worker import evaluate
from services.calculation_context import CalculationEvidence, CycleEvidence
from services.legacy_document import _is_pass
from tests._synth import comp_session, cpr_session, vo_session
from tests.request_support import condition


DATASET = Path(__file__).parent / "dataset"
PROJECTION = "cycle-goal-test-projection-v1"
RAW_BASE = ("calculator_result/interpreted_rtdata/arc/test/_no_org/2023-11-14/"
            "CPR-ACTION-1700000000-12345678-1234-4234-9234-123456789abc")
REQUIRED = {row[0]: row[3] for row in PROGRAMS if row[2] == "cycles"}  # mock-cpr 3, 2-rescuer 8, 2-rescuer AED 10
MODES = (("mock-cpr", False, False), ("mock-two-rescuer-cpr", True, False), ("mock-two-rescuer-aed", True, True))


# ---------------------------------------------------------------------------
# 1. The resolver
# ---------------------------------------------------------------------------

def _cycle(number, case, actor="ONLY:RP", part=1, comp=30, vent=2):
    return CycleEvidence(part_number=part, cycle_number=number, calc_case=case, actor_type=actor,
                         compression_action_count=comp, ventilation_action_count=vent)


def _evidence(*cycles):
    comp = sum(cycle.compression_action_count for cycle in cycles)
    vent = sum(cycle.ventilation_action_count for cycle in cycles)
    return CalculationEvidence(comp, vent, len(cycles), tuple(cycles))


CYCLES_DEFINITION = {"goal": {"kind": "cycles", "required": 3}}


@pytest.mark.parametrize("cycles,expected", [
    ((), 0),
    ((_cycle(1, CALC_CASE_CPR),), 1),
    ((_cycle(1, CALC_CASE_CPR), _cycle(2, CALC_CASE_CPR), _cycle(3, CALC_CASE_CPR)), 3),
    # The last unfinished group (compressions without ventilations) is not a closed cycle.
    ((_cycle(1, CALC_CASE_CPR), _cycle(2, CALC_CASE_CPR), _cycle(3, CALC_CASE_ONLY_COMP, vent=0)), 2),
    # Ventilation-only, not-calculated and rescue-breath groups do not count either.
    ((_cycle(1, CALC_CASE_ONLY_VENT, comp=0), _cycle(2, CALC_CASE_CPR), _cycle(3, CALC_CASE_NOT_CALC, comp=9, vent=0),
      _cycle(4, CALC_CASE_DID_NOT_RESCUE_VENT), _cycle(5, CALC_CASE_CPR)), 2),
    ((_cycle(1, CALC_CASE_ONLY_COMP, vent=0),), 0),
    ((_cycle(1, CALC_CASE_ONLY_VENT, comp=0),), 0),
    # A virtual partner's cycles count, whatever the actor type (D136, no actor distinction).
    ((_cycle(1, CALC_CASE_CPR, "ONLY:VP"), _cycle(2, CALC_CASE_CPR, "VP:COMP"), _cycle(3, CALC_CASE_CPR, "VP:VENT")), 3),
    # Cycles of every part count (an AED part boundary does not reset the count).
    ((_cycle(1, CALC_CASE_CPR), _cycle(2, CALC_CASE_CPR), _cycle(1, CALC_CASE_CPR, part=2), _cycle(2, CALC_CASE_NOT_CALC, part=2, comp=9, vent=0)), 3),
    # Compression/ventilation counts inside a cycle are the score's business, not the count's.
    ((_cycle(1, CALC_CASE_CPR, comp=9, vent=1), _cycle(2, CALC_CASE_CPR, comp=50, vent=2)), 2),
])
def test_closed_cycle_count_counts_only_cpr_cycles(cycles, expected):
    assert closed_cycle_count(_evidence(*cycles), deepcopy(CYCLES_DEFINITION)) == expected


@pytest.mark.parametrize("evidence,definition", [
    (None, CYCLES_DEFINITION),
    ({"cycles": ()}, CYCLES_DEFINITION),
    (_evidence(_cycle(1, CALC_CASE_CPR)), {"goal": {"kind": "compressions", "required": 60}}),
    (_evidence(_cycle(1, CALC_CASE_CPR)), {"goal": None}),
    (_evidence(_cycle(1, CALC_CASE_CPR)), {}),
    (_evidence(_cycle(1, CALC_CASE_CPR)), None),
])
def test_closed_cycle_count_rejects_foreign_evidence_or_goal(evidence, definition):
    with pytest.raises(JourneyError) as error:
        closed_cycle_count(evidence, definition)
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


# ---------------------------------------------------------------------------
# 2. The real calculator under the current adapter
# ---------------------------------------------------------------------------

def _definition(cond, required, *, adapter=CYCLE_GOAL_ADAPTER_VERSION, profile=CYCLE_GOAL_PROFILE_VERSION):
    return {"condition": deepcopy(cond), "calculation_profile": {}, "goal": {"kind": "cycles", "required": required},
            "catalog_version": "mock-catalog-v1", "profile_version": profile,
            "adapter_version": adapter, "projection_version": PROJECTION}


def _accepted(cpr, aed, cond, required, *, vp_events=(), adapter=CYCLE_GOAL_ADAPTER_VERSION,
              profile=CYCLE_GOAL_PROFILE_VERSION):
    definition = _definition(cond, required, adapter=adapter, profile=profile)
    projected = project_input({"condition": cond, "cpr_b64_data": cpr, "aed_b64_data": aed,
                               "vp_event_list": list(vp_events)},
                              definition, ProjectionSchema(PROJECTION, {}))
    binding = {"attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()), "input_digest": typed_identity(projected),
               "adapter_version": adapter, "projection_version": PROJECTION,
               "job_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    return LoadedInput(projected, RAW_BASE), binding, definition


def _calculate(cpr, aed, cond, required, **options):
    loaded, binding, definition = _accepted(cpr, aed, cond, required, **options)
    adapter = internal_calculator(binding["adapter_version"], projection=PROJECTION, stage="test")
    raw = adapter.calculate(loaded, binding, lambda: None)
    verified = adapter.validate_response(raw, loaded.projected, binding)
    assessment = evaluate(verified.core_result, definition, verified)
    checked = DynamoJobRepository.check_evaluation(assessment, {"definition_json": json.dumps(definition)})
    assert checked == assessment
    return verified, assessment, typed.parse_json(raw)


def _condition(target, two_rescuers):
    cond = condition(target, "cpr")
    cond["is_2rescuers"] = two_rescuers
    return cond


def _dataset(number, aed):
    cpr = (DATASET / f"cpr_{number}.bin").read_bytes()
    return cpr, (DATASET / f"aed_{number}.bin").read_bytes() if aed else b""


# Closed cpr cycles of each recorded dataset (independent of target, rescuer count and AED file).
DATASET_CYCLES = {1: 1, 2: 3, 3: 2, 4: 3, 5: 4}


@pytest.mark.parametrize("target", ("adult", "child", "infant"))
@pytest.mark.parametrize("program,two_rescuers,aed", MODES)
@pytest.mark.parametrize("number", sorted(DATASET_CYCLES))
def test_dataset_cycle_goals_are_the_closed_cycle_count_and_completion_needs_the_score(number, program, two_rescuers, aed, target):
    cpr, aed_bytes = _dataset(number, aed)
    required = REQUIRED[program]
    verified, assessment, candidate = _calculate(cpr, aed_bytes, _condition(target, two_rescuers), required)
    observed = DATASET_CYCLES[number]
    passed = bool(_is_pass(verified.core_result, None, None, None, target))
    assert verified.goal_status == "evaluated" and verified.observed == observed
    assert candidate["schema"] == "arc-internal-calculation-v3"
    assert candidate["goal"] == {"kind": "cycles", "observed": observed, "status": "evaluated"}
    assert assessment["goal"] == {"kind": "cycles", "required": required, "observed": observed,
                                  "met": observed >= required, "status": "evaluated"}
    assert assessment["score"] == {"decision": "pass" if passed else "fail"}
    assert assessment["program_completed"] is (observed >= required and passed)
    assert assessment["reason_codes"] == (([] if observed >= required else ["GOAL_NOT_MET"])
                                          + ([] if passed else ["SCORE_NOT_PASS"]))
    assert "GOAL_POLICY_UNRESOLVED" not in assessment["reason_codes"]
    # The device grouping (training_stats.cycle_count) is not the completion count.
    assert verified.core_result["training_stats"]["cycle_count"] >= observed


@pytest.mark.parametrize("number,program,target,expected", [
    # (observed, score decision, program_completed)
    (2, "mock-cpr", "adult", (3, "pass", True)),          # score 89: 3 closed cycles + pass -> completed
    (2, "mock-cpr", "child", (3, "pass", True)),
    (2, "mock-cpr", "infant", (3, "fail", False)),        # 3 cycles met, infant score fails
    (2, "mock-two-rescuer-cpr", "adult", (3, "pass", False)),   # 3 < 8
    (2, "mock-two-rescuer-aed", "adult", (3, "pass", False)),   # 3 < 10
    (3, "mock-cpr", "adult", (2, "pass", False)),         # pass, but 2 closed cycles < 3
    (4, "mock-cpr", "adult", (3, "fail", False)),         # 3 cycles met, score 74 fails
    (4, "mock-two-rescuer-aed", "adult", (3, "pass", False)),   # AED raises the score, not the count
    (5, "mock-cpr", "adult", (4, "fail", False)),         # 4 short cycles, no score
    (1, "mock-cpr", "adult", (1, "fail", False)),
])
def test_dataset_table_rows_pin_observed_score_and_completion(number, program, target, expected):
    two_rescuers, aed = next((two, aed) for name, two, aed in MODES if name == program)
    cpr, aed_bytes = _dataset(number, aed)
    _, assessment, _ = _calculate(cpr, aed_bytes, _condition(target, two_rescuers), REQUIRED[program])
    assert (assessment["goal"]["observed"], assessment["score"]["decision"], assessment["program_completed"]) == expected


@pytest.mark.parametrize("target", ("adult", "infant"))
@pytest.mark.parametrize("program,data,observed", [
    ("mock-cpr", cpr_session([(30, 2)] * 3), 3),
    ("mock-cpr", cpr_session([(30, 2)]), 1),
    ("mock-cpr", cpr_session([(30, 2)] * 2), 2),
    ("mock-cpr", cpr_session([(30, 2)] * 2 + [(30, 0)]), 2),  # last group open: excluded
    ("mock-cpr", comp_session(60), 0),                          # compression only: no closed cycle
    ("mock-cpr", vo_session(8), 0),                             # ventilation only: no closed cycle
    ("mock-two-rescuer-cpr", cpr_session([(30, 2)] * 8), 8),
    ("mock-two-rescuer-cpr", cpr_session([(30, 2)] * 7), 7),
    ("mock-two-rescuer-aed", cpr_session([(15, 2)] * 10), 10),
    ("mock-two-rescuer-aed", cpr_session([(15, 2)] * 9), 9),
])
def test_synthetic_sessions_meet_the_catalog_required_cycles_exactly_at_the_threshold(target, program, data, observed):
    two_rescuers = program != "mock-cpr"
    required = REQUIRED[program]
    verified, assessment, _ = _calculate(data, b"", _condition(target, two_rescuers), required)
    passed = assessment["score"]["decision"] == "pass"
    assert verified.observed == observed
    assert assessment["goal"]["met"] is (observed >= required)
    assert assessment["program_completed"] is (observed >= required and passed)


def test_virtual_partner_cycles_count_toward_the_goal_but_not_the_score():
    data = cpr_session([(30, 2)] * 3)
    last = int.from_bytes(data[-8:], "big")
    partner = [{"event": EVENT_ID_START_COMP, "timestamp": 0}, {"event": EVENT_ID_END_COMP, "timestamp": last + 1}]
    real, real_assessment, _ = _calculate(data, b"", _condition("adult", True), 8)
    partnered, assessment, _ = _calculate(data, b"", _condition("adult", True), 8, vp_events=partner)
    assert real.observed == partnered.observed == 3
    assert real_assessment["goal"]["met"] is False and assessment["goal"]["met"] is False
    # The unchanged tester scores a partner-only session 0 (ONLY:VP), so pass and completion differ by score only.
    assert real.core_result["cpr_score"]["total_score"]["overall"] == 99
    assert partnered.core_result["cpr_score"]["total_score"]["overall"] == 0
    assert assessment["reason_codes"] == ["GOAL_NOT_MET", "SCORE_NOT_PASS"]
    met, met_assessment, _ = _calculate(data, b"", _condition("adult", False), 3, vp_events=partner)
    assert met.observed == 3 and met_assessment["goal"]["met"] is True
    assert met_assessment["program_completed"] is False and met_assessment["reason_codes"] == ["SCORE_NOT_PASS"]


# ---------------------------------------------------------------------------
# 3. Version rules
# ---------------------------------------------------------------------------

def test_cycle_goal_adapter_rejects_the_pending_profile_and_pending_candidates():
    cond = _condition("adult", False)
    loaded, binding, _ = _accepted(cpr_session([(30, 2)] * 3), b"", cond, 3, profile=PENDING_GOAL_PROFILE_VERSION)
    adapter = internal_calculator(CYCLE_GOAL_ADAPTER_VERSION, projection=PROJECTION, stage="test")
    with pytest.raises(JourneyError) as error:
        adapter.calculate(loaded, binding, lambda: None)
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    # A pending-shaped candidate is not a cycle-goal result.
    loaded, binding, _ = _accepted(cpr_session([(30, 2)] * 3), b"", cond, 3)
    raw = adapter.calculate(loaded, binding, lambda: None)
    for damage in (lambda c: c["goal"].update(status="pending_policy", observed=None),
                   lambda c: c.update(schema="arc-internal-calculation-v2"),
                   lambda c: c["goal"].update(observed=4),
                   lambda c: c["goal"].pop("status")):
        damaged = typed.parse_json(raw)
        damage(damaged)
        with pytest.raises(JourneyError) as error:
            adapter.validate_response(typed.json_bytes(damaged), loaded.projected, binding)
        assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


def test_retained_pending_v3_still_calculates_its_own_attempts_as_pending_policy():
    cond = _condition("adult", False)
    loaded, binding, definition = _accepted(cpr_session([(30, 2)] * 3), b"", cond, 3,
                                            adapter=PENDING_GOAL_ADAPTER_VERSION, profile=PENDING_GOAL_PROFILE_VERSION)
    adapter = internal_calculator(PENDING_GOAL_ADAPTER_VERSION, projection=PROJECTION, stage="test")
    assert adapter.can_calculate is True and adapter.cycle_goal_resolver is None
    raw = adapter.calculate(loaded, binding, lambda: None)
    verified = adapter.validate_response(raw, loaded.projected, binding)
    assert typed.parse_json(raw)["schema"] == "arc-internal-calculation-v2"
    assert verified.goal_status == "pending_policy" and verified.observed is None
    assessment = evaluate(verified.core_result, definition, verified)
    assert assessment["goal"] == {"kind": "cycles", "required": 3, "observed": None, "met": None,
                                  "status": "pending_policy"}
    assert assessment["program_completed"] is False and assessment["reason_codes"][0] == "GOAL_POLICY_UNRESOLVED"
    assert DynamoJobRepository.check_evaluation(assessment, {"definition_json": json.dumps(definition)}) == assessment
    # The same raw is not a v4 candidate: neither adapter accepts the other's version binding.
    current = internal_calculator(CYCLE_GOAL_ADAPTER_VERSION, projection=PROJECTION, stage="test")
    with pytest.raises(JourneyError):
        current.validate_response(raw, loaded.projected, binding)


def test_retained_pending_v2_is_verify_only(monkeypatch):
    monkeypatch.setattr("main.run_calculator", lambda *a, **k: pytest.fail("verify-only adapter ran the core"))
    loaded, binding, _ = _accepted(cpr_session([(30, 2)] * 3), b"", _condition("adult", False), 3,
                                   adapter=RETAINED_PENDING_GOAL_ADAPTER_VERSION, profile=PENDING_GOAL_PROFILE_VERSION)
    adapter = internal_calculator(RETAINED_PENDING_GOAL_ADAPTER_VERSION, projection=PROJECTION, stage="test")
    assert adapter.can_calculate is False
    with pytest.raises(JourneyError) as error:
        adapter.calculate(loaded, binding, lambda: None)
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"


def test_cycle_goal_adapter_is_never_built_without_the_rule():
    for options in ({}, {"allow_pending_cycle_goal": True}, {"allow_pending_cycle_goal": True,
                                                               "cycle_goal_resolver": closed_cycle_count}):
        with pytest.raises(ValueError):
            InternalCalculator(version=CYCLE_GOAL_ADAPTER_VERSION, projection_version=PROJECTION, stage="test",
                               **options)
    with pytest.raises(ValueError):
        internal_calculator("arc-internal-detection-v5", projection=PROJECTION, stage="test")


def test_worker_evaluate_and_check_evaluation_follow_the_adapter_version():
    definition = _definition(_condition("adult", False), 3)
    assessment = evaluate({"cpr_score": {"total_score": {"overall": 90}}}, definition,
                          VerifiedCalculation({}, "cycles", 3, "no_chart", goal_status="evaluated"))
    assert assessment == {"goal": {"kind": "cycles", "required": 3, "observed": 3, "met": True, "status": "evaluated"},
                          "score": {"decision": "pass"}, "program_completed": True, "reason_codes": []}
    short = evaluate({"cpr_score": {"total_score": {"overall": 90}}}, definition,
                     VerifiedCalculation({}, "cycles", 2, "no_chart", goal_status="evaluated"))
    assert short["program_completed"] is False and short["reason_codes"] == ["GOAL_NOT_MET"]
    for verified in (VerifiedCalculation({}, "cycles", None, "no_chart", goal_status="pending_policy"),
                     VerifiedCalculation({}, "cycles", 3, "no_chart")):
        with pytest.raises(JourneyError) as error:
            evaluate({"cpr_score": {"total_score": {"overall": 90}}}, definition, verified)
        assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    pending = {**assessment, "goal": {"kind": "cycles", "required": 3, "observed": None, "met": None,
                                      "status": "pending_policy"}, "program_completed": False,
               "reason_codes": ["GOAL_POLICY_UNRESOLVED"]}
    with pytest.raises(JourneyError):
        DynamoJobRepository.check_evaluation(pending, {"definition_json": json.dumps(definition)})
    old = {**definition, "adapter_version": PENDING_GOAL_ADAPTER_VERSION, "profile_version": PENDING_GOAL_PROFILE_VERSION}
    assert DynamoJobRepository.check_evaluation(pending, {"definition_json": json.dumps(old)}) == pending
    with pytest.raises(JourneyError):
        DynamoJobRepository.check_evaluation(assessment, {"definition_json": json.dumps(old)})


def test_aws_settings_accept_only_the_current_and_exact_retained_registry():
    from mock_journey.aws_settings import AwsSettings
    from tests.aws_runtime_support import configuration
    assert CURRENT_ADAPTER_VERSION == CYCLE_GOAL_ADAPTER_VERSION
    assert RETAINED_ADAPTER_VERSIONS == (RETAINED_PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION)
    for role in ("api", "worker"):
        config = configuration(role)
        assert config["execution"]["current_adapter_version"] == CYCLE_GOAL_ADAPTER_VERSION
        assert AwsSettings.parse(json.dumps(config), role).execution == (
            CYCLE_GOAL_ADAPTER_VERSION, "arc-local-projection-v1", RETAINED_ADAPTER_VERSIONS)
        for change in ({"current_adapter_version": PENDING_GOAL_ADAPTER_VERSION},
                       {"retained_adapter_versions": [RETAINED_PENDING_GOAL_ADAPTER_VERSION]},
                       {"retained_adapter_versions": [PENDING_GOAL_ADAPTER_VERSION, RETAINED_PENDING_GOAL_ADAPTER_VERSION]},
                       {"retained_adapter_versions": []}):
            invalid = json.dumps({**config, "execution": {**config["execution"], **change}})
            with pytest.raises(ValueError, match="Invalid explicit AWS journey configuration."):
                AwsSettings.parse(invalid, role)


def test_local_health_completion_policy_reports_every_goal_kind_evaluated():
    from local_server import http as local_http
    assert dict(local_http._COMPLETION_POLICY) == {"cycles": "evaluated", "compressions": "evaluated",
                                                  "ventilations": "evaluated"}


def test_execution_catalog_definitions_carry_the_cycle_goal_adapter_and_profile():
    from mock_journey.execution_definitions import execution_catalog
    execution = execution_catalog()
    assert execution.required_bindings == ((CYCLE_GOAL_ADAPTER_VERSION, "arc-local-projection-v1"),)
    for program, _, kind, _ in PROGRAMS:
        for target in ("adult", "child", "infant"):
            value = execution.get_definition(program, target)
            assert (value["adapter_version"], value["profile_version"]) == (CYCLE_GOAL_ADAPTER_VERSION,
                                                                            CYCLE_GOAL_PROFILE_VERSION)


# ---------------------------------------------------------------------------
# 4. The public /api/v2 journey (in-memory store; integration_tests has the DynamoDB Local twin)
# ---------------------------------------------------------------------------

def test_cpr_course_practice_and_final_complete_by_the_cycle_rule_in_memory():
    from tests.cycle_goal_support import cpr_course_journey
    from tests.journey_support import JourneyStore
    cpr_course_journey(JourneyStore.memory())
