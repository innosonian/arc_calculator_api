"""D139: below the ARC minimum quantity the scores are shown, but the session does not pass.

The rule (arc-internal-detection-v5 evaluations only):

* Minimum (D07): ARC2020/ARC2025 CPR programs (two-rescuer ones included) need
  adult/child 90 and infant 45 compressions AND 6 ventilations. Other
  guidelines, Compression Only and Ventilation Only have no such minimum.
* If either group is below its minimum, ``score.decision`` is "fail" whatever
  the displayed overall is, so ``program_completed`` is false.
* ``reason_codes`` order: goal reason, ``MINIMUM_QUANTITY_NOT_MET``,
  ``SCORE_NOT_PASS``. ``SCORE_NOT_PASS`` appears only when the unchanged
  tester rule itself fails; "pass" carries neither code.
* The retained adapters (v4, pending-v3, pending-v2) and unversioned
  definitions are untouched: no gate, no new code (their null policy already
  keeps such a session from passing).

Thresholds below are written out by hand (not read from NullPolicy), so the
tests do not follow a change of the production predicate. Scores of real
sessions are the unchanged tester's.
"""

from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

from mock_journey import typed
from mock_journey.assembly import internal_calculator
from mock_journey.catalog import PROGRAMS
from mock_journey.contracts import VerifiedCalculation, expected_profile_version, minimum_quantity_policy
from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import LoadedInput, ProjectionSchema, project_input, typed_identity
from mock_journey.worker import evaluate
from tests._synth import comp_session, cpr_session
from tests.calculation_options_support import STEM
from tests.eof_vent_support import CPR_STOPPED_AT_SIXTH_BREATH, TESTER_RECORDING, VO_STOPPED_AT_EIGHTH_BREATH


V5, V4 = "arc-internal-detection-v5", "arc-internal-detection-v4"
PENDING_V3, PENDING_V2 = "arc-internal-detection-pending-v3", "arc-local-calculator-pending-v2"
G, M, F = "GOAL_NOT_MET", "MINIMUM_QUANTITY_NOT_MET", "SCORE_NOT_PASS"
DATASET = Path(__file__).parent / "dataset"
PROJECTION = "minimum-gate-test-projection-v1"
RAW_BASE = "calculator_result/interpreted_rtdata/arc/test/_no_org/2023-11-14/" + STEM
REQUIRED = {row[0]: (row[2], row[3]) for row in PROGRAMS}


def _condition(target="adult", training="cpr", guideline="ARC2025", two_rescuers=False):
    return {"mode": "training", "target": target, "training_type": training, "guideline": guideline,
            "cpr_cycle_type": "152" if target == "infant" else "302", "is_2rescuers": two_rescuers}


def _definition(adapter=V5, *, kind="cycles", required=3, **condition):
    definition = {"condition": _condition(**condition), "calculation_profile": {},
                  "goal": {"kind": kind, "required": required}, "catalog_version": "mock-catalog-v1",
                  "adapter_version": adapter, "projection_version": PROJECTION}
    profile = expected_profile_version(adapter)
    if profile is not None:
        definition["profile_version"] = profile
    return definition


def _assess(definition, comp, vent, *, overall=90, observed=None):
    """worker.evaluate on a verified result with these whole-session counts and displayed overall."""
    goal = definition["goal"]
    adapter = definition["adapter_version"]
    pending = adapter in (PENDING_V3, PENDING_V2) and goal["kind"] == "cycles"
    status = "pending_policy" if pending else "evaluated" if expected_profile_version(adapter) else None
    verified = VerifiedCalculation({"action_count": {"comp": comp, "vent": vent}}, goal["kind"],
                                   None if pending else goal["required"] if observed is None else observed,
                                   "no_chart", goal_status=status)
    assessment = evaluate({"cpr_score": {"total_score": {"overall": overall}}}, definition, verified)
    # Whatever evaluate produces is what the repository accepts for the same definition.
    assert DynamoJobRepository.check_evaluation(assessment, {"definition_json": json.dumps(definition)}) == assessment
    return assessment


def _outcome(assessment):
    return assessment["score"]["decision"], assessment["program_completed"], assessment["reason_codes"]


# ---------------------------------------------------------------------------
# 1. The rule in worker.evaluate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("guideline", ["ARC2020", "ARC2025"])
@pytest.mark.parametrize("target,minimum", [("adult", 90), ("child", 90), ("infant", 45)])
def test_both_groups_must_reach_the_minimum_to_pass(target, minimum, guideline):
    definition = _definition(target=target, guideline=guideline)
    # At the minimum and above: the displayed score decides alone.
    for comp, vent in ((minimum, 6), (minimum + 1, 7), (300, 20)):
        assert _outcome(_assess(definition, comp, vent)) == ("pass", True, [])
        assert _outcome(_assess(definition, comp, vent, overall=79)) == ("fail", False, [F])
    # One compression or one ventilation short, or both: not a pass even at overall 90 or 100.
    for comp, vent in ((minimum - 1, 6), (minimum, 5), (minimum - 1, 5), (0, 6), (minimum, 0), (0, 0)):
        for overall in (80, 90, 100):
            assert _outcome(_assess(definition, comp, vent, overall=overall)) == ("fail", False, [M])
        for overall in (79, 0, None):
            assert _outcome(_assess(definition, comp, vent, overall=overall)) == ("fail", False, [M, F])


def test_adult_and_infant_minimums_differ_only_in_compressions():
    # 45..89 compressions with 6 ventilations: enough for an infant, not for an adult or a child.
    for comp in (45, 60, 89):
        assert _outcome(_assess(_definition(target="infant"), comp, 6)) == ("pass", True, [])
        for target in ("adult", "child"):
            assert _outcome(_assess(_definition(target=target), comp, 6)) == ("fail", False, [M])


def test_reason_order_is_goal_then_minimum_then_score():
    definition = _definition()
    assert _outcome(_assess(definition, 89, 6, observed=2)) == ("fail", False, [G, M])
    assert _outcome(_assess(definition, 89, 6, observed=2, overall=50)) == ("fail", False, [G, M, F])
    assert _outcome(_assess(definition, 90, 6, observed=2)) == ("pass", False, [G])
    assert _outcome(_assess(definition, 90, 6, observed=2, overall=50)) == ("fail", False, [G, F])
    # "pass" never carries the minimum or the score code.
    for comp, vent, overall, observed in ((90, 6, 90, 3), (90, 6, 90, 2), (200, 12, 80, 5)):
        reasons = _assess(definition, comp, vent, overall=overall, observed=observed)["reason_codes"]
        assert M not in reasons and F not in reasons


@pytest.mark.parametrize("program", ["mock-cpr", "mock-two-rescuer-cpr", "mock-two-rescuer-aed"])
def test_every_cpr_program_is_gated_the_same_way(program):
    kind, required = REQUIRED[program]
    definition = _definition(kind=kind, required=required, two_rescuers=program != "mock-cpr")
    assert _outcome(_assess(definition, 90, 6)) == ("pass", True, [])
    assert _outcome(_assess(definition, 89, 6)) == ("fail", False, [M])
    assert _outcome(_assess(definition, 90, 5)) == ("fail", False, [M])


@pytest.mark.parametrize("target", ["adult", "child", "infant"])
@pytest.mark.parametrize("guideline", ["AHA2020", "ERC2020", "STD2015"])
def test_other_guidelines_have_no_minimum_gate(guideline, target):
    definition = _definition(target=target, guideline=guideline)
    for comp, vent in ((0, 0), (30, 2), (44, 5), (89, 5)):
        assert _outcome(_assess(definition, comp, vent)) == ("pass", True, [])
        assert _outcome(_assess(definition, comp, vent, overall=79)) == ("fail", False, [F])


@pytest.mark.parametrize("target", ["adult", "child", "infant"])
@pytest.mark.parametrize("kind,training,required", [("compressions", "compression_only", 60),
                                                    ("ventilations", "ventilation_only", 8)])
def test_single_skill_programs_have_no_cpr_minimum(kind, training, required, target):
    definition = _definition(kind=kind, required=required, target=target, training=training)
    # A Compression Only session has no ventilation and fewer than 90 compressions; it still passes.
    comp, vent = (60, 0) if kind == "compressions" else (0, 8)
    assert _outcome(_assess(definition, comp, vent)) == ("pass", True, [])
    assert _outcome(_assess(definition, comp, vent, overall=79)) == ("fail", False, [F])


@pytest.mark.parametrize("adapter", [V4, PENDING_V3, PENDING_V2, "retained-v1"])
def test_older_adapters_have_no_gate_and_no_new_code(adapter):
    definition = _definition(adapter)
    pending = adapter in (PENDING_V3, PENDING_V2)
    goal_reasons = ["GOAL_POLICY_UNRESOLVED"] if pending else []
    for comp, vent in ((90, 6), (89, 6), (90, 5), (0, 0)):
        # Exactly the previous formula: the score decision alone, whatever the counts are.
        assert _outcome(_assess(definition, comp, vent)) == ("pass", not pending, goal_reasons)
        assert _outcome(_assess(definition, comp, vent, overall=79)) == ("fail", False, goal_reasons + [F])
    # They never read the counts at all (a core without them is still evaluated).
    status = "pending_policy" if pending else "evaluated" if expected_profile_version(adapter) else None
    bare = VerifiedCalculation({}, "cycles", None if pending else 3, "no_chart", goal_status=status)
    assert evaluate({"cpr_score": {"total_score": {"overall": 90}}}, definition, bare)["score"] == {"decision": "pass"}


@pytest.mark.parametrize("core", [
    {}, {"action_count": None}, {"action_count": {"comp": 90}}, {"action_count": {"comp": 90, "vent": 6, "x": 1}},
    {"action_count": {"comp": True, "vent": 6}}, {"action_count": {"comp": 90.0, "vent": 6}},
    {"action_count": {"comp": -1, "vent": 6}}, {"action_count": {"comp": "90", "vent": 6}},
])
def test_current_adapter_never_passes_a_result_whose_counts_cannot_be_read(core):
    verified = VerifiedCalculation(core, "cycles", 3, "no_chart", goal_status="evaluated")
    with pytest.raises(JourneyError) as error:
        evaluate({"cpr_score": {"total_score": {"overall": 100}}}, _definition(), verified)
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


@pytest.mark.parametrize("damage", [
    lambda c: c.pop("guideline"), lambda c: c.pop("target"), lambda c: c.update(target="teen"),
    lambda c: c.update(guideline="ARC1999"),
])
def test_current_adapter_never_passes_under_a_condition_it_cannot_configure(damage):
    definition = _definition()
    damage(definition["condition"])
    verified = VerifiedCalculation({"action_count": {"comp": 300, "vent": 20}}, "cycles", 3, "no_chart",
                                   goal_status="evaluated")
    with pytest.raises((JourneyError, KeyError)) as error:
        evaluate({"cpr_score": {"total_score": {"overall": 100}}}, definition, verified)
    if isinstance(error.value, JourneyError):
        assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


def test_predicate_is_the_score_policys_own_rule():
    # contracts.minimum_quantity_policy is NullPolicy.create: its scope and thresholds, not a copy.
    from calculators.cycle_evaluator import NullPolicy
    from services.config import Config
    for target, training, guideline, comp, vent in (
            ("adult", "cpr", "ARC2025", 89, 6), ("adult", "cpr", "ARC2025", 90, 6), ("infant", "cpr", "ARC2020", 44, 6),
            ("infant", "cpr", "ARC2020", 45, 5), ("adult", "cpr", "AHA2020", 0, 0),
            ("adult", "compression_only", "ARC2025", 0, 0), ("child", "ventilation_only", "ARC2025", 0, 0)):
        cond = _condition(target, training, guideline)
        expected = NullPolicy.create(Config(cond).calculation_config, comp, vent)
        actual = minimum_quantity_policy(cond, comp, vent)
        assert (actual.chest_null, actual.vent_null, actual.active) == (expected.chest_null, expected.vent_null,
                                                                         expected.active)


# ---------------------------------------------------------------------------
# 2. jobs.check_evaluation (the stored evaluation has no counts)
# ---------------------------------------------------------------------------

def _stored(decision, reasons, *, observed=3, required=3, kind="cycles", status="evaluated"):
    met = observed >= required
    goal = {"kind": kind, "required": required, "observed": observed, "met": met}
    if status is not None:
        goal["status"] = status
    return {"goal": goal, "score": {"decision": decision}, "program_completed": met and decision == "pass",
            "reason_codes": list(reasons)}


def _check(value, definition):
    return DynamoJobRepository.check_evaluation(value, {"definition_json": json.dumps(definition)})


def _refused(value, definition):
    with pytest.raises(JourneyError) as error:
        _check(value, definition)
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


@pytest.mark.parametrize("observed,goal_reasons", [(3, []), (2, [G])])
def test_current_adapter_accepts_exactly_three_fail_explanations(observed, goal_reasons):
    definition = _definition()
    for tail in ([F], [M], [M, F]):
        value = _stored("fail", goal_reasons + tail, observed=observed)
        assert _check(value, definition) == value
    assert _check(_stored("pass", goal_reasons, observed=observed), definition) == _stored("pass", goal_reasons,
                                                                                         observed=observed)
    for tail in ([], [F, M], [M, M], [M, F, F], [F, F], ["OTHER"], [M, "OTHER"]):
        _refused(_stored("fail", goal_reasons + tail, observed=observed), definition)
    for tail in ([M], [F], [M, F]):
        _refused(_stored("pass", goal_reasons + tail, observed=observed), definition)
    # The minimum code never precedes the goal reason.
    if goal_reasons:
        _refused(_stored("fail", [M] + goal_reasons, observed=observed), definition)
        _refused(_stored("fail", [M, F], observed=observed), definition)  # the goal reason is missing
    # A completed program is never stored with a fail decision.
    completed = {**_stored("fail", [M]), "program_completed": True}
    _refused(completed, definition)
    for wrong_type in ((M,), M, None, {M: True}):
        _refused({**_stored("fail", [M]), "reason_codes": wrong_type}, definition)


@pytest.mark.parametrize("condition", [
    {"training": "compression_only"}, {"training": "ventilation_only"}, {"guideline": "AHA2020"},
    {"guideline": "ERC2020"}, {"guideline": "STD2015"},
])
def test_minimum_code_is_refused_where_the_minimum_cannot_apply(condition):
    kind = {"compression_only": "compressions", "ventilation_only": "ventilations"}.get(condition.get("training"),
                                                                                       "cycles")
    definition = _definition(kind=kind, required=3, **condition)
    assert _check(_stored("fail", [F], kind=kind), definition) == _stored("fail", [F], kind=kind)
    for tail in ([M], [M, F]):
        _refused(_stored("fail", tail, kind=kind), definition)


@pytest.mark.parametrize("adapter", [V4, PENDING_V3, PENDING_V2, "retained-v1"])
def test_older_adapters_refuse_the_minimum_code_and_keep_their_exact_rule(adapter):
    definition = _definition(adapter)
    versioned = expected_profile_version(adapter) is not None
    if adapter in (PENDING_V3, PENDING_V2):
        base = {"goal": {"kind": "cycles", "required": 3, "observed": None, "met": None, "status": "pending_policy"},
                "program_completed": False}
        accepted_fail = {**base, "score": {"decision": "fail"}, "reason_codes": ["GOAL_POLICY_UNRESOLVED", F]}
        assert _check(accepted_fail, definition) == accepted_fail
        for reasons in (["GOAL_POLICY_UNRESOLVED", M], ["GOAL_POLICY_UNRESOLVED", M, F], [M]):
            _refused({**base, "score": {"decision": "fail"}, "reason_codes": reasons}, definition)
        return
    status = "evaluated" if versioned else None
    assert _check(_stored("fail", [F], status=status), definition) == _stored("fail", [F], status=status)
    assert _check(_stored("pass", [], status=status), definition) == _stored("pass", [], status=status)
    for tail in ([M], [M, F], [F, M]):
        _refused(_stored("fail", tail, status=status), definition)
    _refused(_stored("pass", [M], status=status), definition)


# ---------------------------------------------------------------------------
# 3. Real calculations through the adapters
# ---------------------------------------------------------------------------

def _through_adapter(program, data, *, target="adult", adapter=V5, guideline="ARC2025", aed=b""):
    kind, required = REQUIRED[program]
    training = {"compressions": "compression_only", "ventilations": "ventilation_only"}.get(kind, "cpr")
    definition = _definition(adapter, kind=kind, required=required, target=target, training=training,
                             guideline=guideline, two_rescuers=program in ("mock-two-rescuer-cpr",
                                                                           "mock-two-rescuer-aed"))
    cond = definition["condition"]
    projected = project_input({"condition": deepcopy(cond), "cpr_b64_data": data, "aed_b64_data": aed,
                               "vp_event_list": []}, definition, ProjectionSchema(PROJECTION, {}))
    binding = {"attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()), "input_digest": typed_identity(projected),
               "adapter_version": adapter, "projection_version": PROJECTION,
               "job_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    calculator = internal_calculator(adapter, projection=PROJECTION, stage="test")
    raw = calculator.calculate(LoadedInput(projected, RAW_BASE), binding, lambda: None)
    verified = calculator.validate_response(raw, projected, binding)
    assessment = evaluate(verified.core_result, definition, verified)
    assert _check(assessment, definition) == assessment
    counts = typed.parse_json(raw)["counts"]
    total = verified.core_result["cpr_score"]["total_score"]
    return ((counts["comp"], counts["vent"]), total["overall"], assessment["goal"]["observed"],
            assessment["score"]["decision"], assessment["program_completed"], assessment["reason_codes"]), total


def _recording(number):
    return (DATASET / f"cpr_{number}.bin").read_bytes()


@pytest.mark.parametrize("name,data,expected", [
    # ((comp, vent), overall shown, observed cycles, decision, completed, reason_codes) -- adult CPR, ARC2025
    ("tester", TESTER_RECORDING, ((61, 6), 83, 3, "fail", False, [M])),      # compressions short; 83 is shown
    ("cpr_1", _recording(1), ((48, 2), 70, 1, "fail", False, [G, M, F])),
    ("cpr_2", _recording(2), ((99, 5), 86, 3, "fail", False, [M])),          # one ventilation short
    ("cpr_3", _recording(3), ((100, 3), 82, 2, "fail", False, [G, M])),
    ("cpr_4", _recording(4), ((69, 6), 80, 3, "fail", False, [M])),          # compressions short; exactly 80 shown
    ("cpr_5", _recording(5), ((36, 4), 66, 4, "fail", False, [M, F])),
    ("3x(30+2)", cpr_session([(30, 2)] * 3), ((90, 6), 99, 3, "pass", True, [])),
    ("3x(30+2) cut", CPR_STOPPED_AT_SIXTH_BREATH, ((90, 6), 99, 3, "pass", True, [])),   # the sixth breath by D138
    ("89/6", cpr_session([(30, 2), (30, 2), (29, 2)]), ((89, 6), 98, 3, "fail", False, [M])),
    ("90/5", cpr_session([(30, 2), (30, 2), (30, 1)]), ((90, 5), 99, 3, "fail", False, [M])),
    ("3x(10+1)", cpr_session([(10, 1)] * 3), ((30, 3), 81, 3, "fail", False, [M])),      # scores shown, not a pass
    ("1x(30+2)", cpr_session([(30, 2)]), ((30, 2), 99, 1, "fail", False, [G, M])),
], ids=lambda value: value if type(value) is str else "")
def test_adult_cpr_sessions_under_the_current_adapter(name, data, expected):
    outcome, total = _through_adapter("mock-cpr", data)
    assert outcome == expected
    # D139 keeps the scores visible: no group is nulled below the minimum.
    for key in ("score_comp_depth", "score_recoil", "score_hand_position", "score_comp_count",
                "score_vent_vol", "score_vent_count", "score_vent_rate", "overall"):
        assert total[key] is not None, (name, key)


@pytest.mark.parametrize("cycles,counts,minimum_met", [
    ([(15, 2)] * 3, (45, 6), True),
    ([(15, 2), (15, 2), (14, 2)], (44, 6), False),
    ([(15, 2), (15, 2), (15, 1)], (45, 5), False),
])
def test_infant_cpr_boundary_with_real_sessions(cycles, counts, minimum_met):
    outcome, _ = _through_adapter("mock-cpr", cpr_session(cycles), target="infant")
    # The synthetic infant session's own score does not pass (unchanged tester); the
    # minimum code appears exactly on the 44/6 and 45/5 sides and never at 45/6.
    assert outcome[0] == counts and outcome[3] == "fail"
    assert (M in outcome[5]) is (not minimum_met)
    assert outcome[5] == ([] if minimum_met else [M]) + [F]


@pytest.mark.parametrize("program,required,per_cycle", [("mock-two-rescuer-cpr", 8, 30),
                                                       ("mock-two-rescuer-aed", 10, 15)])
def test_two_rescuer_programs_are_gated_with_real_sessions(program, required, per_cycle):
    full, _ = _through_adapter(program, cpr_session([(per_cycle, 2)] * required))
    assert full[0] == (per_cycle * required, 2 * required) and M not in full[5]
    if per_cycle == 30:
        assert full[3:] == ("pass", True, [])
    else:
        # Ten 15-compression cycles (the counter byte holds at most 255) meet the quantity; the
        # unchanged tester fails the adult 30:2 compression-count score, which is the only reason.
        assert full[3:] == ("fail", False, [F])
    short, _ = _through_adapter(program, TESTER_RECORDING)
    assert short[0] == (61, 6) and short[3:] == ("fail", False, [G, M])
    # Enough cycles for the goal but below the minimum: the goal is met, the minimum is not.
    thin, total = _through_adapter(program, cpr_session([(5, 1)] * required))
    assert thin[2] == required and thin[3] == "fail" and thin[4] is False and M in thin[5] and G not in thin[5]
    assert total["overall"] is not None


@pytest.mark.parametrize("guideline", ["AHA2020", "ERC2020", "STD2015"])
def test_other_guidelines_are_not_gated_with_real_sessions(guideline):
    outcome, _ = _through_adapter("mock-cpr", cpr_session([(10, 1)] * 3), guideline=guideline)
    assert outcome[0] == (30, 3) and M not in outcome[5]
    assert outcome[5] == ([] if outcome[3] == "pass" else [F])
    tester, _ = _through_adapter("mock-cpr", TESTER_RECORDING, guideline=guideline)
    assert tester[0] == (61, 6) and M not in tester[5]


def test_single_skill_programs_are_not_gated_with_real_sessions():
    compressions, _ = _through_adapter("mock-compression-only", comp_session(60))
    assert compressions == ((60, 0), 100, 60, "pass", True, [])
    ventilations, _ = _through_adapter("mock-ventilation-only", VO_STOPPED_AT_EIGHTH_BREATH, target="infant")
    assert ventilations == ((0, 8), 92, 8, "pass", True, [])


@pytest.mark.parametrize("adapter", [V4, PENDING_V3])
def test_retained_adapters_results_are_unchanged_by_the_gate(adapter):
    pending = adapter == PENDING_V3
    goal = ["GOAL_POLICY_UNRESOLVED"] if pending else []
    # Their null policy decides as before; the minimum code never appears.
    tester, total = _through_adapter("mock-cpr", TESTER_RECORDING, adapter=adapter)
    assert tester[0] == (61, 5) and total["overall"] is None and tester[3:] == ("fail", False, goal + [F])
    # cpr_2 (99 / 5): the ventilation group is null, the chest-only overall 89 passes under v4.
    cpr_2, total = _through_adapter("mock-cpr", _recording(2), adapter=adapter)
    assert cpr_2[0] == (99, 5) and total["overall"] == 89 and total["score_vent_vol"] is None
    assert cpr_2[3:] == ("pass", not pending, goal)
    full, _ = _through_adapter("mock-cpr", cpr_session([(30, 2)] * 3), adapter=adapter)
    assert full[0] == (90, 6) and full[3:] == ("pass", not pending, goal)
